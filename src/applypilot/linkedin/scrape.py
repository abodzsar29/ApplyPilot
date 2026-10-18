"""Read LinkedIn job descriptions using the existing saved browser session."""

import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.linkedin.unified_apply import (
    _apply_exact_location_filter,
    _browser_launch_options,
    _build_linkedin_search_url,
    _click_action_button,
    _clean_linkedin_card_text,
    _detect_job_language,
    _ensure_configured_linkedin_search_location,
    _extract_job_title_from_card,
    _extract_job_url_from_card,
    _find_linkedin_next_page_button,
    _hold_setup_session_open,
    _job_language_allowed,
    _looks_like_linkedin_job_title,
)

log = logging.getLogger(__name__)
CARD_SELECTOR = "[componentkey^='job-card-component-ref-'], div.job-card-container"


def title_matches(title: str, query: str) -> bool:
    """Require every query word, in any order; AI must not match 'retail'."""
    def words(value):
        normalized = unicodedata.normalize("NFKC", value).casefold()
        # Retain meaningful technical suffixes: C++, C#, .NET.
        return set(re.findall(r"\.?[^\W_]+(?:\+\+|#)?", normalized))

    required = words(query)
    return bool(required) and required <= words(title)


def validate_config(settings: dict) -> int:
    if not isinstance(settings, dict):
        raise ValueError("Config must be a JSON object")
    for key in ("job_title", "location"):
        if not isinstance(settings.get(key), str) or not settings[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if not title_matches(settings["job_title"], settings["job_title"]):
        raise ValueError("job_title must contain at least one word")
    limit = settings.get("max_applications", settings.get("max_application", 20))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("max_applications (or max_application) must be a non-negative integer")
    if "max_applications" in settings and "max_application" in settings:
        if settings["max_application"] != limit:
            raise ValueError("max_application and max_applications disagree; use only one")
    return limit


def build_search_url(settings: dict) -> str:
    """Reuse configured search filters but search all jobs with authoritative keywords."""
    parsed = urlsplit(_build_linkedin_search_url(settings))
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("f_AL", "currentJobId", "start"):
        params.pop(key, None)
    params["keywords"] = settings["job_title"].strip()
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))


def _job_id(url: str) -> str:
    match = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)(?:/|\?|$)", url)
    return match.group(1) if match else ""


def _text(element) -> str:
    """Keep paragraph/list breaks without splitting inline spans into separate lines."""
    if element is None:
        return ""
    clone = BeautifulSoup(str(element), "html.parser")
    for node in clone.select("button, script, style"):
        node.decompose()
    for node in clone.select("br"):
        node.replace_with("\n")
    for node in clone.select("p, li, h1, h2, h3, ul, ol"):
        node.insert_before("\n")
        node.insert_after("\n")
    value = clone.get_text().replace("\xa0", " ")
    return "\n".join(line for raw in value.splitlines() if (line := " ".join(raw.split())))


def _job_header(soup, about):
    """Find the primary top card, excluding sticky titles and recommended jobs."""
    panel = None
    if about is not None:
        panel = about.find_parent(attrs={"data-testid": "lazy-column"})
        if panel is None:
            panel = about.find_parent("section", attrs={"aria-label": "Primary content"})
    panel = panel or soup.select_one("main") or soup
    header = panel.select_one(".job-details-jobs-unified-top-card, .top-card-layout")
    if header is not None:
        return header
    seed = panel.select_one("a[href*='/company/'], h1, [data-testid='job-title']")
    if seed is None:
        seed = panel.select_one("p:has(a[href*='/jobs/view/'])")
    if seed is None:
        return None
    header = seed
    for parent in seed.parents:
        if parent.name in {"body", "html", "[document]"}:
            break
        if parent.select_one("[id^='JobDetails_AboutTheJob_'], [componentkey^='JobDetails_AboutTheJob_'], #job-details"):
            break
        header = parent
        if parent.select_one("[aria-label*='Apply'], .jobs-apply-button, .top-card-layout__cta"):
            break
    return header


def _is_title_text(value: str) -> bool:
    # Same-job links are also used for salary, workplace, and employment badges.
    return _looks_like_linkedin_job_title(value) and value.casefold() not in {
        "hybrid", "remote", "on-site", "onsite", "full-time", "part-time", "contract",
        "internship", "save", "apply", "i’m interested", "i'm interested",
    }


