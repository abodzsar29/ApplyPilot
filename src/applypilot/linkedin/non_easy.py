"""LinkedIn non-Easy-Apply search and autonomous external apply handoff."""

from __future__ import annotations

import logging
import re
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    cleanup_worker,
    kill_all_chrome,
    launch_chrome,
    reset_worker_dir,
)
from applypilot.qwen_mcp import QwenMCPAgent, get_effective_model_and_provider

log = logging.getLogger(__name__)

LINKEDIN_BASE_URL = "https://www.linkedin.com"
PROTONMAIL_INBOX_URL = "https://mail.proton.me/u/0/inbox"
DEFAULT_NONEASY_APPLIED_JOBS_FILE = Path("config/linkedin_noneasy_applied.json")


def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            }
        }
    }


def _extract_result(output: str) -> str:
    """Parse the agent output into a normalized status string."""
    lowered = output.lower()
    if "credit balance is too low" in lowered:
        return "failed:provider_credit_low"
    if "insufficient credits" in lowered or "insufficient balance" in lowered:
        return "failed:provider_credit_low"

    for result_status in ("APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"):
        if f"RESULT:{result_status}" in output:
            return result_status.lower()

    if "RESULT:FAILED" in output:
        for out_line in output.splitlines():
            if "RESULT:FAILED" not in out_line:
                continue
            reason = out_line.split("RESULT:FAILED:", 1)[-1].strip()
            reason = re.sub(r'[*`"]+$', "", reason).strip() or "unknown"
            return f"failed:{reason}"

    return "failed:no_result_line"


def _read_resume_text(resume_path: Path) -> str:
    """Read plain-text resume content when a sibling text file exists."""
    txt_path = resume_path.with_suffix(".txt")
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8").strip()
    return ""


def _answer_bank_summary(answers: dict) -> str:
    """Render recurring answers and experience years for the agent prompt."""
    lines: list[str] = []

    for key in (
        "visa_sponsorship",
        "authorized_to_work",
        "onsite",
        "linkedin_profile",
        "current_job_title",
        "gender",
        "current_salary",
        "expected_salary",
    ):
        value = answers.get(key)
        if value:
            lines.append(f"- {key}: {value}")

    for key, value in sorted(answers.items()):
        if key.endswith("_level") and value:
            lines.append(f"- {key}: {value}")

    years_experience = answers.get("years_experience", {}) or {}
    if years_experience:
        lines.append("- years_experience:")
        for skill, years in sorted(years_experience.items()):
            lines.append(f"  - {skill}: {years}")

    return "\n".join(lines) if lines else "- none provided"


def _screening_override_summary(answers: dict) -> str:
    """Render explicit question-to-answer overrides for recurring screenings."""
    overrides = answers.get("screening_overrides", {}) or {}
    if not overrides:
        return "- none provided"

    lines: list[str] = []
    for question_hint, answer in overrides.items():
        if not question_hint or answer in (None, ""):
            continue
        lines.append(f"- when question contains '{question_hint}': answer '{answer}'")
    return "\n".join(lines) if lines else "- none provided"


def _education_summary(answers: dict) -> str:
    """Render structured education data for deterministic degree answers."""
    education = answers.get("education", {}) or {}
    if not education:
        return "- none provided"

    lines: list[str] = []
    for key, value in education.items():
        if value in (None, "", []):
            continue
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) if lines else "- none provided"


def _mailbox_context(config_dict: dict) -> tuple[str | None, str]:
    """Return inbox URL and applicant email for email-code retrieval flows."""
    profile = config_dict.get("profile", {}) or {}
    email = (profile.get("email", "") or "").strip()
    inbox_url = (config_dict.get("mail_inbox_url", "") or "").strip()

    if inbox_url:
        return inbox_url, email

    lowered = email.lower()
    if lowered.endswith(("@proton.me", "@protonmail.com", "@pm.me")):
        return PROTONMAIL_INBOX_URL, email

    return None, email


