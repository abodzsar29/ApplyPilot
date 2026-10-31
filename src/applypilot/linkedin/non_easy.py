"""LinkedIn non-Easy-Apply search and autonomous external apply handoff."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.application_history import ApplicationHistory, HistoryError, cooldown_days
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    cleanup_worker,
    kill_all_chrome,
    launch_chrome,
    reset_worker_dir,
)
from applypilot.linkedin.email_verification import EmailVerificationHandler
from applypilot.linkedin.filters import configured_title_exclusion, title_is_ignored
from applypilot.linkedin.greenhouse import prefill_greenhouse_application
from applypilot.linkedin.intelligent_apply import PreparationError, intelligent_enabled, intelligent_session
from applypilot.linkedin.jobboards import JobboardFilter, load_jobboard_filter
from applypilot.linkedin.job_status import JobAvailabilityMonitor
from applypilot.linkedin.non_easy_questions import UNKNOWN_QUESTION_POLICY
from applypilot.linkedin.pagination import advance_results_page, wait_for_fresh_cards
from applypilot.linkedin.pagination import configured_max_scan_pages as _configured_max_scan_pages
from applypilot.linkedin.unified_apply import (
    _click_action_button,
    _configured_language_resume_paths,
    _detect_job_language,
    _ensure_configured_linkedin_search_location,
    _extract_company_name,
    _extract_job_title_from_card,
    _find_linkedin_next_page_button,
    _is_blacklisted_company,
    _job_language_allowed,
    _log_active_linkedin_search_state,
    _normalize_company_name,
    _select_resume_path_for_job,
)
from applypilot.qwen_mcp import QwenMCPAgent, get_configured_model, get_effective_model_and_provider

log = logging.getLogger(__name__)

LINKEDIN_BASE_URL = "https://www.linkedin.com"
PROTONMAIL_INBOX_URL = "https://mail.proton.me/u/0/inbox"


class AnswerFallbackError(RuntimeError):
    """The browser agent refused the answer policy even after a same-job retry."""


def _history_path() -> Path:
    return config.APP_DIR / "linkedin_noneasy_applied.jsonl"


def _job_registry_key(job: dict) -> str:
    """Prefer job identity; title/company is only a legacy fallback."""
    url = job.get("linkedin_url", "")
    if isinstance(url, str) and url:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)", parsed.path)
            if match:
                return f"linkedin:{match.group(1)}"
    title, company = job.get("title"), job.get("company")
    if not isinstance(title, str) or not isinstance(company, str):
        return ""
    title, company = (" ".join(value.split()).casefold() for value in (title, company))
    if not title or not company or title == "unknown title" or company == "unknown company":
        return ""
    return f"legacy:{title}\t{company}"


def _load_applied_job_keys() -> set[str]:
    keys: set[str] = set()
    try:
        with _history_path().open(encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    key = _job_registry_key(row) if isinstance(row, dict) else ""
                except (ValueError, TypeError):
                    key = ""
                if key:
                    keys.add(key)
                else:
                    log.warning("Ignoring invalid application history record on line %s", number)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("Cannot read application history: %s", _history_path())
        raise
    return keys


def _was_applied(job: dict, keys: set[str]) -> bool:
    job_key = _job_registry_key(job)
    legacy_key = _job_registry_key({"title": job.get("title"), "company": job.get("company")})
    return bool((job_key and job_key in keys) or (legacy_key and legacy_key in keys))


def _record_applied_job(job: dict) -> None:
    entry = {key: job.get(key, "") for key in ("title", "company", "linkedin_url", "application_url")}
    entry["recorded_at"] = int(time.time())
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A separator also isolates a truncated final record from an interrupted run.
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n" + json.dumps(entry, ensure_ascii=True) + "\n")


def _mailbox_context(settings: dict) -> tuple[str | None, str]:
    email = str((settings.get("profile") or {}).get("email") or "").strip()
    url = str(settings.get("mail_inbox_url") or "").strip()
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("mail_inbox_url must be an HTTP(S) inbox URL")
        return url, email
    if email.lower().endswith(("@proton.me", "@protonmail.com", "@pm.me")):
        return PROTONMAIL_INBOX_URL, email
    return None, email


def _ensure_mailbox_tab(context, settings: dict, excluded_pages=()):
    url, _ = _mailbox_context(settings)
    if not url:
        return None
    for page in context.pages:
        if page not in excluded_pages and not page.is_closed() and page.url.startswith(url):
            return page
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return page
    except Exception as exc:
        log.warning("Could not open mailbox: %s", exc)
        page.close()
        return None


def _close_application_tabs(context, keep):
    for page in list(context.pages):
        if page not in keep:
            try:
                page.close()
            except Exception:
                pass


def _application_state_check(monitor, application_page, initial_pages, *, mailbox_host=None):
    """Watch this application and tabs it opens, excluding preexisting/mailbox tabs."""
    def check():
        pages = [application_page, *(page for page in monitor.context.pages
                                   if page not in initial_pages and page is not application_page)]
        for page in pages:
            try:
                if page.is_closed():
                    continue
                if mailbox_host and urlparse(page.url).hostname == mailbox_host:
                    continue
                reason = monitor.reason(page)
                if reason:
                    log.info("External job unavailable at %s: %s; stopping this application", page.url, reason)
                    return "RESULT:EXPIRED"
            except PlaywrightError:
                continue
        return None
    return check


def _configured_company_blacklist(settings: dict) -> tuple[str, ...]:
    """Use the same normalized company-name matching as Easy Apply."""
    companies = settings.get("company_blacklist")
    if companies is None:
        return ()
    if not isinstance(companies, list) or any(not isinstance(name, str) for name in companies):
        raise ValueError("company_blacklist must be a list of strings or null")
    return tuple(dict.fromkeys(
        normalized for name in companies if (normalized := _normalize_company_name(name))
    ))


def _application_progress_check(availability_check, verification):
    """Check terminal pages first, then perform the local email-code handoff."""
    def check():
        return availability_check() or verification.check()
    return check


def _non_easy_company_name(detail_page, card=None) -> str:
    """Read modern job-card labels as well as legacy job-detail company names."""
    company = _extract_company_name(card if card is not None else detail_page, detail_page)
    if company:
        return company
    try:
        element = detail_page.query_selector("a.topcard__org-name-link")
        return (element.text_content() or "").strip() if element else ""
    except Exception:
        return ""


def validate_non_easy_config(settings: dict) -> JobboardFilter | None:
    cooldown_days(settings)
    configured_title_exclusion(settings)
    _configured_company_blacklist(settings)
    _configured_max_scan_pages(settings)
    paths = {}
    if not intelligent_enabled(settings):
        paths["resume_path"] = settings.get("resume_path")
        paths.update({f"resume_paths.{lang}": path for lang, path in
                      _configured_language_resume_paths(settings).items() if path})
    if settings.get("photo_path"):
        paths["photo_path"] = settings["photo_path"]
    for label, path in paths.items():
        if not isinstance(path, str) or not path.strip() or not Path(path).is_file():
            raise ValueError(f"{label} is not a file: {path}")
        try:
            with Path(path).open("rb"):
                pass
        except OSError as exc:
            raise ValueError(f"{label} cannot be read: {path}") from exc
    _mailbox_context(settings)
    return load_jobboard_filter(settings)


def _settings_for_job(settings: dict, description: str) -> tuple[dict | None, str]:
    language = _detect_job_language(description)
    if not _job_language_allowed(settings, language):
        return None, language
    resume = settings["resume_path"]
    if not intelligent_enabled(settings):
        resume, _ = _select_resume_path_for_job(settings, description, resume)
    return {**settings, "resume_path": resume}, language


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

    for line in reversed(output.splitlines()):
        line = line.strip().strip('`*"').strip()
        if line == "RESULT:APPLIED (dry run)":
            return "ready"
        match = re.fullmatch(r"RESULT:(APPLIED|READY|EXPIRED|CAPTCHA|LOGIN_ISSUE|FAILED)(?::(.*))?", line)
        if match:
            status, reason = match.groups()
            return f"failed:{reason or 'unknown'}" if status == "FAILED" else status.lower()

    return "failed:no_result_line"


def _read_resume_text(resume_path: Path) -> str:
    """Read plain-text resume content when a sibling text file exists."""
    txt_path = resume_path.with_suffix(".txt")
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8").strip()
    return ""


def _answer_bank_summary(answers: dict) -> str:
    """Preserve every configured answer, including exact questions, false, and zero."""
    return json.dumps(answers, ensure_ascii=False, indent=2)


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


def _build_prompt(job: dict, config_dict: dict, dry_run: bool = False) -> str:
    """Build an autonomous prompt for external non-Easy-Apply sites."""
    from applypilot.apply.prompt import _build_captcha_section

    profile = config_dict.get("profile", {}) or {}
    answers = config_dict.get("answers", {}) or {}
    resume_path = Path(config_dict["resume_path"]).resolve()
    resume_text = (config_dict["_intelligent_resume_text"] if "_intelligent_resume_text" in config_dict
                   else _read_resume_text(resume_path))
    if config_dict.get("_intelligent_pdf_sha256"):
        resume_text += ("\n\nThis résumé was generated for this job. Upload the exact PDF path above; "
                        "do not select a previously saved résumé with the same filename. If the fresh upload "
                        "cannot be completed and verified, output RESULT:FAILED:upload_rejected.")
    photo = str(config_dict.get("photo_path") or "").strip()
    photo_path = str(Path(photo).resolve()) if photo else "Not configured"
    mailbox_url, applicant_email = _mailbox_context(config_dict)
    mailbox_section = """
