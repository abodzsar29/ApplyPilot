"""LinkedIn jobs search using Playwright."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from applypilot import config as app_config

log = logging.getLogger(__name__)


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
    search_url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={quote(job_title)}"
        f"&location={quote(location)}"
        f"&f_AL=true"  # Easy Apply filter
    )

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
        )
        page = browser.new_page()

        try:
            # Navigate to search page
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

            # Wait extra time for LinkedIn to render the job list
            page.wait_for_timeout(3000)

            # Try to wait for job cards with fallback selectors
            job_cards = []
            selectors = [
                "ul.jobs-search__results-list li a.base-card__full-link",
                "a.base-card__full-link",
                "div.job-card-container a",
                "div[data-job-id] a",
            ]

            for selector in selectors:
                try:
                    page.wait_for_selector(selector, timeout=5000)
                    job_cards = page.query_selector_all(selector)
                    if job_cards:
                        log.info(f"Found {len(job_cards)} job cards using selector: {selector}")
                        break
                except Exception as e:
                    log.debug(f"Selector '{selector}' not found: {e}")
                    continue

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

                    # Try the most generic selector
                    job_cards = page.query_selector_all("a.base-card__full-link")
                    log.info(f"Found {len(job_cards)} cards after scrolling")

                log.info(f"Processing {len(job_cards)} job cards on page {page_num}...")
                for idx, card in enumerate(job_cards):
                    try:
                        url = card.get_attribute("href")
                        if not url:
                            log.info(f"Card {idx}: No href attribute")
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
                        for selector in ["div.job-card-container a", "a.base-card__full-link"]:
                            job_cards = page.query_selector_all(selector)
                            if job_cards:
                                log.info(f"Found {len(job_cards)} cards on next page using selector: {selector}")
                                break
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