def _applied_jobs_registry_path(config_dict: dict) -> Path:
    """Return the JSON file used to persist previously applied non-easy jobs."""
    configured = (config_dict.get("applied_jobs_file", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_NONEASY_APPLIED_JOBS_FILE


def _normalize_registry_value(value: str) -> str:
    """Normalize title/company strings for stable duplicate detection."""
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _job_registry_key(title: str, company: str) -> str:
    """Build a normalized registry key from title and company."""
    return f"{_normalize_registry_value(title)}\t{_normalize_registry_value(company)}"


def _normalize_registry_url(url: str) -> str:
    """Normalize job URLs for stable duplicate detection."""
    return (url or "").strip().rstrip("/").casefold()


def _load_applied_jobs_registry(config_dict: dict) -> list[dict]:
    """Load the JSON registry of previously applied non-easy jobs."""
    path = _applied_jobs_registry_path(config_dict)
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("Applied jobs registry is not valid JSON: %s", path)
        return []
    except Exception:
        log.warning("Could not read applied jobs registry: %s", path, exc_info=True)
        return []

    return data if isinstance(data, list) else []


def _registry_lookups(entries: list[dict]) -> tuple[set[str], set[str]]:
    """Build duplicate-detection lookups from registry entries."""
    title_company_keys: set[str] = set()
    url_keys: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title", "")
        company = entry.get("company", "")
        if title or company:
            title_company_keys.add(_job_registry_key(title, company))

        for field in ("application_url", "linkedin_url"):
            normalized_url = _normalize_registry_url(entry.get(field, ""))
            if normalized_url:
                url_keys.add(normalized_url)
    return title_company_keys, url_keys


def _write_applied_jobs_registry(config_dict: dict, entries: list[dict]) -> None:
    """Persist the JSON registry of previously applied non-easy jobs."""
    path = _applied_jobs_registry_path(config_dict)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _record_applied_job(config_dict: dict, registry_entries: list[dict], job: dict) -> None:
    """Append a successfully applied job to the JSON registry file."""
    registry_entries.append({
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "linkedin_url": job.get("linkedin_url", ""),
        "application_url": job.get("application_url", ""),
        "recorded_at": int(time.time()),
    })
    _write_applied_jobs_registry(config_dict, registry_entries)


def _build_prompt(
    job: dict,
    config_dict: dict,
    dry_run: bool = False,
    mailbox_url: str | None = None,
    session_started_at: str | None = None,
) -> str:
    """Build an autonomous prompt for external non-Easy-Apply sites."""
    from applypilot.apply.prompt import _build_captcha_section

    profile = config_dict.get("profile", {}) or {}
    answers = config_dict.get("answers", {}) or {}
    resume_path = Path(config_dict["resume_path"]).resolve()
    resume_text = _read_resume_text(resume_path)
    applicant_email = (profile.get("email", "") or "").strip()

    profile_lines = [
        f"First name: {profile.get('first_name', '')}",
        f"Last name: {profile.get('last_name', '')}",
        f"Email: {applicant_email}",
        f"Phone country code: {profile.get('phone_country_code', '')}",
        f"Phone number: {profile.get('phone_number', '')}",
        f"City: {profile.get('city', '')}",
        f"Postcode: {profile.get('postcode', '')}",
    ]
    answer_bank = _answer_bank_summary(answers)
    screening_overrides = _screening_override_summary(answers)
    education_summary = _education_summary(answers)
    captcha_section = _build_captcha_section()
    submit_instruction = (
        "Do NOT click the final Submit/Apply button. Review the form, then output RESULT:APPLIED (dry run)."
        if dry_run
        else "Submit the application after verifying all required fields are correct."
    )
    email_2fa_section = ""
    if mailbox_url:
        email_2fa_section = f"""

== EMAIL / 2FA ==
- Applicant email address: {applicant_email or "not provided"}
- Mail inbox tab should already be available at: {mailbox_url}
- Current application session started at: {session_started_at or "unknown"}
- Use the mail tab ONLY if the application flow visibly asks for an email verification code, OTP, passcode, one-time code, or 2FA code.
- When such a code is required:
  1. First note the employer / ATS name, the page domain, and that the code request is happening now for the current application.
  2. Treat any older OTP / verification emails already sitting in the inbox as stale by default. Do NOT reuse a code from a previous session.
  3. Only use an email if it clearly matches the current application by sender, employer name, ATS name, domain, or a timestamp that is newer than the current code request.
  4. If the inbox already contains older verification emails, ignore them and wait for a newer matching email to arrive. Refresh/check again before opening a code email.
  5. If needed, use a resend-code action on the application page, then wait for the new matching email.
  6. Before submitting a code, verify it came from the current application flow rather than a previous job application.
  7. If there is any ambiguity between an old code email and a new one, do NOT guess. Wait for the newer matching email or use resend.
  8. Switch to the Proton Mail tab.
  9. Wait briefly and refresh/check for the newest relevant email from the employer / ATS / verification sender.
  10. Open the newest matching message, extract the verification code, copy it, and return to the application tab.
  11. Paste the code into the blocking verification field and continue the registration/application flow.
- If the site never requests an email code, ignore the mail tab completely.
- If a visible email-code challenge is blocking progress and the mailbox cannot be accessed or no code arrives after reasonable retries, output RESULT:FAILED:email_2fa_unavailable.
"""

    return f"""You are an autonomous browser agent completing a LinkedIn non-Easy-Apply job application.

This job was discovered on LinkedIn, but it is NOT an Easy Apply job. Do not try to use LinkedIn Easy Apply. Go directly to the external application site and complete the application there.

== JOB ==
LinkedIn URL: {job['linkedin_url']}
External application URL: {job['application_url']}
Title: {job.get('title', 'Unknown')}
Company: {job.get('company', 'Unknown')}

== FILES ==
Resume PDF (upload this): {resume_path}

== APPLICANT PROFILE ==
{chr(10).join(profile_lines)}

== STANDARD ANSWERS ==
{answer_bank}

== SCREENING OVERRIDES ==
{screening_overrides}

== EDUCATION ==
{education_summary}

== RESUME TEXT ==
{resume_text or "Not available as text. Use the uploaded resume PDF plus the profile/answer bank above."}
{email_2fa_section}

== CORE RULES ==
1. Never pause and wait for a human. Either answer, skip safely, or fail with a clear RESULT code.
2. Answer work authorization, sponsorship, citizenship, licenses, education credentials, criminal history, and security clearance truthfully from the provided profile/answers only.
3. For common screening questions not explicitly listed, infer the best truthful answer from the profile, answer bank, resume text, and job page.
4. For open-ended required questions, write concise factual answers. Use the job description and the candidate profile. Do not invent employers, projects, degrees, certifications, or years.
5. Fill only mandatory fields by default. Treat fields as mandatory when they are marked with *, required, mandatory, aria-required, validation text, or when submission highlights them as missing.
6. If a question is optional and you lack enough truthful information, leave it blank.
7. Never sign in through Google, Microsoft, Okta, Auth0, or any SSO provider. If required, output RESULT:FAILED:sso_required.
8. Never grant camera, microphone, location, screen-sharing, identity-verification, or biometric permissions.
9. Never stop just because the form structure is unfamiliar. Read the page, inspect labels/options, and continue.

== QUESTION POLICY ==
- FIRST: check SCREENING OVERRIDES. If the visible question text substantially matches an override, use that override answer exactly.
- Prefer deterministic answers from the provided profile and answer bank.
- For location / commute / willing-to-work-onsite threshold questions, use answers.onsite when present.
- For visa / sponsorship / work permit / require support questions, use answers.visa_sponsorship when present.
- For authorized-to-work questions, use answers.authorized_to_work when present.
- For postcode / zip / postal code questions, use the applicant profile postcode when present.
- For LinkedIn profile / LinkedIn URL questions, use answers.linkedin_profile exactly, even if it is an empty string.
- For current or previous job title questions, use answers.current_job_title when present.
- For gender questions, use answers.gender when present.
- For current salary or salary history questions, use answers.current_salary when present.
- For expected salary or salary expectation questions, use answers.expected_salary when present.
- For degree and field-of-study questions, use EDUCATION first. Treat that section as the source of truth for bachelor's, master's, highest education, and Computer Science field checks.
- If EDUCATION provides explicit booleans such as computer_science_bachelors, computer_science_masters, or computer_science_bachelors_or_masters, use them directly for matching yes/no questions.
- If EDUCATION provides bachelors_field or masters_field, use those exact fields when asked what subject the degree is in.
- For yes/no tool questions: if the tool clearly matches a skill listed in years_experience with > 0 years, answer Yes.
- For "years of experience" questions: use the matching years_experience value when present.
- For open text such as "Why are you interested?" or "Tell us about yourself": write 2-3 sentences grounded in the visible job description and the provided profile/resume.
- If a required question would force fabrication, output RESULT:FAILED:unknown_required_question.

== PROCESS ==
1. Start by listing tabs/windows and switch to the newest non-LinkedIn, non-mail tab if one exists. The external application page is expected to already be open from LinkedIn. Use that live page instead of trying to rediscover the job.
2. If no external tab is already open, navigate directly to the external application URL as a fallback.
3. Read the page with browser_snapshot. Use the visible page content/HTML structure to understand the form.
4. If the page says the job is unavailable, listing not available, job no longer exists, position closed, no longer accepting applications, 404-like vacancy text, or any equivalent closure/unavailable message, stop and output RESULT:EXPIRED.
5. Upload the resume PDF when asked.
6. Fill only identifiable mandatory fields from the provided profile and answer bank.
7. Answer screening questions using the question policy above, but only when they are mandatory unless you intentionally choose to complete an optional field.
8. If the site requires account creation with email verification, complete it only when it can be done with the provided email address and, if needed, the mail-tab 2FA flow above.
9. If the site uses a magic link email instead of a code, stay in the current registration/application flow, switch to the mail tab, wait for the new matching email, open the magic link from that email, and continue the same application flow from the newly opened page/tab. Do NOT abandon the email wait and do NOT jump back prematurely before checking for the incoming magic-link email.
10. Before final submission, do one validation pass focused on missing required fields only.
11. If submit/review causes the page to jump back up, shows inline validation, red outlines, required-field messages, or highlights missing fields, fill those newly identified required fields and try again.
12. If the form has mandatory consent controls near the end, such as privacy policy, data processing, terms, consent, or acknowledgment checkboxes/radios required to proceed, select/accept only the required ones so submission can continue.
13. If the site is not a real job application form, output RESULT:FAILED:not_a_job_application.
14. If you hit a login wall that requires SSO or account creation you cannot complete safely, output RESULT:LOGIN_ISSUE.
15. {submit_instruction}
16. After submit, confirm success and output exactly one RESULT line.

== RESULT CODES ==
RESULT:APPLIED
RESULT:EXPIRED
RESULT:CAPTCHA
RESULT:LOGIN_ISSUE
RESULT:FAILED:reason

{captcha_section}

== GIVE UP CONDITIONS ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Job is closed or no longer accepting applications -> RESULT:EXPIRED
- Listing unavailable / posting removed / job not available -> RESULT:EXPIRED
- Broken page / unusable site -> RESULT:FAILED:page_error
"""


def _is_easy_apply(page) -> bool:
    """Return True if the LinkedIn job detail page exposes Easy Apply."""
    selectors = [
        ".job-details-top-card button:has-text('Easy Apply')",
        "[data-test-id='jobs-details'] button:has-text('Easy Apply')",
        ".jobs-detail__main-content button:has-text('Easy Apply')",
        "button[aria-label='Easy Apply job']",
        "button:has-text('Easy Apply')",
    ]
    for selector in selectors:
        try:
            for btn in page.query_selector_all(selector):
                if not btn.is_visible():
                    continue
                text = ((btn.text_content() or "") + " " + (btn.get_attribute("aria-label") or "")).lower()
                if "easy apply" in text:
                    return True
        except Exception:
            continue
    return False


def _find_apply_control(page):
    """Find a visible external apply control in the main job detail pane."""
    selectors = [
        ".jobs-apply-button--top-card",
        ".jobs-apply-button",
        "a:has-text('Apply on company website')",
        "a[aria-label*='Apply on company website']",
        "a[data-tracking-control-name*='apply']",
        "a:has-text('Apply')",
        "button:has-text('Apply on company website')",
        "button[aria-label*='Apply on company website']",
        "button:has-text('Apply')",
    ]
    for selector in selectors:
        try:
            for elem in page.query_selector_all(selector):
                if not elem.is_visible():
                    continue
                text = (elem.text_content() or "").strip().lower()
                aria = (elem.get_attribute("aria-label") or "").strip().lower()
                href = (elem.get_attribute("href") or "").strip()
                cls = (elem.get_attribute("class") or "").strip().lower()
                combined = f"{text} {aria}"
                if "apply" not in combined and "company website" not in combined:
                    if "apply" not in cls and "apply" not in href.lower():
                        continue
                if "easy apply" in combined:
                    continue
                return elem
        except Exception:
            continue
    return None


def _is_external_http_url(url: str) -> bool:
    """Return True when a URL is a non-LinkedIn HTTP(S) page."""
    cleaned = (url or "").strip()
    if not cleaned:
        return False
    parsed = urlparse(cleaned)
    host = (parsed.netloc or "").lower()
    return parsed.scheme in {"http", "https"} and bool(host) and "linkedin.com" not in host


def _safe_wait_for_timeout(page, timeout_ms: int) -> bool:
    """Sleep on a page unless it has already been closed."""
    try:
        if page.is_closed():
            return False
        page.wait_for_timeout(timeout_ms)
        return True
    except Exception:
        return False


def _clean_external_url(url: str) -> str | None:
    """Return a usable external application URL or None if still on LinkedIn."""
    cleaned = (url or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered.startswith("/"):
        return None
    parsed = urlparse(cleaned)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()

    if parsed.scheme not in {"http", "https"}:
        return None
    if not host or "." not in host:
        return None

    # Reject LinkedIn-owned and asset/CDN hosts.
    blocked_host_fragments = (
        "linkedin.com",
        "licdn.com",
        "linkedin-ei.com",
    )
    if any(fragment in host for fragment in blocked_host_fragments):
        return None
    if not lowered.startswith("http"):
        return None

    # Reject obvious assets/media rather than HTML application pages.
    blocked_suffixes = (
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
        ".css", ".js", ".woff", ".woff2", ".ttf", ".map",
        ".mp4", ".webm", ".mp3", ".pdf",
    )
    if path.endswith(blocked_suffixes):
        return None

    # Favor URLs that look like actual job/apply pages.
    plausible_tokens = (
        "apply", "job", "jobs", "career", "careers", "position",
        "posting", "opportunit", "vacanc", "greenhouse", "lever",
        "workday", "ashby", "smartrecruiters", "icims", "taleo",
    )
    if not any(token in f"{host}{path}" for token in plausible_tokens):
        return None

    return cleaned


def _normalize_linkedin_job_url(url: str) -> str:
    """Convert relative LinkedIn job URLs to absolute URLs."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    return urljoin(f"{LINKEDIN_BASE_URL}/", cleaned)


def _extract_external_apply_url(detail_page) -> str | None:
    """Extract the external application URL from a LinkedIn job detail page.

    Read-only on purpose: do not click anything during discovery.
    If LinkedIn does not expose a usable outbound URL in the DOM, skip the job.
    """
    candidates: list[str] = []

    apply_control = _find_apply_control(detail_page)
    if apply_control:
        attrs = [
            apply_control.get_attribute("href") or "",
            apply_control.get_attribute("data-tracking-url") or "",
            apply_control.get_attribute("data-apply-url") or "",
            apply_control.get_attribute("data-url") or "",
        ]
        candidates.extend(attrs)

    try:
        dom_urls = detail_page.eval_on_selector_all(
            "a[href], button[data-tracking-url], button[data-apply-url], [data-url]",
            """nodes => nodes.flatMap(node => [
                node.getAttribute('href') || '',
                node.getAttribute('data-tracking-url') || '',
                node.getAttribute('data-apply-url') || '',
                node.getAttribute('data-url') || ''
            ])""",
        )
        candidates.extend(dom_urls)
    except Exception:
        pass

    seen: set[str] = set()
    for raw in candidates:
        cleaned = _clean_external_url(raw)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        return cleaned

    # Fallback: one controlled click on the actual apply control, then
    # capture the destination and immediately close/revert any new page.
    if not apply_control:
        return None

    context = detail_page.context
    before_pages = list(context.pages)
    before_set = set(before_pages)
    original_url = detail_page.url

    try:
        with detail_page.expect_popup(timeout=5000) as popup_info:
            apply_control.click(timeout=5000)
        popup = popup_info.value
        try:
            popup.wait_for_load_state("commit", timeout=15000)
        except Exception:
            pass
        popup_url = _clean_external_url(popup.url)
        try:
            popup.close()
        except Exception:
            pass
        if popup_url:
            return popup_url
    except Exception:
        pass

    try:
        apply_control.click(timeout=5000)
        detail_page.wait_for_timeout(2500)
    except Exception:
        return None

    # Some LinkedIn flows open a new tab/window without triggering expect_popup.
    current_pages = list(context.pages)
    for page in current_pages:
        if page in before_set:
            continue
        try:
            page.wait_for_load_state("commit", timeout=15000)
        except Exception:
            pass
        page_url = _clean_external_url(page.url)
        try:
            page.close()
        except Exception:
            pass
        if page_url:
            return page_url

    # Some flows navigate the current detail page away from LinkedIn.
    navigated_url = _clean_external_url(detail_page.url)
    if detail_page.url != original_url:
        try:
            detail_page.go_back(wait_until="commit", timeout=15000)
            detail_page.wait_for_timeout(1000)
        except Exception:
            pass
        if navigated_url:
            return navigated_url

    # Best effort cleanup for any extra tabs that slipped through.
    for page in list(context.pages):
        if page not in before_set:
            try:
                page.close()
            except Exception:
                pass

    return None


def _open_external_application_page(detail_page):
    """Click LinkedIn's external apply control and keep the live destination page open."""
    apply_control = _find_apply_control(detail_page)
    if not apply_control:
        return None, None

    context = detail_page.context
    before_pages = list(context.pages)
    before_set = set(before_pages)
    original_url = detail_page.url

    try:
        with detail_page.expect_popup(timeout=5000) as popup_info:
            apply_control.click(timeout=5000)
        popup = popup_info.value
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        return popup, popup.url
    except Exception:
        pass

    try:
        apply_control.click(timeout=5000)
    except Exception:
        return None, None

    deadline = time.time() + 8
    while time.time() < deadline:
        for page in list(context.pages):
            if page in before_set:
                continue
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            return page, page.url

        try:
            if detail_page.is_closed():
                return None, None
            if detail_page.url != original_url and _is_external_http_url(detail_page.url):
                try:
                    detail_page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                return detail_page, detail_page.url
        except Exception:
            return None, None

        time.sleep(0.25)

    return None, None


def search_non_easy_jobs(config_dict: dict, headless: bool = False) -> tuple[list[dict], dict]:
    """Search LinkedIn for non-Easy-Apply jobs and extract external URLs."""
    job_title = config_dict.get("job_title", "")
    location = config_dict.get("location", "")
    title_keyword = config_dict.get("title_keyword", "").lower()
    max_applications = int(config_dict.get("max_applications", 20))
    scan_target = max(max_applications * 4, max_applications)
    max_pages = max(int(config_dict.get("max_scan_pages", 3)), 1)

    if not job_title or not location:
        raise ValueError("job_title and location are required")

    config.ensure_dirs()
    profile_dir = config.CHROME_WORKER_DIR / "linkedin-search"
    profile_dir.mkdir(parents=True, exist_ok=True)

    search_url = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote(job_title)}"
        f"&location={quote(location)}"
    )

    candidates: list[dict] = []
    found_jobs: list[dict] = []
    seen_urls: set[str] = set()
    stats = {
        "candidate_urls": 0,
        "easy_skipped": 0,
        "no_external_url": 0,
        "pages_scanned": 0,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(str(profile_dir), headless=headless)
        page = browser.pages[0] if browser.pages else browser.new_page()
        detail_page = browser.new_page()

        try:
            log.info("Searching LinkedIn non-Easy-Apply jobs: %s in %s", job_title, location)
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            page_num = 1
            seen_page_markers: set[str] = set()
            while len(candidates) < scan_target and page_num <= max_pages:
                job_cards = page.query_selector_all("a.base-card__full-link, div.job-card-container a[href*='/jobs/view/']")
                if not job_cards:
                    break

                page_marker = ""
                for card in job_cards[:5]:
                    href = _normalize_linkedin_job_url(card.get_attribute("href") or "")
                    if href:
                        page_marker = href
                        break
                if page_marker and page_marker in seen_page_markers:
                    log.info("LinkedIn scan stopped: repeated results page %s", page_num)
                    break
                if page_marker:
                    seen_page_markers.add(page_marker)

                stats["pages_scanned"] = page_num
                log.info(
                    "Scanning LinkedIn results page %d/%d (%d candidates collected)",
                    page_num,
                    max_pages,
                    len(candidates),
                )

                for card in job_cards:
                    href = _normalize_linkedin_job_url(card.get_attribute("href") or "")
                    if not href or "/jobs/" not in href or href in seen_urls:
                        continue

                    title_text = (card.text_content() or "").strip()
                    if title_keyword and title_keyword not in title_text.lower():
                        continue

                    seen_urls.add(href)
                    candidates.append({"linkedin_url": href, "title": title_text})
                    log.info("Queued candidate %d/%d: %s", len(candidates), scan_target, href)
                    if len(candidates) >= scan_target:
                        break

                if len(candidates) >= scan_target:
                    break

                next_btn = page.query_selector("button[aria-label*='next'], a[aria-label*='next'], button:has-text('Next')")
                if not next_btn:
                    break
                try:
                    next_btn.click()
                    page.wait_for_timeout(2000)
                    page_num += 1
                except Exception:
                    break

            stats["candidate_urls"] = len(candidates)

            for candidate in candidates:
                if len(found_jobs) >= max_applications:
                    break

                try:
                    log.info("Inspecting candidate: %s", candidate["linkedin_url"])
                    detail_page.goto(candidate["linkedin_url"], wait_until="commit", timeout=60000)
                    detail_page.wait_for_timeout(2500)

                    if _is_easy_apply(detail_page):
                        stats["easy_skipped"] += 1
                        log.info("Skipped Easy Apply job: %s", candidate["linkedin_url"])
                        continue

                    application_url = _extract_external_apply_url(detail_page)
                    if not application_url:
                        stats["no_external_url"] += 1
                        log.info("No external URL found: %s", candidate["linkedin_url"])
                        continue

                    company = ""
                    try:
                        company_elem = detail_page.query_selector("a.topcard__org-name-link, .job-details-jobs-unified-top-card__company-name a, .job-details-jobs-unified-top-card__company-name")
                        if company_elem:
                            company = (company_elem.text_content() or "").strip()
                    except Exception:
                        pass

                    found_jobs.append({
                        "linkedin_url": candidate["linkedin_url"],
                        "application_url": application_url,
                        "title": candidate["title"] or "Unknown title",
                        "company": company or "Unknown company",
                    })
                    log.info("Captured external application URL: %s", application_url)
                except PlaywrightTimeoutError:
                    stats["no_external_url"] += 1
                except Exception as e:
                    log.debug("Skipping LinkedIn job due to detail-page error: %s", e)
                    stats["no_external_url"] += 1

        finally:
            detail_page.close()
            page.close()
            browser.close()

    return found_jobs, stats


def _run_external_application(
    provider: str,
    model: str,
    agent: QwenMCPAgent | None,
    job: dict,
    config_dict: dict,
    port: int,
    dry_run: bool,
) -> tuple[str, int]:
    """Run the configured browser agent against a single external application URL."""
    mailbox_url, _email = _mailbox_context(config_dict)
    session_started_at = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    if provider == "claude":
        start = time.time()
        prompt = _build_prompt(
            job,
            config_dict,
            dry_run=dry_run,
            mailbox_url=mailbox_url,
            session_started_at=session_started_at,
        )

        mcp_config_path = config.APP_DIR / ".mcp-linkedin-noneasy-0.json"
        mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

        cmd = [
            "claude",
            "--model", model,
            "-p",
            "--mcp-config", str(mcp_config_path),
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--output-format", "stream-json",
            "--verbose", "-",
        ]

        worker_dir = reset_worker_dir(0)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(worker_dir),
        )

        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        log_file = config.LOG_DIR / f"linkedin_noneasy_claude_{int(start)}.log"
        with open(log_file, "w", encoding="utf-8") as lf:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                    elif msg_type == "result":
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=config.DEFAULTS["apply_timeout"])
        output = "\n".join(text_parts)
        status = _extract_result(output)
        duration_ms = int((time.time() - start) * 1000)
        return status, duration_ms

    if agent is None:
        raise RuntimeError(f"Provider '{provider}' requires an initialized MCP agent")

    start = time.time()
    prompt = _build_prompt(
        job,
        config_dict,
        dry_run=dry_run,
        mailbox_url=mailbox_url,
        session_started_at=session_started_at,
    )

    log_file = config.LOG_DIR / f"linkedin_noneasy_{provider}_{int(start)}.log"
    output = agent.run_prompt(prompt, log_path=log_file)
    status = _extract_result(output)
    duration_ms = int((time.time() - start) * 1000)
    return status, duration_ms