== EMAIL VERIFICATION ==
No mailbox is configured. If an email code is required, output
RESULT:FAILED:email_2fa_unavailable. Do not repeatedly attempt to continue.
"""
    if mailbox_url:
        mailbox_section = f"""
== EMAIL VERIFICATION ==
Applicant email: {applicant_email}
Mailbox URL: {mailbox_url}
- Only use this inbox when the application visibly requests an email verification
  code, OTP, or one-time passcode. Otherwise ignore the mailbox.
- Use the existing mailbox tab. If it is absent, open the configured inbox URL.
- Check the newest relevant employer/ATS verification message for this application,
  read its code, return to the application tab, and enter the code.
- Greenhouse/Proton verification is handled locally between browser steps.
  FIRST inspect the CURRENT application: if the code inputs are already complete,
  leave them unchanged and continue. Do not request another code or reopen mail.
- Proton renders email bodies inside iframe[data-testid="content-iframe"] with
  title "Email content". The outer article HTML does not include that body.
  Preserve every character and its case; codes may contain letters AND digits.
- A code is NOT a screening answer: never use a placeholder or random answer.
  Code widgets can be one input or a group of one-character inputs; verify their
  actual values and validation state before resubmitting.
- Snapshot refs such as e877/f3e12 are NOT HTML attributes. Never use selectors
  like input[ref="e877"] or treat semantic roles such as heading as HTML tags.
  Use real labels, IDs, input[autocomplete="one-time-code"], or inputs scoped to
  the verification fieldset/group. Use browser typing/fill actions, not direct
  input.value assignment, so React receives normal input events.
- Check at most three times, waiting at most 10 seconds between checks. If the
  inbox requires a login you cannot complete using the existing session, or no
  matching code arrives, output RESULT:FAILED:email_2fa_unavailable.
- Do not send, delete, or modify mail. Do not print verification codes in logs.
- Account registration may continue with the supplied email and available profile
  details. If required credentials are unavailable, output RESULT:LOGIN_ISSUE.
"""

    jobboard_filter = load_jobboard_filter(config_dict)
    jobboard_section = ""
    if jobboard_filter:
        jobboard_section = f"""
