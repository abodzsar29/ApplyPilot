"""LinkedIn jobs search using Playwright."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright

from applypilot import config as app_config

log = logging.getLogger(__name__)


def _extract_job_id_from_componentkey(value: str | None) -> str:
    match = re.search(r"job-card-component-ref-(\d+)", value or "")
    return match.group(1) if match else ""


def _normalize_linkedin_job_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        return f"https://www.linkedin.com{url}"
    return url


def _extract_job_url_from_card(card) -> str:
    url = _normalize_linkedin_job_url(card.get_attribute("href") or "")
    if url and "/jobs/" in url:
        return url

    link = card.query_selector("a[href*='/jobs/view/']")
    if link:
        url = _normalize_linkedin_job_url(link.get_attribute("href") or "")
        if url:
            return url

    job_id = _extract_job_id_from_componentkey(card.get_attribute("componentkey"))
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}/"

    child = card.query_selector("[componentkey^='job-card-component-ref-']")
    if child:
        job_id = _extract_job_id_from_componentkey(child.get_attribute("componentkey"))
        if job_id:
            return f"https://www.linkedin.com/jobs/view/{job_id}/"

    return ""


def _find_linkedin_job_cards(page) -> list:
    selectors = [
        "[componentkey^='job-card-component-ref-']",
        "ul.jobs-search__results-list li a.base-card__full-link",
        "a.base-card__full-link",
        "div.job-card-container",
        "div[data-job-id] a",
    ]
    cards = []
    seen = set()

    for selector in selectors:
        try:
            for card in page.query_selector_all(selector):
                url = _extract_job_url_from_card(card)
                component_key = card.get_attribute("componentkey") or ""
                job_id = _extract_job_id_from_componentkey(component_key)
                key = job_id or url or f"{selector}:{id(card)}"
                if key in seen:
                    continue
                seen.add(key)
                cards.append(card)
        except Exception as e:
            log.debug(f"Selector '{selector}' not found: {e}")

    return cards


def _browser_launch_options(config: dict) -> dict:
    options = {}

    browser_name = str(config.get("browser", "") or "").strip().lower()
    if browser_name == "brave":
        brave_path = app_config.get_brave_path()
        log.info("Using Brave browser executable: %s", brave_path)
        options["executable_path"] = brave_path
    elif browser_name == "chrome":
        chrome_path = app_config.get_chrome_path()
        log.info("Using Chrome browser executable: %s", chrome_path)
        options["executable_path"] = chrome_path

    for config_key, option_key in (
        ("locale", "locale"),
        ("timezone_id", "timezone_id"),
        ("user_agent", "user_agent"),
        ("device_scale_factor", "device_scale_factor"),
        ("geolocation", "geolocation"),
        ("permissions", "permissions"),
        ("viewport", "viewport"),
    ):
        value = config.get(config_key)
        if value not in (None, "", [], {}):
            options[option_key] = value

    headers = dict(config.get("extra_http_headers") or {})
    accept_language = config.get("accept_language")
    if accept_language:
        headers["Accept-Language"] = accept_language
    if headers:
        options["extra_http_headers"] = headers

    return options


def _build_linkedin_search_url(config: dict) -> str:
    explicit_url = str(config.get("search_url", "") or "").strip()
    job_title = str(config.get("job_title", "") or "").strip()
    location = str(config.get("location", "") or "").strip()
    if explicit_url and config.get("old_url") is True:
        parsed = urlsplit(explicit_url)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        replaced_keywords = False
        updated_query_items = []
        for key, value in query_items:
            if key == "keywords":
                updated_query_items.append((key, job_title))
                replaced_keywords = True
            else:
                updated_query_items.append((key, value))
        if not replaced_keywords:
            updated_query_items.append(("keywords", job_title))
        return urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(updated_query_items, quote_via=quote),
            parsed.fragment,
        ))

    stale_location_params = {
        "currentJobId",
        "distance",
        "geoId",
        "locationId",
        "origin",
        "refresh",
        "spellCorrectionEnabled",
    }

    if explicit_url:
        parsed = urlsplit(explicit_url)
        path = parsed.path.rstrip("/") or "/jobs/search"
        if not path.startswith("/jobs/search"):
            path = "/jobs/search-results"
        url_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        url_params = {
            key: value
            for key, value in url_params.items()
            if key not in stale_location_params and key not in {"keywords", "location", "f_AL"}
        }
    else:
        use_search_results = config.get("use_search_results_url") is True
        path = "/jobs/search-results" if use_search_results else "/jobs/search"
        url_params = {}

    params = url_params | {
        "keywords": job_title,
        "location": location,
        "f_AL": "true",
    }
    params.update(config.get("search_params") or {})
    params.pop("currentJobId", None)
    return f"https://www.linkedin.com{path}/?{urlencode(params)}"


def get_linkedin_jobs(config: dict, headless: bool = False) -> list[str]:
    """Search LinkedIn jobs and return Easy Apply job URLs.

    Args:
        config: Config dict with keys:
            - job_title: Search query (e.g. "Software Engineer")
            - location: Location string (e.g. "London")
            - title_keyword: Filter jobs by this keyword in title (e.g. "C++")
            - max_applications: Max URLs to return

        headless: If True, run browser in headless mode. If False, show browser window.

    Returns:
        List of job URLs (up to max_applications).
    """
    job_title = config.get("job_title", "")
    location = config.get("location", "")
    title_keyword = config.get("title_keyword", "").lower()
    max_applications = config.get("max_applications", 20)

    if not job_title or not location:
        log.error("job_title and location are required in config")
        return []

    # Build search URL with Easy Apply filter (f_AL=true)
    search_url = _build_linkedin_search_url(config)

    log.info(f"Searching LinkedIn: {job_title} in {location}")
    log.info(f"Search URL: {search_url}")

    job_urls = []

    # Ensure chrome-workers directory exists
    app_config.ensure_dirs()

    # Use persistent context to save login session across runs
    profile_dir = app_config.CHROME_WORKER_DIR / "linkedin-search"
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        # Launch Chromium with persistent user data directory for saved cookies/session
        browser = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            **_browser_launch_options(config),
        )
        page = browser.new_page()

        try:
            # Navigate to search page
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

            # Wait extra time for LinkedIn to render the job list
            page.wait_for_timeout(3000)

            job_cards = _find_linkedin_job_cards(page)
            log.info(f"Found {len(job_cards)} job cards")

            # Paginate through all job results
            page_num = 1
            while len(job_urls) < max_applications:
                log.info(f"Processing page {page_num}...")

                if not job_cards:
                    log.warning(f"No job cards found on page {page_num}")
                    # Try scrolling and re-querying as a last resort
                    for _ in range(3):
                        page.evaluate("window.scrollBy(0, window.innerHeight)")
                        page.wait_for_timeout(1000)

                    # Try the supported selectors again after scrolling.
                    job_cards = _find_linkedin_job_cards(page)
                    log.info(f"Found {len(job_cards)} cards after scrolling")

                log.info(f"Processing {len(job_cards)} job cards on page {page_num}...")
                for idx, card in enumerate(job_cards):
                    try:
                        url = _extract_job_url_from_card(card)
                        if not url:
                            log.info(f"Card {idx}: No job URL")
                            continue

                        # Ensure it's a LinkedIn jobs URL (more lenient check)
                        if "/jobs/" not in url:
                            log.info(f"Card {idx}: Skipping non-job URL")
                            continue

                        # Extract and check job title from the card
                        # Try multiple selectors to find the title
                        title_elem = None
                        title_selectors = ["h3", "h2", ".job-title", "[data-job-title]", "span.job-title"]

                        title_text = None
                        for title_selector in title_selectors:
                            title_elem = card.query_selector(title_selector)
                            if title_elem:
                                title_text = title_elem.text_content() or ""
                                log.info(f"Card {idx}: Found title with selector '{title_selector}': {title_text}")
                                break

                        # Filter by keyword if specified
                        if title_keyword:
                            if not title_text:
                                log.info(f"Card {idx}: Could not extract title, skipping (keyword filter active)")
                                continue
                            title_text_lower = title_text.lower()
                            if title_keyword not in title_text_lower:
                                log.info(f"Card {idx}: Skipping (keyword '{title_keyword}' not in '{title_text}')")
                                continue

                        # Accept job (with or without title)
                        log.info(f"Found: {title_text or 'Unknown title'} -> {url}")
                        job_urls.append(url)

                        if len(job_urls) >= max_applications:
                            log.info(f"Reached max_applications ({max_applications}), stopping search")
                            break

                    except Exception as e:
                        log.info(f"Error processing card {idx}: {e}")
                        import traceback
                        traceback.print_exc()
                        continue

                # Check if we've reached max applications
                if len(job_urls) >= max_applications:
                    break

                # Try to go to next page
                log.info(f"Looking for next page button...")
                next_buttons = page.query_selector_all("button[aria-label*='next'], a[aria-label*='next'], button:has-text('Next')")
                if next_buttons:
                    try:
                        next_buttons[0].click()
                        page.wait_for_timeout(2000)  # Wait for next page to load
                        job_cards = []
                        job_cards = _find_linkedin_job_cards(page)
                        log.info(f"Found {len(job_cards)} cards on next page")
                        page_num += 1
                    except Exception as e:
                        log.info(f"Could not navigate to next page: {e}")
                        break
                else:
                    log.info(f"No next page button found, stopping pagination")
                    break

            log.info(f"Collected {len(job_urls)} LinkedIn Easy Apply jobs")

        except Exception as e:
            log.error(f"Error searching LinkedIn: {e}")
            import traceback
            traceback.print_exc()

        finally:
            page.close()
            browser.close()

    return job_urls