def _search_url(config_dict: dict) -> str:
    return (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote(config_dict.get('job_title', ''))}"
        f"&location={quote(config_dict.get('location', ''))}"
    )


def _iter_result_cards(page):
    return page.query_selector_all("a.base-card__full-link, div.job-card-container a[href*='/jobs/view/']")


def _ensure_mailbox_tab(context, config_dict: dict):
    """Open or reuse the configured mailbox inbox tab in the current browser context."""
    mailbox_url, email = _mailbox_context(config_dict)
    if not mailbox_url:
        return None

    for page in context.pages:
        try:
            if page.is_closed():
                continue
            current = (page.url or "").strip()
            if current.startswith(mailbox_url):
                return page
        except Exception:
            continue

    mailbox_page = context.new_page()
    try:
        mailbox_page.goto(mailbox_url, wait_until="domcontentloaded", timeout=60000)
        _safe_wait_for_timeout(mailbox_page, 1500)
        log.info("Opened mailbox tab for %s at %s", email or "<unknown email>", mailbox_url)
        return mailbox_page
    except Exception as exc:
        log.warning("Could not open mailbox tab %s: %s", mailbox_url, exc)
        try:
            mailbox_page.close()
        except Exception:
            pass
        return None


def _run_non_easy_setup_mode(config_dict: dict, headless: bool = False) -> dict:
    """Open LinkedIn search and mailbox tabs, then idle until interrupted."""
    job_title = config_dict.get("job_title", "")
    location = config_dict.get("location", "")

    if not job_title or not location:
        raise ValueError("job_title and location are required")

    summary = {
        "found": 0,
        "applied": 0,
        "skipped": 0,
        "failed": 0,
        "search_stats": {
            "candidate_urls": 0,
            "easy_skipped": 0,
            "no_external_url": 0,
            "pages_scanned": 0,
        },
        "jobs": [],
    }

    port = BASE_CDP_PORT
    chrome_proc = None

    try:
        chrome_proc = launch_chrome(0, port=port, headless=headless)

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            if not browser.contexts:
                raise RuntimeError("CDP browser has no contexts")
            context = browser.contexts[0]
            search_page = context.pages[0] if context.pages else context.new_page()
            mailbox_page = _ensure_mailbox_tab(context, config_dict)

            try:
                search_url = _search_url(config_dict)
                log.info("Setup mode: opening LinkedIn search page for %s in %s", job_title, location)
                search_page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                search_page.wait_for_timeout(3000)
                log.info("Setup mode ready. Browser will stay open until interrupted.")
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                log.info("Setup mode interrupted by user.")
            finally:
                if mailbox_page:
                    try:
                        mailbox_page.close()
                    except Exception:
                        pass
                try:
                    search_page.close()
                except Exception:
                    pass
                browser.close()
    finally:
        if chrome_proc:
            cleanup_worker(0, chrome_proc)
        kill_all_chrome()

    return summary