def _header_title(header, company: str) -> str:
    if header is None:
        return ""
    for selector in ("h1, [data-testid='job-title'], .job-details-jobs-unified-top-card__job-title", "p"):
        for node in header.select(selector):
            text = _clean_linkedin_card_text(_text(node))
            if text != company and "·" not in text and _is_title_text(text):
                return text
    return ""


def parse_job(html: str, job_url: str) -> dict:
    """Extract only this job's detail panel, never the company description as the job."""
    soup = BeautifulSoup(html, "html.parser")
    job_id = _job_id(job_url)
    if not job_id:
        raise ValueError(f"Invalid LinkedIn job URL: {job_url}")
    about = soup.select_one(f"[id='JobDetails_AboutTheJob_{job_id}']")
    if about is None:
        about = soup.select_one(f"[componentkey='JobDetails_AboutTheJob_{job_id}']")
    if about is None and soup.select_one("[id^='JobDetails_AboutTheJob_'], [componentkey^='JobDetails_AboutTheJob_']"):
        raise ValueError(f"The displayed description does not belong to job {job_id}")
    description = about.select_one("[data-testid='expandable-text-box']") if about else None
    if description is None:
        description = about or soup.select_one(
            ".jobs-description__content .jobs-box__html-content, "
            "#job-details, .show-more-less-html__markup, .jobs-description-content__text"
        )
    description_text = _text(description)
    if description_text.startswith("About the job\n"):
        description_text = description_text.removeprefix("About the job\n")
    if not description_text or description_text == "About the job":
        raise ValueError(f"No job description found for {job_id}")

    header = _job_header(soup, about)
    if header is None:
        raise ValueError(f"No job header found for {job_id}")
    scope = header
    company_node = next((node for node in scope.select("a[href*='/company/'], .topcard__org-name-link")
                         if _text(node)), None)
    title = _header_title(header, _text(company_node))
    if not title:
        raise ValueError(f"No job title found for {job_id}")
    company_section = soup.select_one(f"[id='JobDetails_AboutTheCompany_{job_id}']")
    company_description = company_section.select_one("[data-testid='expandable-text-box']") if company_section else None
    metadata = []
    for node in scope.select("p, .job-details-jobs-unified-top-card__primary-description-container, .topcard__flavor--bullet"):
        value = _text(node)
        if value and value != title and value != _text(company_node):
            metadata.append(value)
    location = posted = applicants = ""
    for line in metadata:
        if "·" in line:
            pieces = [part.strip() for part in line.split("·")]
            location = pieces[0]
            for part in pieces[1:]:
                if re.search(r"\b(ago|reposted|posted)\b", part, re.I):
                    posted = part
                if re.search(r"\b(applicants?|people|clicked)\b", part, re.I):
                    applicants = part
            break
    if not location:
        node = scope.select_one(".topcard__flavor--bullet, .job-details-jobs-unified-top-card__tertiary-description-container")
        location = _text(node)
    badges = []
    for node in scope.select("a[href*='currentJobId='], a[href*='/jobs/view/'], .job-details-jobs-unified-top-card__job-insight"):
        value = _text(node)
        if value and value != title and value not in badges:
            badges.append(value)
    apply_node = scope.select_one("a[aria-label*='Apply'], a[href*='/safety/go/'], a.topcard__link")
    apply_url = urljoin(job_url, apply_node.get("href", "")) if apply_node else ""
    if "/safety/go/" in apply_url:
        apply_url = dict(parse_qsl(urlsplit(apply_url).query)).get("url", apply_url)
    return {
        "job_id": job_id,
        "title": title,
        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "company": _text(company_node),
        "company_url": urljoin(job_url, company_node.get("href", "")) if company_node else "",
        "location": location,
        "posted": posted,
        "applicants": applicants,
        "job_type": next((v for v in badges if re.search(r"full.time|part.time|contract|internship", v, re.I)), ""),
        "workplace_type": next((v for v in badges if re.search(r"hybrid|remote|on.site", v, re.I)), ""),
        "badges": badges,
        "apply_url": apply_url,
        "metadata": metadata,
        "job_card_text": _text(header),
        "description": description_text,
        "company_description": _text(company_description),
        "company_details": _text(company_section),
        "language": _detect_job_language(description_text),
        "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _collect_candidates(page) -> list[dict]:
    candidates = {}
    for card in page.query_selector_all(CARD_SELECTOR):
        job_id = _job_id(_extract_job_url_from_card(card))
        if job_id:
            candidates[job_id] = {"job_id": job_id, "title": _extract_job_title_from_card(card)}
    # Covers the supplied plain paragraph/anchor markup too.
    for link in page.query_selector_all("a[href*='/jobs/view/']"):
        job_id = _job_id(link.get_attribute("href") or "")
        title = " ".join((link.text_content() or "").split())
        if job_id and _is_title_text(title) and not candidates.get(job_id, {}).get("title"):
            candidates[job_id] = {"job_id": job_id, "title": title}
    return list(candidates.values())


def _scroll_results(page) -> bool:
    """Advance results one viewport so virtualized intermediate cards are not skipped."""
    return page.evaluate("""() => {
        const cards = [...document.querySelectorAll(
            "[componentkey^='job-card-component-ref-'], div.job-card-container, a[href*='/jobs/view/']"
        )];
        const containers = new Set();
        for (const card of cards) {
            for (let el = card.parentElement; el; el = el.parentElement) {
                if (el.scrollHeight > el.clientHeight + 2 &&
                    /auto|scroll/.test(getComputedStyle(el).overflowY)) {
                    containers.add(el); break;
                }
            }
        }
        if (!containers.size) containers.add(document.scrollingElement);
        let moved = false;
        for (const el of containers) {
            if (!el) continue;
            const before = el.scrollTop;
            el.scrollTop += Math.max(200, el.clientHeight * 0.8);
            moved = moved || el.scrollTop > before;
        }
        return moved;
    }""")


def iter_candidates(page):
    """Visit lazy-loaded cards and pagination; stop when a page yields no new IDs."""
    seen = set()
    while True:
        page_new = 0
        idle = 0
        while idle < 3:
            fresh = [c for c in _collect_candidates(page) if c["job_id"] not in seen]
            for candidate in fresh:
                seen.add(candidate["job_id"])
                page_new += 1
                yield candidate
            moved = _scroll_results(page)
            idle = 0 if fresh or moved else idle + 1
            page.wait_for_timeout(1000)
        if not page_new:
            return
        next_button = _find_linkedin_next_page_button(page)
        if not next_button:
            return
        if not _click_action_button(next_button):
            raise RuntimeError("LinkedIn next page button could not be clicked")
        page.wait_for_timeout(2500)


def _read_job(page, candidate: dict) -> dict:
    job_id = candidate["job_id"]
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    return read_current_job(page, url)


def read_current_job(page, url: str) -> dict:
    """Read/expand the current listing in place, retaining its Apply controls."""
    job_id = _job_id(url)
    if not job_id:
        raise ValueError("Cannot read job details without a LinkedIn job ID")
    _check_login(page)
    selector = (
        f"[id='JobDetails_AboutTheJob_{job_id}'] [data-testid='expandable-text-box'], "
        f"[componentkey='JobDetails_AboutTheJob_{job_id}'] [data-testid='expandable-text-box'], "
        "#job-details, .show-more-less-html__markup, .jobs-description-content__text"
    )
    section = page.locator(selector).first
    if section.count():
        try:
            section.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            log.debug("Job description not yet visible; waiting for job %s", job_id)
    page.wait_for_function(
        "selector => [...document.querySelectorAll(selector)].some(el => (el.textContent || '').trim())",
        arg=selector, timeout=15000,
    )
    # Full text is usually already in the DOM, but expand when LinkedIn provides a control.
    for control in section.locator("[data-testid='expandable-text-button'], button[aria-expanded='false']").all():
        try:
            if control.is_visible():
                control.click(timeout=2000)
                page.wait_for_timeout(300)
        except Exception:
            log.debug("Could not expand job %s; reading the full description DOM", job_id)
    return parse_job(page.content(), url)


def _check_login(page) -> None:
    if re.search(r"/(login|authwall|checkpoint)(?:/|\?|$)", page.url):
        raise RuntimeError("LinkedIn requires login or verification. Run linkedin-scrape --setup first.")


def output_path(job_title: str, folder: Path) -> Path:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", unicodedata.normalize("NFKC", job_title))
    name = name.strip(" .") or "jobs"
    # Leave room for date/extension on filesystems with a 255-byte component limit.
    name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore")
    return folder / f"{name}_{datetime.now():%Y-%m-%d_%H-%M-%S-%f}.json"


def _save_jobs(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def scrape_candidates(candidates, read_job, settings: dict, path: Path) -> dict:
    """Stop at the saved-job limit or exhausted results, checkpointing each save."""
    limit = validate_config(settings)
    jobs = []
    summary = {"attempted": 0, "saved": 0, "skipped": 0, "failed": 0, "output": str(path.resolve())}
    _save_jobs(path, jobs)
    seen = set()
    candidates = iter(candidates)
    while summary["saved"] < limit:
        try:
            candidate = next(candidates)
        except StopIteration:
            break
        if candidate["job_id"] in seen:
            continue
        seen.add(candidate["job_id"])
        if candidate.get("title") and not title_matches(candidate["title"], settings["job_title"]):
            summary["skipped"] += 1
            log.info("Skipping title mismatch: %s", candidate["title"])
            continue
        summary["attempted"] += 1
        log.info("Scraping attempt %s (saved %s/%s): %s (%s)", summary["attempted"], summary["saved"], limit,
                 candidate.get("title") or "unknown title", candidate["job_id"])
        try:
            job = read_job(candidate)
        except RuntimeError:
            raise
        except Exception as exc:
            log.warning("Could not scrape job %s: %s", candidate["job_id"], exc)
            summary["failed"] += 1
            continue
        if not title_matches(job["title"], settings["job_title"]):
            summary["skipped"] += 1
            log.warning("Job %s detail title does not match %r: %r", candidate["job_id"],
                        settings["job_title"], job["title"])
            continue
        if not _job_language_allowed(settings, job["language"]):
            summary["skipped"] += 1
            log.info("Skipping %s: only_english is enabled; detected language is %s",
                     candidate["job_id"], job["language"] or "unclear")
            continue
        jobs.append(job)
        _save_jobs(path, jobs)
        summary["saved"] = len(jobs)
        log.info("Saved %s/%s jobs to %s", len(jobs), limit, path)
    summary["stopped_reason"] = "limit_reached" if summary["saved"] >= limit else "results_exhausted"
    log.info("Stopped: %s. Attempted %s, saved %s/%s, skipped %s, failed %s",
             summary["stopped_reason"], summary["attempted"], summary["saved"], limit,
             summary["skipped"], summary["failed"])
    return summary


def search_and_scrape(settings: dict, *, headless=False, exact_location=False, setup=False,
                      folder=Path("scrapedjobs")) -> dict:
    limit = validate_config(settings)
    search_url = build_search_url(settings)
    path = output_path(settings["job_title"], Path(folder))
    if limit == 0 and not setup:
        return scrape_candidates([], None, settings, path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            str(config.CHROME_WORKER_DIR / "linkedin-search"),
            headless=headless, **_browser_launch_options(settings),
        )
        try:
            search_page = browser.new_page()
            if setup:
                _hold_setup_session_open(search_page, search_url)
                return {"saved": 0, "skipped": 0, "failed": 0, "output": None}
            search_page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            search_page.wait_for_timeout(3000)
            _check_login(search_page)
            if not _ensure_configured_linkedin_search_location(search_page, settings["location"]):
                raise RuntimeError("Could not set the configured LinkedIn search location")
            if exact_location:
                _apply_exact_location_filter(search_page, settings["location"])
                if not _ensure_configured_linkedin_search_location(search_page, settings["location"]):
                    raise RuntimeError("Could not verify the configured LinkedIn search location")
            detail_page = browser.new_page()
            delay = max(0.0, float(settings.get("delay", 0)))

            def read_job(candidate):
                if delay:
                    detail_page.wait_for_timeout(int(delay * 1000))
                return _read_job(detail_page, candidate)

            log.info("Scraping LinkedIn to %s", path.resolve())
            return scrape_candidates(iter_candidates(search_page), read_job, settings, path)
        finally:
            browser.close()