== JOB-BOARD FILTER ==
Allowed application hostnames: {json.dumps(jobboard_filter.patterns)}
Only fill, upload documents, or submit on these application hosts. Compare the
actual HTTP(S) URL hostname case-insensitively, not the URL path or query.
A '*' matches a non-empty part of one hostname label, never a dot.
Check the current URL after navigation, redirects, or opening an application tab,
and again before entering personal information, uploading, or submitting. If an
application redirects to an unlisted hostname, do not continue or rediscover it;
output RESULT:FAILED:jobboard_filtered. The separately configured verification
mailbox may still be used only for the EMAIL VERIFICATION process, not as an
application destination.
"""

    profile_summary = json.dumps(profile, ensure_ascii=False, indent=2)
    answer_bank = _answer_bank_summary(answers)
    screening_overrides = _screening_override_summary(answers)
    education_summary = _education_summary(answers)
    prefill_report = json.dumps(config_dict.get("_configured_prefill", []), ensure_ascii=False, indent=2)
    continuation = ""
    if config_dict.get("_answer_fallback_retry"):
        continuation = """
== CONTINUE THE SAME APPLICATION ==
The previous turn reported an unknown/missing question answer. That is NOT a
terminal result. Inspect a fresh snapshot of the CURRENT application and apply
MISSING-ANSWER FALLBACK to the unanswered fields now. Choose real options using
the browser tools. Do not rediscover the job, repeat successful uploads, or stop
because a fact is missing. A valid default is explicitly authorized by the user.
"""
    captcha_section = _build_captcha_section()
    submit_instruction = (
        "Do NOT click the final Submit/Apply button. Review the form, then output RESULT:READY."
        if dry_run
        else "Submit the application after verifying all required fields are correct."
    )

    return f"""You are an autonomous browser agent completing a LinkedIn non-Easy-Apply job application.

This job was discovered on LinkedIn, but it is NOT an Easy Apply job. Do not try to use LinkedIn Easy Apply. Go directly to the external application site and complete the application there.

== JOB ==
LinkedIn URL: {job['linkedin_url']}
External application URL: {job['application_url']}
Title: {job.get('title', 'Unknown')}
Company: {job.get('company', 'Unknown')}

== FILES ==
Resume PDF (upload this): {resume_path}
For every document upload request, upload this same Resume PDF again, even when
the field requests a transcript, cover letter, certificate, or another attachment.
Handle each requested upload separately; do not generate or look for another document.
Photo (only for a photo/image upload field): {photo_path}
If a photo is required but not configured, output RESULT:FAILED:missing_required_photo.
Do not upload the resume into a photo field. Respect file type and size constraints;
if a required upload is rejected, output RESULT:FAILED:upload_rejected.

== APPLICANT PROFILE ==
{profile_summary}

== STANDARD ANSWERS ==
{answer_bank}

== SCREENING OVERRIDES ==
{screening_overrides}

== EDUCATION ==
{education_summary}

== CONFIGURED PREFILL REPORT ==
{prefill_report}
Fields marked filled were set using real browser actions. Preserve
them unless the page shows a validation error or they contradict an explicit
configured override. Fields marked needs_agent or retry_with_agent still need
attention; do not treat a failed prefill as an answered question.
Entries with source fallback.* used the user-requested default policy, not a
verified applicant fact. Preserve valid fallback selections on retries unless
an explicit configured answer takes precedence.

== RESUME TEXT ==
{resume_text or "Not available as text. Use the uploaded resume PDF plus the profile/answer bank above."}
{mailbox_section}
{jobboard_section}

== CORE RULES ==
1. Never pause and wait for a human. Either answer, skip safely, or fail with a clear RESULT code.
   Do not merely describe an action you intend to take: invoke the browser tool in the same turn.
2. Configured profile/answer facts and explicit overrides take precedence. Use the MISSING-ANSWER FALLBACK only when no configured or supported answer is available.
3. For common screening questions not explicitly listed, use equivalent configured answers or profile/CV facts, then the user-requested favorable/negative/random fallback.
4. For open-ended required questions, use concise configured facts where available; otherwise use the type-compatible placeholder from MISSING-ANSWER FALLBACK instead of fabricating a biography.
5. Missing information is never a reason to fail or skip a job. Commit an actual fallback answer for required questions and continue.
6. Never sign in through Google, Microsoft, Okta, Auth0, or any SSO provider. If required, output RESULT:FAILED:sso_required.
7. Never grant camera, microphone, location, screen-sharing, identity-verification, or biometric permissions.
8. Never stop just because the form structure is unfamiliar. Read the page, inspect labels/options, and continue.
9. Never close the browser, browser context, or tabs. ApplyPilot manages tab cleanup after your final RESULT line.

== QUESTION POLICY ==
- FIRST: check SCREENING OVERRIDES. If the visible question text substantially matches an override, use that override answer exactly.
- APPLICANT PROFILE and STANDARD ANSWERS contain the complete configured JSON,
  not just common fields. Look up exact question keys first (ignoring case,
  punctuation, and required-field asterisks), then equivalent wording. Explicit
  false, No, and numeric 0 are real answers, not missing data. Respect lists and
  nested education/experience mappings. Never ignore a configured answer just
  because its key is a full question instead of a short predefined identifier.
- Prefer deterministic answers from the provided profile and answer bank.
- For location / commute / willing-to-work-onsite threshold questions, use answers.onsite when present.
- For visa / sponsorship / work permit / require support questions, use answers.visa_sponsorship when present.
- For authorized-to-work questions, use answers.authorized_to_work when present.
- A question about authorization WITHOUT visa sponsorship is not asking whether
  sponsorship is required. If authorized_to_work is Yes and visa_sponsorship is
  No, the combined answer is Yes. Use an explicit matching question override
  first; never copy visa_sponsorship=No into an authorization-without-sponsorship
  field merely because its text mentions visas.
