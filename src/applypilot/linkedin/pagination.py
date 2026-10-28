"""Shared bounded pagination checks for LinkedIn's asynchronous search results."""

import re

from playwright.sync_api import Error as PlaywrightError


def configured_max_scan_pages(settings: dict, *, default: int | None = 3) -> int | None:
    """Accept positive JSON integers/digit strings; preserve each caller's default."""
    if "max_scan_pages" not in settings:
        return default
    value = settings["max_scan_pages"]
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value).strip()):
        raise ValueError("max_scan_pages must be a positive integer")
    return int(value)


def wait_for_fresh_cards(page, *, read_cards, card_keys, previous_keys=None, wait=None) -> list:
    """Poll for up to ten seconds, ignoring empty/stale results during navigation."""
    for attempt in range(40):
        try:
            cards = read_cards(page)
            keys = card_keys(cards)
            if keys and (previous_keys is None or keys - previous_keys):
                return cards
        except PlaywrightError:
            pass  # LinkedIn can detach the old nodes before rendering the next page.
        if attempt < 39:
            try:
                if wait is None:
                    page.wait_for_timeout(250)
                elif not wait(page, 250):
                    break
            except PlaywrightError:
                break
    return []


def advance_results_page(page, page_num: int, previous_keys: set[str], *,
                         find_next, click_next, wait_cards, logger) -> tuple[list, str]:
    """Click Next once, then verify new job identities rather than assuming success."""
    button = find_next(page)
    if not button:
        logger.info("No visible enabled LinkedIn next-page control after page %s", page_num)
        return [], "no_next_page"
    logger.info("Going to LinkedIn results page %s", page_num + 1)
    if not click_next(button):
        logger.warning("LinkedIn next-page control could not be clicked after page %s", page_num)
        return [], "pagination_click_failed"
    cards = wait_cards(page, previous_keys)
    if not cards:
        logger.warning(
            "LinkedIn pagination timed out: no new job cards within 10 seconds after page %s; "
            "this does not confirm that the remaining results are exhausted", page_num,
        )
        return [], "pagination_no_new_results"
    return cards, ""