def _run_non_easy_apply_direct(config_dict: dict, model: str = "qwen-flash",
                               headless: bool = False, dry_run: bool = False) -> dict:
    """Process LinkedIn non-Easy-Apply jobs directly from search results."""
    job_title = config_dict.get("job_title", "")
    location = config_dict.get("location", "")
    title_keyword = config_dict.get("title_keyword", "").lower()
    max_applications = int(config_dict.get("max_applications", 20))
    max_pages = max(int(config_dict.get("max_scan_pages", 3)), 1)
    max_candidates = max(max_applications * 4, max_applications)

    if not job_title or not location:
        raise ValueError("job_title and location are required")

    summary = {
        "found": 0,
        "applied": 0,
        "skipped": 0,
        "failed": 0,
        "search_stats": {
            "candidate_urls": 0,
            "easy_skipped": 0,
            "no_external_url": 0,
            "pages_scanned": 0,
        },
        "jobs": [],
    }

    port = BASE_CDP_PORT
    chrome_proc = None
    registry_entries = _load_applied_jobs_registry(config_dict)
    applied_job_keys, applied_job_urls = _registry_lookups(registry_entries)

    try:
        chrome_proc = launch_chrome(0, port=port, headless=headless)
        effective_model, provider = get_effective_model_and_provider(model)
        agent = None if provider == "claude" else QwenMCPAgent(
            model=effective_model,
            mcp_config=_make_mcp_config(port),
        )

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            if not browser.contexts:
                raise RuntimeError("CDP browser has no contexts")
            context = browser.contexts[0]
            search_page = context.pages[0] if context.pages else context.new_page()
            detail_page = context.new_page()
            mailbox_page = _ensure_mailbox_tab(context, config_dict)

            try:
                search_url = _search_url(config_dict)
                log.info("Searching LinkedIn non-Easy-Apply jobs: %s in %s", job_title, location)
                search_page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                search_page.wait_for_timeout(3000)

                page_num = 1
                seen_urls: set[str] = set()
                inspected = 0

                while page_num <= max_pages and inspected < max_candidates and summary["found"] < max_applications:
                    cards = _iter_result_cards(search_page)
                    if not cards:
                        break

                    summary["search_stats"]["pages_scanned"] = page_num
                    log.info(
                        "Scanning LinkedIn results page %d/%d (%d jobs processed)",
                        page_num,
                        max_pages,
                        inspected,
                    )

                    for card in cards:
                        if inspected >= max_candidates or summary["found"] >= max_applications:
                            break

                        linkedin_url = _normalize_linkedin_job_url(card.get_attribute("href") or "")
                        if not linkedin_url or linkedin_url in seen_urls or "/jobs/view/" not in linkedin_url:
                            continue

                        title_text = (card.text_content() or "").strip()
                        if title_keyword and title_keyword not in title_text.lower():
                            continue

                        seen_urls.add(linkedin_url)
                        inspected += 1
                        summary["search_stats"]["candidate_urls"] = inspected
                        log.info("Inspecting LinkedIn job %d/%d: %s", inspected, max_candidates, linkedin_url)

                        if detail_page.is_closed():
                            detail_page = context.new_page()

                        try:
                            detail_page.goto(linkedin_url, wait_until="commit", timeout=60000)
                            _safe_wait_for_timeout(detail_page, 2500)
                        except PlaywrightTimeoutError:
                            summary["skipped"] += 1
                            summary["search_stats"]["no_external_url"] += 1
                            continue
                        except Exception:
                            summary["skipped"] += 1
                            summary["search_stats"]["no_external_url"] += 1
                            continue

                        if _is_easy_apply(detail_page):
                            summary["skipped"] += 1
                            summary["search_stats"]["easy_skipped"] += 1
                            log.info("Skipped Easy Apply job: %s", linkedin_url)
                            continue

                        company = "Unknown company"
                        try:
                            company_elem = detail_page.query_selector(
                                "a.topcard__org-name-link, "
                                ".job-details-jobs-unified-top-card__company-name a, "
                                ".job-details-jobs-unified-top-card__company-name"
                            )
                            if company_elem:
                                company = (company_elem.text_content() or "").strip() or "Unknown company"
                        except Exception:
                            pass

                        application_page, application_url = _open_external_application_page(detail_page)
                        if not application_page:
                            summary["skipped"] += 1
                            summary["search_stats"]["no_external_url"] += 1
                            log.info("No external application page opened after Apply click: %s", linkedin_url)
                            continue

                        job = {
                            "linkedin_url": linkedin_url,
                            "application_url": application_url or "",
                            "title": title_text or "Unknown title",
                            "company": company,
                        }
                        job_key = _job_registry_key(job["title"], job["company"])
                        job_urls = {
                            _normalize_registry_url(job["application_url"]),
                            _normalize_registry_url(job["linkedin_url"]),
                        }
                        job_urls.discard("")
                        if job_key in applied_job_keys or any(url in applied_job_urls for url in job_urls):
                            summary["skipped"] += 1
                            log.info(
                                "Skipping previously applied non-easy job: %s @ %s",
                                job["title"],
                                job["company"],
                            )
                            try:
                                if application_page is not detail_page:
                                    application_page.close()
                            except Exception:
                                pass
                            continue

                        summary["jobs"].append(job)
                        summary["found"] += 1
                        log.info("Opened external application page: %s", application_url or "<unknown>")

                        result, _duration_ms = _run_external_application(
                            provider=provider,
                            model=effective_model,
                            agent=agent,
                            job=job,
                            config_dict=config_dict,
                            port=port,
                            dry_run=dry_run,
                        )
                        if result == "applied":
                            summary["applied"] += 1
                            if not dry_run:
                                _record_applied_job(config_dict, registry_entries, job)
                                applied_job_keys.add(job_key)
                                applied_job_urls.update(job_urls)
                        else:
                            summary["failed"] += 1
                            log.info(
                                "Non-easy apply failed for %s: %s",
                                application_url or linkedin_url,
                                result,
                            )
                            if result == "failed:provider_credit_low":
                                log.error("Stopping non-easy apply run: provider credits are exhausted.")
                                return summary

                        # Cleanup any external tabs opened during apply, but keep the LinkedIn pages alive.
                        for pg in list(context.pages):
                            if pg is search_page or pg is detail_page or pg is mailbox_page:
                                continue
                            try:
                                pg.close()
                            except Exception:
                                pass

                    if summary["found"] >= max_applications or inspected >= max_candidates:
                        break

                    next_btn = search_page.query_selector(
                        "button[aria-label*='next'], a[aria-label*='next'], button:has-text('Next')"
                    )
                    if not next_btn:
                        break
                    try:
                        next_btn.click()
                        search_page.wait_for_timeout(2000)
                        page_num += 1
                    except Exception:
                        break
            finally:
                try:
                    detail_page.close()
                except Exception:
                    pass
                if mailbox_page:
                    try:
                        mailbox_page.close()
                    except Exception:
                        pass
                try:
                    search_page.close()
                except Exception:
                    pass
                browser.close()
    finally:
        if chrome_proc:
            cleanup_worker(0, chrome_proc)
        kill_all_chrome()

    return summary


def run_non_easy_apply(config_dict: dict, model: str = "qwen-flash",
                       headless: bool = False, dry_run: bool = False, setup: bool = False) -> dict:
    """Backward-compatible shim."""
    if setup:
        return _run_non_easy_setup_mode(config_dict, headless=headless)
    return _run_non_easy_apply_direct(config_dict, model=model, headless=headless, dry_run=dry_run)