- For degree and field-of-study questions, use EDUCATION first. Treat that section as the source of truth for bachelor's, master's, highest education, and Computer Science field checks.
- If EDUCATION provides explicit booleans such as computer_science_bachelors, computer_science_masters, or computer_science_bachelors_or_masters, use them directly for matching yes/no questions.
- If EDUCATION provides bachelors_field or masters_field, use those exact fields when asked what subject the degree is in.
- For yes/no tool questions: if the tool clearly matches a skill listed in years_experience with > 0 years, answer Yes.
- For "years of experience" questions: use the matching years_experience value when present.
- For open text such as "Why are you interested?" or "Tell us about yourself": write 2-3 sentences grounded in the visible job description and the provided profile/resume.
{UNKNOWN_QUESTION_POLICY}

== CUSTOM DROPDOWNS AND VALIDATION ==
- Inspect the actual tag. Use browser_select_option only on native <select>
  elements. Greenhouse's input[role="combobox"] controls are React Select
  widgets, not native selects, even when aria-haspopup is "true".
- Match a control by its associated label/accessible name (aria-labelledby also
  supplies a name), or its actual current input ID. Do not guess aria-label
  selectors, use a nonexistent <combobox> tag, or reuse stale snapshot refs.
- For a custom dropdown: click that control, type the configured answer when it
  is searchable, inspect its open listbox, and CLICK the matching visible option.
  Options can be portalled outside the form. Do not use select_option, patch
  input.value with JavaScript, or assume that typing commits the answer.
- Verify the selected-value display and linked validation state after choosing.
  A React Select search input normally becomes empty AFTER a successful choice;
  its .value alone does not tell you whether the question is answered. A selected
  label with aria-invalid=true or a required-field error is not yet verified:
  re-open the control, click the actual option, blur, and check a fresh snapshot.
- For Location (City), use profile.city (the applicant's residence, NOT the job
  location). Wait for a matching city/region/country suggestion and click it.
  Do not click Locate me or request geolocation permission.
- Ignore aria-hidden required mirror inputs, hidden country-search inputs,
  honeypots, and CAPTCHA textareas. Fill the visible labeled control instead.
- Do not press Enter on a closed dropdown: it can submit the entire form before
  other fields are complete. Before final submission, fix visible required-field
  errors, refreshing snapshots after rerenders. Never submit repeatedly without
  changing an unresolved field.

== PROCESS ==
{continuation}
1. Start by listing tabs/windows and select the page matching the external application URL above, allowing redirects to the employer's ATS only when permitted by JOB-BOARD FILTER if enabled. Exclude mailbox tabs, including mailbox login pages. The external application page is expected to already be open from LinkedIn. Use that live page instead of trying to rediscover the job.
2. If no external tab is already open, navigate directly to the external application URL as a fallback.
3. Read the page with browser_snapshot. Use the visible page content/HTML structure to understand the form.
   A 404/410, "This URL does not exist", "Page not found", "Job not found", or
   closed/expired job message is terminal: immediately output RESULT:EXPIRED.
   Do not reload, change the language, guess replacement URLs, click Back to all
   jobs, search other listings, or restart this application to work around it.
4. Upload the resume PDF when asked.
5. Fill all identifiable required fields from the provided profile.
6. Answer screening questions using the question policy above.
7. If the site is not a real job application form, output RESULT:FAILED:not_a_job_application.
8. If you hit a login wall that requires SSO or account creation you cannot complete safely, output RESULT:LOGIN_ISSUE.
9. {submit_instruction}
10. After submit, confirm visible success before outputting RESULT:APPLIED. In a dry run output RESULT:READY instead. Output exactly one final RESULT line.

== RESULT CODES ==
RESULT:APPLIED
RESULT:READY
RESULT:EXPIRED
RESULT:CAPTCHA
RESULT:LOGIN_ISSUE
RESULT:FAILED:reason

{captcha_section}

== GIVE UP CONDITIONS ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Apply MISSING-ANSWER FALLBACK before counting unsuccessful attempts. Missing
  knowledge is not a technical failure or a reason to declare the job stuck.
- Job is closed or no longer accepting applications -> RESULT:EXPIRED
- Broken page / unusable site -> RESULT:FAILED:page_error
"""


def _is_easy_apply(page) -> bool:
    """Return True if the LinkedIn job detail page exposes Easy Apply."""
    selectors = [
        "[data-sdui-screen*='JobDetails'] a[href*='/jobs/view/'][href*='/apply/']",
        "[data-sdui-screen*='jobs.SemanticJobDetails'] a[aria-label*='Easy Apply']",
        "[data-sdui-screen*='jobs.SemanticJobDetails'] a[aria-label='LinkedIn Apply to this job']",
        "main a[href*='/jobs/view/'][href*='/apply/'][href*='openSDUIApplyFlow=true']",
        "a[aria-label='LinkedIn Apply to this job'][href*='currentJobId=']",
        "a[aria-label='LinkedIn Apply to this job']",
        "a[aria-label='Easy Apply to this job']",
        "a[aria-label*='Easy Apply'][href*='/apply/']",
        ".job-details-top-card button:has-text('Easy Apply')",
        "[data-test-id='jobs-details'] button:has-text('Easy Apply')",
        ".jobs-detail__main-content button:has-text('Easy Apply')",
        "button[aria-label='Easy Apply job']",
        "button[aria-label*='Easy Apply']",
        "button:has-text('Easy Apply')",
    ]
    for selector in selectors:
        try:
            for elem in page.query_selector_all(selector):
                if not elem.is_visible():
                    continue
                text = (
                    (elem.text_content() or "")
                    + " "
                    + (elem.get_attribute("aria-label") or "")
                    + " "
                    + (elem.get_attribute("href") or "")
                ).lower()
                if "easy apply" in text or "/apply/" in text or "linkedin apply to this job" in text:
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
                href_lower = urljoin(LINKEDIN_BASE_URL, href).lower()
                if "apply" not in combined and "company website" not in combined:
                    if "apply" not in cls and "apply" not in href_lower:
                        continue
                if "easy apply" in combined or "linkedin apply to this job" in combined or (
                    "linkedin.com" in href_lower
                    and "/jobs/view/" in href_lower
                    and "/apply/" in href_lower
                ):
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
        if _is_external_http_url(popup.url):
            return popup, popup.url
    except Exception:
        pass

    # expect_popup can time out after successful same-tab navigation. Inspect
    # the original click's result instead of clicking the stale control again.
    deadline = time.time() + 8
    while time.time() < deadline:
        for page in list(context.pages):
            if page in before_set:
                continue
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            if _is_external_http_url(page.url):
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
    to_ignore = configured_title_exclusion(config_dict)
    blacklisted_companies = _configured_company_blacklist(config_dict)
    max_applications = int(config_dict.get("max_applications", 20))
    scan_target = max(max_applications * 4, max_applications)
    max_pages = _configured_max_scan_pages(config_dict)

    if not job_title or not location:
        raise ValueError("job_title and location are required")

    config.ensure_dirs()
    profile_dir = config.CHROME_WORKER_DIR / "linkedin-search"
    profile_dir.mkdir(parents=True, exist_ok=True)

    search_url = _search_url(config_dict)

    candidates: list[dict] = []
    found_jobs: list[dict] = []
    seen_urls: set[str] = set()
    stats = {
        "candidate_urls": 0,
        "easy_skipped": 0,
        "company_blacklisted": 0,
        "no_external_url": 0,
        "pages_scanned": 0,
        "stop_reason": "",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(str(profile_dir), headless=headless)
        page = browser.pages[0] if browser.pages else browser.new_page()
        detail_page = browser.new_page()

        try:
            log.info("Searching LinkedIn non-Easy-Apply jobs: %s in %s", job_title, location)
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            _log_active_linkedin_search_state(page, job_title, location)
            if not _ensure_configured_linkedin_search_location(page, location):
                raise RuntimeError("Could not set the configured LinkedIn search location")
            _log_active_linkedin_search_state(page, job_title, location)

            page_num = 1
            scanned_urls: set[str] = set()
            job_cards = _wait_for_result_cards(page)
            while len(candidates) < scan_target and page_num <= max_pages:
                if not job_cards:
                    stats["stop_reason"] = "no_results"
                    break

                page_urls = _result_card_urls(job_cards)
                if not page_urls - scanned_urls:
                    stats["stop_reason"] = "repeated_results_page"
                    break
                scanned_urls.update(page_urls)

                stats["pages_scanned"] = page_num
                log.info(
                    "Scanning LinkedIn results page %d/%d (%d candidates collected)",
                    page_num,
                    max_pages,
                    len(candidates),
                )

                for card in job_cards:
                    href = _job_url_from_result_card(card)
                    if not href or "/jobs/" not in href or href in seen_urls:
                        continue

                    card_title = _extract_job_title_from_card(card) if to_ignore else ""
                    if title_is_ignored(card_title, to_ignore):
                        seen_urls.add(href)
                        log.info("Skipping title '%s': contains to_ignore '%s' (case-insensitive)", card_title, to_ignore)
                        continue
                    title_text = card_title or (card.text_content() or "").strip()
                    if title_keyword and title_keyword not in title_text.lower():
                        continue

                    seen_urls.add(href)
                    candidates.append({"linkedin_url": href, "title": title_text})
                    log.info("Queued candidate %d/%d: %s", len(candidates), scan_target, href)
                    if len(candidates) >= scan_target:
                        break

                if len(candidates) >= scan_target:
                    stats["stop_reason"] = "candidate_limit_reached"
                    break
                if page_num >= max_pages:
                    stats["stop_reason"] = "max_scan_pages_reached"
                    break
                job_cards, reason = _next_results_page(page, page_num, page_urls)
                if reason:
                    stats["stop_reason"] = reason
                    break
                page_num += 1

            log.info("LinkedIn scan stopped: %s; pages=%s/%s; candidates=%s",
                     stats["stop_reason"] or "candidate_limit_reached", stats["pages_scanned"],
                     max_pages, len(candidates))

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

                    company = _non_easy_company_name(detail_page)
                    if blacklisted_companies and not company:
                        from applypilot.linkedin.scrape import parse_job

                        try:
                            company = parse_job(detail_page.content(), candidate["linkedin_url"])["company"]
                        except (ValueError, PlaywrightTimeoutError):
                            pass
                    if _is_blacklisted_company(company, blacklisted_companies):
                        stats["company_blacklisted"] += 1
                        log.info("Skipping blacklisted company '%s': %s", company, candidate["linkedin_url"])
                        continue

                    application_url = _extract_external_apply_url(detail_page)
                    if not application_url:
                        stats["no_external_url"] += 1
                        log.info("No external URL found: %s", candidate["linkedin_url"])
                        continue

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
    """Never silently abandon a job on a model's missing-answer refusal."""
    total_duration = 0
    for attempt in range(2):
        settings = config_dict if attempt == 0 else {**config_dict, "_answer_fallback_retry": True}
        result, duration = _run_external_application_once(provider, model, agent, job, settings, port, dry_run)
        total_duration += duration
        reason = result.removeprefix("failed:").split(":", 1)[0]
        if not result.startswith("failed:") or reason not in {
            "unknown_required_question", "unknown_answer", "missing_answer", "unanswered_question", "unsupported_question",
        }:
            return result, total_duration
        log.warning("Agent reported an unanswered question; applying default policy on the same application (attempt %s/2)",
                    attempt + 1)
    raise AnswerFallbackError("Browser agent did not execute the required answer fallback after a same-job retry; "
                              "stopping rather than silently skipping this job")


def _run_external_application_once(
    provider: str, model: str, agent: QwenMCPAgent | None, job: dict, config_dict: dict, port: int, dry_run: bool,
) -> tuple[str, int]:
    """Run the configured browser agent against a single external application URL."""
    page_check = config_dict.get("_application_state_check")
    if callable(page_check) and (terminal := page_check()):
        return _extract_result(terminal), 0
    jobboard_filter = load_jobboard_filter(config_dict)
    if jobboard_filter:
        if not jobboard_filter.matches(job.get("application_url", "")):
            log.info("Skipping external application: jobboard_filter hostname not in %s", jobboard_filter.path)
            return "failed:jobboard_filtered", 0
        config_dict = {**config_dict, "_jobboard_filter": jobboard_filter}
    if provider == "claude":
        start = time.time()
        prompt = _build_prompt(job, config_dict, dry_run=dry_run)

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
        suffix = "_answer_retry" if config_dict.get("_answer_fallback_retry") else ""
        log_file = config.LOG_DIR / f"linkedin_noneasy_claude_{int(start)}{suffix}.log"
        with open(log_file, "w", encoding="utf-8") as lf:
            assert proc.stdout is not None
            for line in proc.stdout:
                if callable(page_check) and (terminal := page_check()):
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    lf.write(f"[INFO] Deterministic page guard stopped the agent.\n{terminal}\n")
                    return _extract_result(terminal), int((time.time() - start) * 1000)
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
    prompt = _build_prompt(job, config_dict, dry_run=dry_run)

    suffix = "_answer_retry" if config_dict.get("_answer_fallback_retry") else ""
    log_file = config.LOG_DIR / f"linkedin_noneasy_{provider}_{int(start)}{suffix}.log"
    guard = {"stop_check": page_check} if callable(page_check) else {}
    output = agent.run_prompt(prompt, log_path=log_file, **guard)
    status = _extract_result(output)
    duration_ms = int((time.time() - start) * 1000)
    return status, duration_ms


def _search_url(config_dict: dict) -> str:
    return (
        "https://www.linkedin.com/jobs/search-results/"
        f"?keywords={quote(config_dict.get('job_title', ''))}"
        f"&location={quote(config_dict.get('location', ''))}"
    )


def _iter_result_cards(page):
    return page.query_selector_all(
        "a.base-card__full-link, "
        "div.job-card-container a[href*='/jobs/view/'], "
        "[componentkey^='job-card-component-ref-']"
    )


def _job_url_from_result_card(card) -> str:
    """Extract a LinkedIn job URL from classic or semantic-search cards."""
    href = _normalize_linkedin_job_url(card.get_attribute("href") or "")
    if "/jobs/view/" in href:
        return href

    component_key = card.get_attribute("componentkey") or ""
    match = re.search(r"(?:^|-)job-card-component-ref-(\d+)(?:$|[^\d])", component_key)
    if not match:
        return ""
    return f"{LINKEDIN_BASE_URL}/jobs/view/{match.group(1)}/"


def _result_card_urls(cards) -> set[str]:
    return {url for card in cards if (url := _job_url_from_result_card(card))}


def _wait_for_result_cards(page, previous_urls: set[str] | None = None) -> list:
    """Allow up to ten seconds for results, requiring new jobs after pagination."""
    return wait_for_fresh_cards(
        page, read_cards=_iter_result_cards, card_keys=_result_card_urls,
        previous_keys=previous_urls, wait=_safe_wait_for_timeout,
    )


def _next_results_page(page, page_num: int, previous_urls: set[str]) -> tuple[list, str]:
    """Reuse the Easy Apply/scraper paginator, then verify that results changed."""
    return advance_results_page(
        page, page_num, previous_urls, find_next=_find_linkedin_next_page_button,
        click_next=_click_action_button, wait_cards=_wait_for_result_cards, logger=log,
    )


def _run_non_easy_apply_direct(config_dict: dict, model: str = "qwen-flash",
                               headless: bool = False, dry_run: bool = False) -> dict:
    """Process LinkedIn non-Easy-Apply jobs directly from search results."""
    job_title = config_dict.get("job_title", "")
    location = config_dict.get("location", "")
    title_keyword = config_dict.get("title_keyword", "").lower()
    to_ignore = configured_title_exclusion(config_dict)
    blacklisted_companies = _configured_company_blacklist(config_dict)
    max_applications = int(config_dict.get("max_applications", 20))
    max_pages = _configured_max_scan_pages(config_dict)
    max_candidates = max(max_applications * 4, max_applications)

    if not job_title or not location:
        raise ValueError("job_title and location are required")

    jobboard_filter = validate_non_easy_config(config_dict)
    if jobboard_filter:
        log.info("Jobboard filter enabled: %d hostname patterns from %s",
                 len(jobboard_filter.patterns), jobboard_filter.path)
    applied_keys = _load_applied_job_keys()
    history = ApplicationHistory(config_dict)
    preparer = config_dict.get("_intelligent_preparer")

    summary = {
        "found": 0,
        "applied": 0,
        "ready": 0,
        "skipped": 0,
        "failed": 0,
        "search_stats": {
            "candidate_urls": 0,
            "easy_skipped": 0,
            "already_applied": 0,
            "language_skipped": 0,
            "title_ignored": 0,
            "company_blacklisted": 0,
            "company_cooldown": 0,
            "jobboard_filtered": 0,
            "expired": 0,
            "no_external_url": 0,
            "pages_scanned": 0,
            "stop_reason": "",
        },
        "jobs": [],
    }

    port = BASE_CDP_PORT
    chrome_proc = None
    effective_model, provider = get_effective_model_and_provider(
        model, model_override=get_configured_model(config_dict, "browser_model"),
    )
    log.info("External browser agent model: %s (provider=%s)", effective_model, provider)

    try:
        chrome_proc = launch_chrome(0, port=port, headless=headless)
        agent = None if provider == "claude" else QwenMCPAgent(
            model=effective_model,
            mcp_config=_make_mcp_config(port),
        )

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            if not browser.contexts:
                raise RuntimeError("CDP browser has no contexts")
            context = browser.contexts[0]
            inbox_url, _ = _mailbox_context(config_dict)
            inbox_host = urlparse(inbox_url).hostname if inbox_url else None
            search_page = next((pg for pg in context.pages if not pg.is_closed()
                                and (not inbox_host or urlparse(pg.url).hostname != inbox_host)), None)
            if search_page is None:
                search_page = context.new_page()
            detail_page = context.new_page()
            mailbox_page = None
            availability = JobAvailabilityMonitor(context)

            def skip_unavailable(page):
                reason = availability.reason(page)
                if not reason:
                    return False
                summary["skipped"] += 1
                summary["search_stats"]["expired"] += 1
                log.info("Skipping unavailable external job %s: %s", page.url, reason)
                return True

            try:
                mailbox_page = _ensure_mailbox_tab(context, config_dict, (search_page, detail_page))
                search_url = _search_url(config_dict)
                log.info("Searching LinkedIn non-Easy-Apply jobs: %s in %s", job_title, location)
                search_page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                search_page.wait_for_timeout(3000)
                _log_active_linkedin_search_state(search_page, job_title, location)
                if not _ensure_configured_linkedin_search_location(search_page, location):
                    summary["failed"] += 1
                    summary["stopped_reason"] = "location_mismatch"
                    summary["search_stats"]["stop_reason"] = "location_mismatch"
                    return summary
                _log_active_linkedin_search_state(search_page, job_title, location)

                page_num = 1
                seen_urls: set[str] = set()
                scanned_urls: set[str] = set()
                inspected = 0
                cards = _wait_for_result_cards(search_page)

                while page_num <= max_pages and inspected < max_candidates and summary["found"] < max_applications:
                    if not cards:
                        summary["search_stats"]["stop_reason"] = "no_results"
                        break
                    page_urls = _result_card_urls(cards)
                    if not page_urls - scanned_urls:
                        summary["search_stats"]["stop_reason"] = "repeated_results_page"
                        break
                    scanned_urls.update(page_urls)

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

                        linkedin_url = _job_url_from_result_card(card)
                        if not linkedin_url or linkedin_url in seen_urls or "/jobs/view/" not in linkedin_url:
                            continue

                        card_title = _extract_job_title_from_card(card) if to_ignore else ""
                        if title_is_ignored(card_title, to_ignore):
                            seen_urls.add(linkedin_url)
                            summary["skipped"] += 1
                            summary["search_stats"]["title_ignored"] += 1
                            log.info("Skipping title '%s': contains to_ignore '%s' (case-insensitive)", card_title, to_ignore)
                            continue
                        title_text = card_title or (card.text_content() or "").strip()
                        if title_keyword and title_keyword not in title_text.lower():
                            continue

                        seen_urls.add(linkedin_url)
                        inspected += 1
                        summary["search_stats"]["candidate_urls"] = inspected
                        log.info("Inspecting LinkedIn job %d/%d: %s", inspected, max_candidates, linkedin_url)

                        if _was_applied({"linkedin_url": linkedin_url}, applied_keys):
                            summary["skipped"] += 1
                            summary["search_stats"]["already_applied"] += 1
                            log.info("Skipping previously applied job: %s", linkedin_url)
                            continue

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

                        company = _non_easy_company_name(detail_page, card) or "Unknown company"
                        if _is_blacklisted_company(company, blacklisted_companies):
                            summary["skipped"] += 1
                            summary["search_stats"]["company_blacklisted"] += 1
                            log.info("Skipping blacklisted company '%s': %s", company, linkedin_url)
                            continue

                        # Parse only the description so English navigation text
                        # does not influence the language filter.
                        from applypilot.linkedin.scrape import parse_job, read_current_job

                        description = ""
                        details = {}
                        try:
                            details = (read_current_job(detail_page, linkedin_url) if preparer else
                                       parse_job(detail_page.content(), linkedin_url))
                            description = details["description"]
                            title_text = details["title"] or title_text
                            company = details["company"] or company
                        except (ValueError, PlaywrightTimeoutError) as exc:
                            log.warning("Could not parse job description: %s", exc)
                            if preparer:
                                summary["failed"] += 1
                                continue

                        if _is_blacklisted_company(company, blacklisted_companies):
                            summary["skipped"] += 1
                            summary["search_stats"]["company_blacklisted"] += 1
                            log.info("Skipping blacklisted company '%s': %s", company, linkedin_url)
                            continue

                        job = {
                            "linkedin_url": linkedin_url,
                            "title": title_text or "Unknown title",
                            "company": company,
                            "company_url": details.get("company_url", ""),
                        }
                        filter_title = details.get("title") or card_title
                        if title_is_ignored(filter_title, to_ignore):
                            summary["skipped"] += 1
                            summary["search_stats"]["title_ignored"] += 1
                            log.info("Skipping title '%s': contains to_ignore '%s' (case-insensitive)", filter_title, to_ignore)
                            continue
                        if _was_applied(job, applied_keys):
                            summary["skipped"] += 1
                            summary["search_stats"]["already_applied"] += 1
                            log.info("Skipping previously applied job: %s", linkedin_url)
                            continue
                        job_settings, language = _settings_for_job(config_dict, description)
                        if job_settings is None:
                            summary["skipped"] += 1
                            summary["search_stats"]["language_skipped"] += 1
                            log.info("Skipping %s: only_english enabled, language=%s", linkedin_url, language or "unclear")
                            continue
                        log.info("Job language=%s; CV=%s; photo=%s", language or "unclear",
                                 "pending intelligent preparation" if preparer else job_settings["resume_path"],
                                 job_settings.get("photo_path") or "not configured")

                        application_attempt = history.attempt(job, "linkedin-noneasy", dry_run=dry_run)
                        if not application_attempt.allowed:
                            summary["skipped"] += 1
                            summary["search_stats"]["company_cooldown"] += 1
                            application_attempt.close()
                            continue
                        prepared = None
                        outcome = "failed"
                        try:
                            # Open and inspect before any paid résumé generation,
                            # whether or not the hostname allowlist is enabled.
                            application_page, application_url = _open_external_application_page(detail_page)
                            if not application_page:
                                summary["skipped"] += 1
                                summary["search_stats"]["no_external_url"] += 1
                                outcome = "skipped"
                                log.info("No external application page opened after Apply click: %s", linkedin_url)
                                continue
                            application_url = application_page.url
                            if jobboard_filter:
                                if not jobboard_filter.matches(application_url):
                                    summary["skipped"] += 1
                                    summary["search_stats"]["jobboard_filtered"] += 1
                                    outcome = "skipped"
                                    log.info("Skipping external site %s: jobboard_filter hostname not in %s",
                                             application_url, jobboard_filter.path)
                                    continue
                                job_settings = {**job_settings, "_jobboard_filter": jobboard_filter}
                            if skip_unavailable(application_page):
                                outcome = "skipped"
                                continue
                            if preparer:
                                prepared = preparer.prepare({**details, **job})
                                prepared.verify_published()
                                job_settings = {**job_settings, "resume_path": str(prepared.published_pdf),
                                                "_intelligent_resume_text": prepared.text,
                                                "_intelligent_pdf_sha256": prepared.pdf_sha256}
                            if skip_unavailable(application_page):
                                outcome = "skipped"
                                continue

                            prefill_report = prefill_greenhouse_application(application_page, job_settings)
                            if prefill_report:
                                job_settings = {**job_settings, "_configured_prefill": prefill_report}

                            if jobboard_filter:
                                # The page can redirect again while the job-specific résumé is prepared.
                                application_url = application_page.url
                                if not jobboard_filter.matches(application_url):
                                    summary["skipped"] += 1
                                    summary["search_stats"]["jobboard_filtered"] += 1
                                    outcome = "skipped"
                                    log.info("Skipping redirected site %s: jobboard_filter hostname not in %s",
                                             application_url, jobboard_filter.path)
                                    continue
                                log.info("Jobboard filter matched application host: %s", urlparse(application_url).hostname)

                            if skip_unavailable(application_page):
                                outcome = "skipped"
                                continue
                            availability_check = _application_state_check(
                                availability, application_page, set(context.pages), mailbox_host=inbox_host,
                            )
                            verification = EmailVerificationHandler(
                                application_page, mailbox_page, job_settings, job["company"],
                            )
                            job_settings = {**job_settings, "_application_state_check": _application_progress_check(
                                availability_check, verification,
                            )}
                            job["application_url"] = application_url or ""
                            summary["jobs"].append(job)
                            summary["found"] += 1
                            log.info("Opened external application page: %s", application_url or "<unknown>")
                            result, _duration_ms = _run_external_application(
                                provider=provider,
                                model=effective_model,
                                agent=agent,
                                job=job,
                                config_dict=job_settings,
                                port=port,
                                dry_run=dry_run,
                            )
                            if result == "expired":
                                summary["found"] -= 1
                                summary["jobs"].pop()
                                summary["skipped"] += 1
                                summary["search_stats"]["expired"] += 1
                                outcome = "skipped"
                                log.info("Application skipped: external job is missing, closed, or expired")
                            elif jobboard_filter and result == "failed:jobboard_filtered":
                                summary["found"] -= 1
                                summary["jobs"].pop()
                                summary["skipped"] += 1
                                summary["search_stats"]["jobboard_filtered"] += 1
                                outcome = "skipped"
                                log.info("Application skipped after redirect: jobboard_filter reported an unlisted host")
                            elif dry_run and result in {"ready", "applied"}:
                                summary["ready"] += 1
                                outcome = "ready"
                            elif not dry_run and result == "applied":
                                summary["applied"] += 1
                                outcome = "applied"
                                applied_keys.add(_job_registry_key(job))
                                try:
                                    application_attempt.success(job)
                                    _record_applied_job(job)
                                except (OSError, HistoryError):
                                    log.exception("Application submitted but history could not be saved; stopping to prevent duplicates")
                                    summary["stopped_reason"] = "history_write_failed"
                                    return summary
                            else:
                                summary["failed"] += 1
                                log.info("Non-easy apply failed for %s: %s", application_url or linkedin_url, result)
                                if result == "failed:provider_credit_low":
                                    summary["stopped_reason"] = "provider_credit_low"
                                    return summary
                        except AnswerFallbackError as exc:
                            outcome = "stopped"
                            summary["stopped_reason"] = "answer_fallback_not_executed"
                            log.error("%s", exc)
                            return summary
                        except PreparationError as exc:
                            log.error("%s", exc)
                            summary["failed"] += 1
                            if exc.fatal:
                                summary["stopped_reason"] = f"intelligent_{exc.stage}_failed"
                                return summary
                        except Exception:
                            if not preparer:
                                raise
                            log.exception("Application failed after intelligent preparation: %s", linkedin_url)
                            summary["failed"] += 1
                        finally:
                            application_attempt.close()
                            try:
                                if prepared:
                                    prepared.finish(outcome)
                            finally:
                                _close_application_tabs(context, (search_page, detail_page, mailbox_page))

                    if summary["found"] >= max_applications:
                        summary["search_stats"]["stop_reason"] = "max_applications_reached"
                        break
                    if inspected >= max_candidates:
                        summary["search_stats"]["stop_reason"] = "candidate_limit_reached"
                        break
                    if page_num >= max_pages:
                        summary["search_stats"]["stop_reason"] = "max_scan_pages_reached"
                        break
                    cards, reason = _next_results_page(search_page, page_num, page_urls)
                    if reason:
                        summary["search_stats"]["stop_reason"] = reason
                        break
                    page_num += 1

                log.info("LinkedIn scan stopped: %s; pages=%s/%s; candidates=%s",
                         summary["search_stats"]["stop_reason"] or "max_applications_reached",
                         summary["search_stats"]["pages_scanned"], max_pages, inspected)
            finally:
                availability.close()
                _close_application_tabs(context, (search_page, detail_page))
                try:
                    detail_page.close()
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


@intelligent_session
def run_non_easy_apply(config_dict: dict, model: str = "qwen-flash",
                       headless: bool = False, dry_run: bool = False) -> dict:
    """Backward-compatible shim."""
    return _run_non_easy_apply_direct(config_dict, model=model, headless=headless, dry_run=dry_run)
