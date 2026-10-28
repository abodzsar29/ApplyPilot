"""StepStone search and quick-apply automation."""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from applypilot import config
from applypilot.application_history import ApplicationHistory, HistoryError, page_job_metadata
from applypilot.linkedin.unified_apply import (
    _browser_launch_options,
    _choose_dropdown_value,
    _click_action_button,
    _click_choice_input,
    _element_current_text,
    _extract_field_label,
    _fallback_text_answer,
    _field_requires_answer,
    _fill_text_input,
    _infer_question_answer,
    _lookup_direct_mapping,
    _normalize_lookup_text,
    _radio_group_has_checked_option,
    _repair_required_fields,
    _rewrite_default_a_fields_to_four,
    _select_best_checkbox_option,
    _select_best_radio_option,
    _select_radio_group,
    _scope_text,
    _try_fill_select,
)

log = logging.getLogger(__name__)

STEPSTONE_BASE_URL = "https://www.stepstone.de"

_STEPSTONE_SUBMIT_LABELS = {
    "bewerbung abschicken",
    "bewerbung senden",
    "absenden",
    "submit",
    "send",
}

_STEPSTONE_SUCCESS_TEXTS = (
    "bewerbung wurde versendet",
    "bewerbung erfolgreich",
    "vielen dank fur deine bewerbung",
    "vielen dank für deine bewerbung",
    "application submitted",
    "application sent",
    "thank you for your application",
)


def _stepstone_browser_launch_options(config_dict: dict) -> dict:
    options = _browser_launch_options(config_dict)
    browser_args = list(options.get("args") or [])
    if config_dict.get("disable_http2", True) is not False:
        for arg in ("--disable-http2", "--disable-quic"):
            if arg not in browser_args:
                browser_args.append(arg)
    if browser_args:
        options["args"] = browser_args
    return options


def _slug(value: str) -> str:
    slug = re.sub(r"[^\w]+", "-", value.lower(), flags=re.UNICODE).strip("-_")
    return quote(slug or value.strip().lower())


def build_stepstone_search_url(config_dict: dict) -> str:
    """Build a StepStone search URL with the quick-apply filter enabled."""
    explicit_url = str(config_dict.get("search_url", "") or "").strip()
    if explicit_url:
        separator = "&" if "?" in explicit_url else "?"
        if "am=" in explicit_url or "applicationMethod" in explicit_url:
            return explicit_url
        return f"{explicit_url}{separator}{urlencode({'am': 'INTERNAL'})}"

    job_title = str(config_dict.get("job_title", "") or "").strip()
    location = str(config_dict.get("location", "") or "").strip()
    radius = str(config_dict.get("radius", "30") or "30")
    params = {"radius": radius, "am": "INTERNAL", "searchOrigin": "Resultlist_top-search"}
    return f"{STEPSTONE_BASE_URL}/jobs/{_slug(job_title)}/in-{_slug(location)}?{urlencode(params)}"


def _hold_setup_session_open(page: Page, start_url: str) -> None:
    log.info("Opening StepStone setup session: %s", start_url)
    page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    log.info("StepStone setup session is ready; waiting for manual abort")

    try:
        while True:
            if page.is_closed():
                break
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("StepStone setup session interrupted by user")


def _dismiss_cookie_banner(page: Page) -> None:
    selectors = [
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button[id*='accept']",
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.count() and button.is_visible(timeout=500):
                button.click(timeout=1000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _goto_stepstone_job_page(page: Page, job_url: str, referer: str | None = None) -> bool:
    attempts = [
        ("domcontentloaded", 30000),
        ("commit", 30000),
        ("load", 45000),
    ]
    for index, (wait_until, timeout) in enumerate(attempts, start=1):
        try:
            page.goto(
                job_url,
                wait_until=wait_until,
                timeout=timeout,
                referer=referer or STEPSTONE_BASE_URL,
            )
            page.wait_for_timeout(1500)
            return True
        except PlaywrightError as exc:
            message = str(exc)
            if "ERR_HTTP2_PROTOCOL_ERROR" in message:
                log.warning(
                    "StepStone job navigation hit HTTP/2 protocol error on attempt %d/%d; retrying with wait_until=%s",
                    index,
                    len(attempts),
                    attempts[min(index, len(attempts) - 1)][0],
                )
                page.wait_for_timeout(2000 * index)
                continue
            raise
    log.warning("StepStone job page could not be loaded after HTTP/2 retries: %s", job_url)
    return False


def _fill_search_input(page: Page, selector: str, value: str) -> None:
    field = page.locator(selector).first
    field.wait_for(state="visible", timeout=10000)
    field.click(timeout=2000)
    field.press("Control+A")
    field.fill(value, timeout=3000)
    page.wait_for_timeout(300)


def _open_stepstone_search(page: Page, config_dict: dict) -> None:
    explicit_url = str(config_dict.get("search_url", "") or "").strip()
    if explicit_url:
        url = build_stepstone_search_url(config_dict)
        log.info("Navigating to configured StepStone search URL: %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        _dismiss_cookie_banner(page)
        return

    start_url = str(config_dict.get("start_url", "") or "").strip() or STEPSTONE_BASE_URL
    job_title = str(config_dict.get("job_title", "") or "").strip()
    location = str(config_dict.get("location", "") or "").strip()

    log.info("Opening StepStone homepage: %s", start_url)
    page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    _dismiss_cookie_banner(page)

    try:
        _fill_search_input(page, "input[data-at='searchbar-keyword-input']", job_title)
        _fill_search_input(page, "input[data-at='searchbar-location-input']", location)
        search_button = page.locator(
            "button[data-at='searchbar-search-button'], button[aria-label='Jobs finden']"
        ).first
        if search_button.count() and search_button.is_enabled(timeout=1000):
            search_button.click(timeout=3000)
        else:
            page.locator("input[data-at='searchbar-location-input']").first.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
    except Exception as exc:
        fallback_url = build_stepstone_search_url(config_dict)
        log.warning("Could not drive StepStone search form (%s); falling back to %s", exc, fallback_url)
        page.goto(fallback_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)


def _ensure_quick_apply_filter(page: Page, config_dict: dict) -> None:
    if config_dict.get("skip_quick_apply_filter") is True:
        return

    selectors = [
        "[data-at='applicationMethod-option-schnelle-bewerbung'] [data-at='facet-link']",
        "a[data-at='facet-link']:has-text('Schnelle Bewerbung')",
        "a:has-text('Schnelle Bewerbung')",
    ]
    for selector in selectors:
        try:
            link = page.locator(selector).first
            if not link.count() or not link.is_visible(timeout=1000):
                continue
            href = link.get_attribute("href") or ""
            if "deselected" in href or "am=INTERNAL" in page.url:
                log.info("StepStone quick-apply filter already active")
                return
            log.info("Selecting StepStone quick-apply filter")
            link.click(timeout=3000)
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            return
        except Exception:
            continue

    if "am=INTERNAL" not in page.url and "applicationMethod" not in page.url:
        fallback_url = build_stepstone_search_url(config_dict)
        log.info("Quick-apply filter link not found; navigating to filtered URL: %s", fallback_url)
        page.goto(fallback_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)


def _normalize_stepstone_url(href: str) -> str:
    if not href:
        return ""
    return urljoin(STEPSTONE_BASE_URL, href.split("#", 1)[0])


def _job_url_from_card(card) -> str:
    selectors = [
        "a[data-at='job-item-title']",
        "a[data-testid='job-item-title']",
        "a[href*='stellenangebote']",
        "a[href*='/jobs/']",
        "a",
    ]
    for selector in selectors:
        try:
            link = card.query_selector(selector)
            if not link:
                continue
            url = _normalize_stepstone_url(link.get_attribute("href") or "")
            if "stepstone.de" in url and ("/stellenangebote" in url or "/jobs/" in url):
                return url
        except Exception:
            continue
    return ""


def _find_stepstone_job_urls(page: Page) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    try:
        cards = page.query_selector_all("[data-testid='job-item'], [data-at='job-item']")
    except Exception:
        cards = []

    for card in cards:
        url = _job_url_from_card(card)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _job_title_matches(page: Page, url: str, title_keyword: str) -> bool:
    if not title_keyword:
        return True
    keyword = title_keyword.lower()
    try:
        title = page.locator("[data-at='header-job-title'], h1").first.text_content(timeout=2000) or ""
    except Exception:
        title = url
    return keyword in title.lower()


def _resolve_application_page(page: Page, context, before_pages: set[Page]) -> Page | None:
    new_pages = [candidate for candidate in context.pages if candidate not in before_pages]
    candidates = list(new_pages)
    candidates.extend([page, *context.pages])
    for candidate in candidates:
        try:
            if candidate.is_closed():
                continue
            candidate.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        try:
            if candidate.locator("[data-testid='smart-apply-form-container'], [data-testid='submit-button']").count():
                for stray_page in new_pages:
                    if stray_page is candidate:
                        continue
                    try:
                        if not stray_page.is_closed():
                            log.info("Closing stray StepStone application-start tab: %s", stray_page.url)
                            stray_page.close()
                    except Exception:
                        pass
                return candidate
        except Exception:
            continue
    for stray_page in new_pages:
        try:
            if not stray_page.is_closed():
                log.info("Closing non-application tab opened from StepStone job page: %s", stray_page.url)
                stray_page.close()
        except Exception:
            pass
    return None


def _click_locator_and_get_application_page(locator, page: Page, context) -> Page | None:
    before_pages = set(context.pages)
    popup_page = None
    try:
        with context.expect_page(timeout=3000) as page_info:
            locator.click(timeout=5000)
        popup_page = page_info.value
    except PlaywrightTimeoutError:
        pass
    except Exception:
        try:
            locator.click(timeout=5000)
        except Exception:
            return None

    if popup_page:
        try:
            popup_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        before_pages.discard(popup_page)
    else:
        page.wait_for_timeout(1500)
    return _resolve_application_page(popup_page or page, context, before_pages)


def _click_handle_and_get_application_page(button, page: Page, context) -> Page | None:
    before_pages = set(context.pages)
    if not _click_action_button(button):
        return None
    page.wait_for_timeout(1500)
    return _resolve_application_page(page, context, before_pages)


def _click_application_start_button(page: Page, context) -> Page | None:
    selectors = [
        "button[data-testid='fallback-apply-button']",
        "button[aria-label='Start Application']",
        "[data-at='apply-now-section'] button",
        "[data-at='job-ad-header'] button[data-genesis-element='BUTTON']",
        "a[href*='/bewerbung/']",
        "a[href*='/application/']",
        "a[href*='apply']",
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.count() and button.is_visible(timeout=3000) and button.is_enabled(timeout=1000):
                application_page = _click_locator_and_get_application_page(button, page, context)
                if application_page:
                    return application_page
        except Exception:
            continue

    try:
        buttons = page.query_selector_all("button, a")
    except Exception:
        return None

    blocked_tokens = (
        "speichern",
        "save",
        "menu",
        "teilen",
        "share",
        "jobübereinstimmung",
        "jobubereinstimmung",
        "unternehmensporträt",
        "unternehmensportrat",
    )
    apply_tokens = (
        "application",
        "bewerbung",
        "bewerben",
        "interessiert",
        "interested",
        "apply",
    )
    for button in buttons:
        try:
            if not button.is_visible() or not button.is_enabled():
                continue
            label = button.evaluate(
                "el => [el.getAttribute('aria-label') || '', el.innerText || '', el.textContent || '', el.href || ''].join(' ')"
            )
            normalized = " ".join(str(label or "").lower().split())
            if any(token in normalized for token in blocked_tokens):
                continue
            if not any(token in normalized for token in apply_tokens):
                continue
            application_page = _click_handle_and_get_application_page(button, page, context)
            if application_page:
                return application_page
        except Exception:
            continue
    return None


def _wait_for_application_page(page: Page, context) -> Page:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    for candidate in [page, *context.pages]:
        try:
            if candidate.is_closed():
                continue
            if candidate.locator("[data-testid='smart-apply-form-container'], [data-testid='submit-button']").count():
                return candidate
        except Exception:
            continue
    return page


def _replace_main_cv_if_present(page: Page, resume_path: str) -> None:
    if not resume_path:
        return

    try:
        menu = page.locator("button[data-testid='attachmentMenuDisclosure']").first
        if menu.count() and menu.is_visible(timeout=1500):
            log.info("Replacing existing StepStone CV attachment")
            menu.click(timeout=3000)
            page.wait_for_timeout(500)
            delete_button = page.locator("button[data-testid='deleteFileButton']").first
            if delete_button.count():
                delete_button.click(timeout=3000)
                page.wait_for_timeout(500)
            ok_button = page.locator("button[data-testid='cvDeleteOk']").first
            if ok_button.count():
                ok_button.click(timeout=3000)
                page.wait_for_timeout(1500)
    except Exception as exc:
        log.debug("Could not delete existing StepStone CV: %s", exc)

    try:
        upload_input = page.locator("input[type='file'][data-testid='upload-input']").first
        if upload_input.count():
            upload_input.set_input_files(resume_path)
            page.wait_for_timeout(2000)
            return
    except Exception as exc:
        log.debug("Could not use direct StepStone upload input: %s", exc)

    try:
        upload_button = page.locator(
            "button[data-testid='cvUpload'], button[aria-label='upload button']:has-text('Datei auswählen')"
        ).first
        if upload_button.count() and upload_button.is_visible(timeout=1500):
            with page.expect_file_chooser(timeout=5000) as chooser_info:
                upload_button.click(timeout=3000)
            chooser_info.value.set_files(resume_path)
            page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("Could not upload StepStone CV: %s", exc)


def _stepstone_scope(page: Page):
    scope = page.locator("[data-testid='smart-apply-form-container']").first
    return scope.element_handle(timeout=3000) if scope.count() else page


def _is_empty_stepstone_control(root, input_elem) -> bool:
    try:
        tag_name = input_elem.evaluate("el => el.tagName").lower()
    except Exception:
        tag_name = "unknown"

    try:
        if tag_name == "select":
            value = input_elem.evaluate("el => el.value || ''")
            return not str(value or "").strip()
        field_type = (input_elem.get_attribute("type") or "text").lower()
        if field_type in {"checkbox", "radio"}:
            name = input_elem.get_attribute("name") or ""
            if name:
                return not _radio_group_has_checked_option(root, name)
            return not input_elem.is_checked()
        return not _element_current_text(input_elem).strip()
    except Exception:
        return False


def _is_stepstone_required_field(root, input_elem, label_text: str) -> bool:
    try:
        if input_elem.get_attribute("required") is not None:
            return True
        if (input_elem.get_attribute("aria-required") or "").lower() == "true":
            return True
    except Exception:
        pass

    if _field_requires_answer(input_elem, label_text):
        return True

    try:
        wrapper_text = input_elem.evaluate(
            """el => {
                const fieldId = el.getAttribute('id') || '';
                const wrapper = el.closest('[role="group"], [data-testid*="wrapper"], article, div');
                const label = fieldId ? el.ownerDocument.querySelector(`label[for="${CSS.escape(fieldId)}"]`) : null;
                return [
                    label ? (label.innerText || label.textContent || '') : '',
                    wrapper ? (wrapper.innerText || wrapper.textContent || '') : ''
                ].join('\\n');
            }"""
        )
        if "*" in str(wrapper_text or ""):
            return True
    except Exception:
        pass

    return False


def _is_required_stepstone_wrapper(wrapper) -> bool:
    try:
        label = wrapper.query_selector("label")
        if label and "*" in (label.text_content() or ""):
            return True
        return bool(wrapper.evaluate("el => (el.innerText || el.textContent || '').includes('*')"))
    except Exception:
        return False


def _stepstone_wrapper_label(wrapper) -> str:
    try:
        label = wrapper.query_selector("label")
        if label:
            return " ".join((label.text_content() or "").replace("*", " ").split())
    except Exception:
        pass
    try:
        return " ".join((wrapper.text_content() or "").replace("*", " ").split())
    except Exception:
        return ""


def _first_select_option_value(input_elem) -> str | None:
    placeholder_texts = {
        "bitte wählen",
        "bitte waehlen",
        "please select",
        "select",
        "auswählen",
        "auswaehlen",
    }
    try:
        options = input_elem.query_selector_all("option")
    except Exception:
        return None
    candidates = []
    for option in options:
        try:
            value = (option.get_attribute("value") or "").strip()
            text = (option.text_content() or "").strip()
            normalized_text = _normalize_lookup_text(text)
            if not value and (not text or normalized_text in placeholder_texts):
                continue
            if value:
                candidates.append(value)
            elif text:
                candidates.append(text)
        except Exception:
            continue
    return random.choice(candidates) if candidates else None


def _stepstone_positive_select_value(input_elem) -> str | None:
    positive_tokens = (
        "ja",
        "yes",
        "einverstanden",
        "zustimmen",
        "muttersprache",
        "fließend",
        "fliessend",
        "fortgeschritten",
        "b2",
        "c1",
        "c2",
    )
    try:
        options = input_elem.query_selector_all("option")
    except Exception:
        return None
    for option in options:
        try:
            value = (option.get_attribute("value") or "").strip()
            text = (option.text_content() or "").strip()
            normalized = _normalize_lookup_text(f"{text} {value}")
            if value and any(token in normalized for token in positive_tokens):
                return value
        except Exception:
            continue
    return None


def _stepstone_work_authorization_select_value(input_elem, label_text: str, answers: dict) -> str | None:
    try:
        options = input_elem.query_selector_all("option")
    except Exception:
        return None
    for option in options:
        try:
            value = (option.get_attribute("value") or "").strip()
            text = (option.text_content() or "").strip()
            if not value and not text:
                continue
            if _stepstone_option_matches_work_authorization(label_text, f"{text} {value}", answers):
                return value or text
        except Exception:
            continue
    return None


def _stepstone_config_answer(label_text: str, profile: dict, answers: dict) -> str | None:
    authorization_answer = _stepstone_work_authorization_answer(label_text, answers)
    if authorization_answer is not None:
        return authorization_answer

    for mapping in (answers, profile):
        value = _lookup_direct_mapping(label_text, mapping or {})
        if value not in (None, ""):
            return str(value)
    inferred = _infer_question_answer(label_text, profile, answers)
    return str(inferred) if inferred not in (None, "") else None


def _stepstone_work_authorization_answer(label_text: str, answers: dict) -> str | None:
    label_norm = _normalize_lookup_text(label_text)
    has_work_auth = any(token in label_norm for token in (
        "authorized to work",
        "authorised to work",
        "legally authorized",
        "legally authorised",
        "legally allowed to work",
        "allowed to work",
        "right to work",
        "legally work",
        "arbeitsberechtigt",
        "arbeitserlaubnis",
        "berechtigt zu arbeiten",
    ))
    if has_work_auth:
        value = answers.get("authorized_to_work")
        if value not in (None, ""):
            return str(value)
        return "Yes"

    if "visa" in label_norm or "sponsorship" in label_norm:
        value = answers.get("visa_sponsorship")
        if value not in (None, ""):
            return str(value)
        return "No"

    return None


def _stepstone_option_matches_work_authorization(label_text: str, option_text: str, answers: dict) -> bool:
    configured = _stepstone_work_authorization_answer(label_text, answers)
    if configured is None:
        return False

    label_norm = _normalize_lookup_text(label_text)
    option_norm = _normalize_lookup_text(option_text)
    wants_yes = _normalize_lookup_text(configured) in {"yes", "ja", "true", "authorized", "authorised"}
    wants_no = _normalize_lookup_text(configured) in {"no", "nein", "false"}
    asks_work_auth = any(token in label_norm for token in (
        "authorized to work",
        "authorised to work",
        "legally authorized",
        "legally authorised",
        "legally allowed to work",
        "allowed to work",
        "right to work",
        "legally work",
        "arbeitsberechtigt",
        "arbeitserlaubnis",
        "berechtigt zu arbeiten",
    ))

    uncertain_or_negative = any(token in option_norm for token in (
        "not sure",
        "unsure",
        "nicht sicher",
        "weiß nicht",
        "weiss nicht",
        "i do not know",
        "don t know",
        "do not know",
    ))
    requires_sponsorship = any(token in option_norm for token in (
        "require visa",
        "requires visa",
        "need visa",
        "needs visa",
        "require sponsorship",
        "requires sponsorship",
        "need sponsorship",
        "needs sponsorship",
        "visa sponsorship required",
    ))

    if wants_yes and asks_work_auth:
        if uncertain_or_negative or requires_sponsorship:
            return False
        return any(token in option_norm for token in (
            "yes",
            "ja",
            "allowed",
            "authorized",
            "authorised",
            "right to work",
            "work permit",
            "citizen",
            "citizenship",
            "permanent",
            "eu",
            "germany",
            "german",
        ))

    if wants_no and ("visa" in label_norm or "sponsorship" in label_norm):
        if uncertain_or_negative:
            return False
        return any(token in option_norm for token in (
            "no",
            "nein",
            "do not require",
            "don t require",
            "no sponsorship",
            "without sponsorship",
            "not require sponsorship",
        ))

    return False


def _stepstone_should_avoid_work_authorization_option(label_text: str, option_text: str, answers: dict) -> bool:
    configured = _stepstone_work_authorization_answer(label_text, answers)
    if configured is None:
        return False

    option_norm = _normalize_lookup_text(option_text)
    wants_yes = _normalize_lookup_text(configured) in {"yes", "ja", "true", "authorized", "authorised"}
    if not wants_yes:
        return False

    return any(token in option_norm for token in (
        "not sure",
        "unsure",
        "nicht sicher",
        "weiß nicht",
        "weiss nicht",
        "i do not know",
        "don t know",
        "do not know",
        "require visa",
        "requires visa",
        "need visa",
        "needs visa",
        "require sponsorship",
        "requires sponsorship",
        "need sponsorship",
        "needs sponsorship",
        "visa sponsorship required",
    ))


def _stepstone_salutation_config_value(profile: dict, answers: dict) -> str | None:
    for mapping in (profile, answers):
        for key in ("Anrede", "anrede", "salutation", "Salutation", "title", "Title"):
            value = (mapping or {}).get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _stepstone_salutation_option(input_elem, profile: dict, answers: dict, *, required_only: bool = False) -> str | None:
    try:
        field_name = _normalize_lookup_text(input_elem.get_attribute("name") or "")
        test_id = _normalize_lookup_text(input_elem.get_attribute("data-testid") or "")
    except Exception:
        return None

    if field_name != "salutation" and "select salutation" not in test_id:
        return None

    configured = _stepstone_salutation_config_value(profile, answers)
    if not configured and not required_only:
        return None

    normalized = _normalize_lookup_text(configured or "Herr")
    aliases = {
        "mr": "herr",
        "mister": "herr",
        "herr": "herr",
        "male": "herr",
        "m": "herr",
        "mrs": "frau",
        "ms": "frau",
        "miss": "frau",
        "frau": "frau",
        "female": "frau",
        "f": "frau",
        "other": "divers",
        "divers": "divers",
        "diverse": "divers",
        "non binary": "divers",
    }
    target = aliases.get(normalized, normalized)

    try:
        options = input_elem.query_selector_all("option")
    except Exception:
        return None

    for option in options:
        try:
            option_text = _normalize_lookup_text(option.text_content() or "")
            option_value = (option.get_attribute("value") or "").strip()
            option_value_norm = _normalize_lookup_text(option_value)
            if target in {option_text, option_value_norm} or aliases.get(option_value_norm) == target:
                return option_value or (option.text_content() or "").strip()
        except Exception:
            continue

    if required_only:
        return _first_select_option_value(input_elem)
    return None


def _stepstone_text_answer(label_text: str, profile: dict, answers: dict) -> str:
    inferred = _stepstone_config_answer(label_text, profile, answers)
    if inferred is not None:
        return str(inferred)

    label_lower = label_text.lower()
    if any(token in label_lower for token in ["arbeitsstunden", "stunden pro woche", "hours per week"]):
        return str(answers.get("weekly_hours") or answers.get("Gewünschte Arbeitsstunden pro Woche") or "40")
    if any(token in label_lower for token in ["verfügbar", "verfugbar", "eintritt", "startdatum", "available"]):
        for key in (
            "Wann wäre Ihr frühestmögliches Startdatum?",
            "earliest_start_date",
            "available_from",
            "start_date",
        ):
            value = answers.get(key)
            if value:
                return str(value)
        return "01/09/2026"

    return _fallback_text_answer(label_text, profile, answers)


def _stepstone_number_answer(text_value: str) -> str:
    """Return a browser-acceptable value for required StepStone number inputs."""
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(text_value or ""))
    if match:
        return match.group(0).replace(",", ".")
    return "4"


def _dispatch_stepstone_change(input_elem) -> None:
    try:
        input_elem.evaluate(
            """el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }"""
        )
    except Exception:
        pass


def _fill_stepstone_control(root, input_elem, profile: dict, answers: dict, *, required_only: bool = False) -> bool:
    field_type = (input_elem.get_attribute("type") or "text").lower()
    if field_type in {"hidden", "file", "submit", "button"}:
        return False
    if input_elem.is_disabled():
        return False

    label_text = _extract_field_label(root, input_elem)
    if required_only and not _is_stepstone_required_field(root, input_elem, label_text):
        return False

    try:
        tag_name = input_elem.evaluate("el => el.tagName").lower()
    except Exception:
        tag_name = "unknown"

    field_name = input_elem.get_attribute("name") or ""
    group_name = field_name or input_elem.get_attribute("id") or ""
    value = _stepstone_config_answer(label_text, profile, answers)

    if field_type == "radio":
        if _radio_group_has_checked_option(root, group_name):
            return False
        selected = bool(value and _select_radio_group(root, group_name, str(value)))
        if not selected:
            selected = _select_best_radio_option(root, group_name, label_text, value, profile, answers)
        if selected:
            _dispatch_stepstone_change(input_elem)
        return selected

    if field_type == "checkbox":
        try:
            if input_elem.is_checked():
                return False
        except Exception:
            pass
        selected = False
        if value and str(value).lower() in {"yes", "true", "ja", "selected"}:
            selected = _click_choice_input(root, input_elem, "checkbox")
        if not selected and required_only:
            selected = _click_choice_input(root, input_elem, "checkbox")
        if not selected:
            selected = _select_best_checkbox_option(root, group_name, label_text, value, profile, answers)
        if selected:
            _dispatch_stepstone_change(input_elem)
        return selected

    current_value = _element_current_text(input_elem)
    label_lower = label_text.lower()
    if current_value and "email" in label_lower:
        return False

    if tag_name == "select":
        if current_value.strip() and not required_only:
            return False
        selected_value = _stepstone_salutation_option(input_elem, profile, answers, required_only=required_only)
        if not selected_value:
            selected_value = _stepstone_work_authorization_select_value(input_elem, label_text, answers)
        if not selected_value:
            selected_value = _choose_dropdown_value(input_elem, label_text, profile, answers)
        if not selected_value and required_only:
            selected_value = _stepstone_positive_select_value(input_elem)
        if not selected_value and required_only:
            selected_value = _first_select_option_value(input_elem)
        if selected_value and _try_fill_select(input_elem, str(selected_value), label_text):
            _dispatch_stepstone_change(input_elem)
            return True
        return False

    if current_value.strip() and not required_only:
        return False
    if current_value.strip() and required_only:
        return False

    text_value = _stepstone_text_answer(label_text, profile, answers)
    if field_type == "number":
        text_value = _stepstone_number_answer(text_value)
    _fill_text_input(input_elem, text_value)
    _dispatch_stepstone_change(input_elem)
    return True


def _stepstone_option_text(input_elem) -> str:
    try:
        return (input_elem.evaluate(
            """el => {
                const id = el.getAttribute('id') || '';
                const label = id ? el.ownerDocument.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                if (label && (label.innerText || label.textContent)) {
                    return label.innerText || label.textContent;
                }
                let node = el.parentElement;
                for (let i = 0; node && i < 4; i++, node = node.parentElement) {
                    const text = (node.innerText || node.textContent || '').trim();
                    if (text) return text;
                }
                return el.getAttribute('value') || '';
            }"""
        ) or "").strip()
    except Exception:
        return ""


def _click_stepstone_choice(root, input_elem, field_type: str) -> bool:
    if _click_choice_input(root, input_elem, field_type):
        return True
    try:
        input_elem.check(force=True, timeout=1000)
        return True
    except Exception:
        pass
    try:
        input_elem.evaluate(
            """el => {
                el.checked = true;
                el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        return True
    except Exception:
        return False


def _stepstone_option_matches_answer(label_text: str, option_text: str, option_value: str, configured: str | None) -> bool:
    if not configured:
        return False

    configured_norm = _normalize_lookup_text(configured)
    option_norm = _normalize_lookup_text(f"{option_text} {option_value}")
    if configured_norm and configured_norm in option_norm:
        return True

    positive_answers = {"yes", "ja", "true", "selected", "ok"}
    negative_answers = {"no", "nein", "false"}
    positive_options = {"yes", "ja", "true", "wahr"}
    negative_options = {"no", "nein", "false", "falsch"}

    if configured_norm in positive_answers and any(token in option_norm.split() for token in positive_options):
        return True
    if configured_norm in negative_answers and any(token in option_norm.split() for token in negative_options):
        return True

    label_norm = _normalize_lookup_text(label_text)
    if configured_norm in positive_answers and any(token in label_norm for token in (
        "vor ort",
        "onsite",
        "hybrid",
        "remote",
        "bereit",
        "willing",
        "konnen sie",
        "können sie",
    )):
        return any(token in option_norm.split() for token in positive_options)

    return False


def _select_required_stepstone_radio_group(wrapper, label_text: str, profile: dict, answers: dict) -> bool:
    try:
        radios = wrapper.query_selector_all("input[type='radio']")
    except Exception:
        return False
    if not radios:
        return False

    try:
        if any(radio.is_checked() for radio in radios):
            return False
    except Exception:
        pass

    configured = _stepstone_config_answer(label_text, profile, answers)
    first_enabled = None
    fallback_choices = []
    for radio in radios:
        try:
            if radio.is_disabled():
                continue
            if first_enabled is None:
                first_enabled = radio
            raw_option_text = _stepstone_option_text(radio)
            option_value = radio.get_attribute("value") or ""
            if _stepstone_option_matches_answer(label_text, raw_option_text, option_value, configured):
                if _click_stepstone_choice(wrapper, radio, "radio"):
                    _dispatch_stepstone_change(radio)
                    log.info("Selected mandatory StepStone option '%s' for '%s'", raw_option_text, label_text)
                    return True
            if not _stepstone_should_avoid_work_authorization_option(label_text, f"{raw_option_text} {option_value}", answers):
                fallback_choices.append(radio)
        except Exception:
            continue

    positive_candidate = None
    for radio in fallback_choices:
        try:
            option_norm = _normalize_lookup_text(f"{_stepstone_option_text(radio)} {radio.get_attribute('value') or ''}")
            if any(token in option_norm.split() for token in ("ja", "yes", "true")):
                positive_candidate = radio
                break
        except Exception:
            continue

    fallback_radio = positive_candidate or (random.choice(fallback_choices) if fallback_choices else first_enabled)
    if fallback_radio and _click_stepstone_choice(wrapper, fallback_radio, "radio"):
        _dispatch_stepstone_change(fallback_radio)
        log.info("Selected fallback mandatory StepStone radio for '%s'", label_text)
        return True
    return False


def _select_required_stepstone_checkbox_group(wrapper, label_text: str, profile: dict, answers: dict) -> bool:
    try:
        checkboxes = wrapper.query_selector_all("input[type='checkbox']")
    except Exception:
        return False
    if not checkboxes:
        return False

    try:
        if any(checkbox.is_checked() for checkbox in checkboxes):
            return False
    except Exception:
        pass

    configured = _stepstone_config_answer(label_text, profile, answers)
    preferred_values = []
    if configured:
        if isinstance(configured, str):
            preferred_values.extend(part.strip() for part in re.split(r"[,;/|]", configured) if part.strip())
        else:
            preferred_values.append(str(configured))
    preferred_values.extend(["ja", "yes", "berlin", "remote", "100% remote"])
    preferred_norms = [_normalize_lookup_text(value) for value in preferred_values if value]

    first_enabled = None
    work_authorization_match = None
    for checkbox in checkboxes:
        try:
            if checkbox.is_disabled():
                continue
            if first_enabled is None:
                first_enabled = checkbox
            raw_option_text = _stepstone_option_text(checkbox)
            option_text = _normalize_lookup_text(raw_option_text)
            option_value = _normalize_lookup_text(checkbox.get_attribute("value") or "")
            haystack = f"{option_text} {option_value}"
            if work_authorization_match is None and _stepstone_option_matches_work_authorization(
                label_text,
                f"{raw_option_text} {checkbox.get_attribute('value') or ''}",
                answers,
            ):
                work_authorization_match = checkbox
                continue
            if _stepstone_option_matches_answer(label_text, raw_option_text, checkbox.get_attribute("value") or "", configured):
                if _click_stepstone_choice(wrapper, checkbox, "checkbox"):
                    _dispatch_stepstone_change(checkbox)
                    log.info("Selected mandatory StepStone option '%s' for '%s'", _stepstone_option_text(checkbox), label_text)
                    return True
            if any(preferred and preferred in haystack for preferred in preferred_norms):
                if _click_stepstone_choice(wrapper, checkbox, "checkbox"):
                    _dispatch_stepstone_change(checkbox)
                    log.info("Selected mandatory StepStone option '%s' for '%s'", _stepstone_option_text(checkbox), label_text)
                    return True
        except Exception:
            continue

    if work_authorization_match and _click_stepstone_choice(wrapper, work_authorization_match, "checkbox"):
        _dispatch_stepstone_change(work_authorization_match)
        log.info(
            "Selected mandatory StepStone work authorization option '%s' for '%s'",
            _stepstone_option_text(work_authorization_match),
            label_text,
        )
        return True

    fallback_choices = []
    for checkbox in checkboxes:
        try:
            option_text = f"{_stepstone_option_text(checkbox)} {checkbox.get_attribute('value') or ''}"
            if not checkbox.is_disabled() and not _stepstone_should_avoid_work_authorization_option(label_text, option_text, answers):
                fallback_choices.append(checkbox)
        except Exception:
            continue

    fallback_checkbox = random.choice(fallback_choices) if fallback_choices else first_enabled
    if fallback_checkbox and _click_stepstone_choice(wrapper, fallback_checkbox, "checkbox"):
        _dispatch_stepstone_change(fallback_checkbox)
        log.info("Selected fallback mandatory StepStone checkbox for '%s'", label_text)
        return True
    return False


def _fill_required_stepstone_wrappers(root, profile: dict, answers: dict) -> int:
    try:
        wrappers = root.query_selector_all("[data-testid*='atsiQuestions'][data-testid*='wrapper']")
    except Exception:
        return 0

    filled = 0
    for wrapper in wrappers:
        try:
            if not _is_required_stepstone_wrapper(wrapper):
                continue
            label_text = _stepstone_wrapper_label(wrapper)
            if _select_required_stepstone_radio_group(wrapper, label_text, profile, answers):
                filled += 1
                continue
            if _select_required_stepstone_checkbox_group(wrapper, label_text, profile, answers):
                filled += 1
                continue

            controls = wrapper.query_selector_all("input, select, textarea")
            for control in controls:
                if _is_empty_stepstone_control(wrapper, control) and _fill_stepstone_control(
                    wrapper,
                    control,
                    profile,
                    answers,
                    required_only=True,
                ):
                    filled += 1
                    break
        except Exception as exc:
            log.debug("Could not fill required StepStone wrapper: %s", exc)
            continue
    return filled


def _fill_stepstone_fields(page: Page, profile: dict, answers: dict) -> None:
    root = _stepstone_scope(page)
    try:
        inputs = root.query_selector_all("input, select, textarea")
    except Exception:
        inputs = []

    processed_choice_groups: set[str] = set()
    for input_elem in inputs:
        try:
            field_type = (input_elem.get_attribute("type") or "text").lower()
            field_name = input_elem.get_attribute("name") or ""
            label_text = _extract_field_label(root, input_elem)
            group_name = field_name or input_elem.get_attribute("id") or ""
            choice_group_key = field_name or f"{field_type}:{label_text}" or group_name
            if field_type in {"radio", "checkbox"}:
                if choice_group_key in processed_choice_groups:
                    continue
                if _fill_stepstone_control(root, input_elem, profile, answers):
                    processed_choice_groups.add(choice_group_key)
                continue

            _fill_stepstone_control(root, input_elem, profile, answers)
        except Exception as exc:
            log.debug("Could not fill StepStone field: %s", exc)
            continue


def _fill_required_stepstone_fields(page: Page, profile: dict, answers: dict) -> int:
    root = _stepstone_scope(page)
    try:
        root.evaluate("el => el.scrollIntoView({ block: 'start', behavior: 'instant' })")
    except Exception:
        pass

    try:
        inputs = root.query_selector_all("input, select, textarea")
    except Exception:
        return 0

    filled = _fill_required_stepstone_wrappers(root, profile, answers)
    processed_choice_groups: set[str] = set()
    for input_elem in inputs:
        try:
            field_type = (input_elem.get_attribute("type") or "text").lower()
            field_name = input_elem.get_attribute("name") or ""
            label_text = _extract_field_label(root, input_elem)
            group_name = field_name or input_elem.get_attribute("id") or ""
            choice_group_key = field_name or f"{field_type}:{label_text}" or group_name

            if field_type in {"radio", "checkbox"}:
                if choice_group_key in processed_choice_groups:
                    continue
                processed_choice_groups.add(choice_group_key)

            if not _is_stepstone_required_field(root, input_elem, label_text):
                continue
            if not _is_empty_stepstone_control(root, input_elem):
                continue
            if _fill_stepstone_control(root, input_elem, profile, answers, required_only=True):
                filled += 1
        except Exception as exc:
            log.debug("Could not fill required StepStone field: %s", exc)
            continue

    if filled:
        page.wait_for_timeout(500)
    return filled


def _find_submit_button(page: Page):
    selectors = [
        "button[data-testid='submit-button']",
        "button[aria-label='Bewerbung abschicken']",
        "button[type='submit']",
    ]
    for selector in selectors:
        try:
            for button in page.query_selector_all(selector):
                if button.is_visible() and button.is_enabled():
                    return button
        except Exception:
            continue

    try:
        for button in page.query_selector_all("button, input[type='submit']"):
            label = button.evaluate(
                "el => [el.getAttribute('aria-label') || '', el.innerText || '', el.value || ''].join(' ')"
            )
            normalized = " ".join(str(label or "").lower().split())
            if any(candidate in normalized for candidate in _STEPSTONE_SUBMIT_LABELS):
                if button.is_visible() and button.is_enabled():
                    return button
    except Exception:
        return None
    return None


def _submit_button_state(page: Page) -> tuple[object | None, bool]:
    try:
        button = page.query_selector("button[data-testid='submit-button'], button[aria-label='Bewerbung abschicken']")
        if button:
            return button, bool(button.is_visible() and button.is_enabled())
    except Exception:
        pass
    enabled = _find_submit_button(page)
    return enabled, bool(enabled)


def _ensure_stepstone_ready_to_submit(page: Page, profile: dict, answers: dict) -> bool:
    for attempt in range(1, 5):
        filled = _fill_required_stepstone_fields(page, profile, answers)
        button, enabled = _submit_button_state(page)
        if enabled:
            return True
        if filled <= 0 and button:
            try:
                button.evaluate("el => el.scrollIntoView({ block: 'center', behavior: 'instant' })")
            except Exception:
                pass
            page.wait_for_timeout(500)
        log.info("StepStone submit button still disabled after required-field pass %d", attempt)
    return False


def _has_success(page: Page) -> bool:
    text = _scope_text(page)
    return any(success_text in text for success_text in _STEPSTONE_SUCCESS_TEXTS)


def _stepstone_page_changed_after_submit(page: Page, before_url: str, before_text: str) -> bool:
    try:
        if page.is_closed():
            return True
    except Exception:
        return True

    try:
        current_url = page.url
    except Exception:
        current_url = ""
    if current_url and current_url != before_url:
        return True

    after_text = _scope_text(page)
    if not after_text:
        return True
    if after_text != before_text and "pflicht" not in after_text and "required" not in after_text:
        return True
    return False


def _wait_for_stepstone_submit_result(page: Page, before_url: str, before_text: str, timeout_ms: int = 15000) -> str | None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        try:
            if page.is_closed():
                return "APPLIED"
            if _has_success(page):
                return "APPLIED"
            submit, enabled = _submit_button_state(page)
            text = _scope_text(page)
            if ("pflicht" in text or "required" in text) and submit:
                return None
            if _stepstone_page_changed_after_submit(page, before_url, before_text):
                return "APPLIED"
            if submit and enabled:
                return None
        except PlaywrightError as exc:
            if "Cannot find context with specified id" in str(exc) or "Target page" in str(exc):
                return "APPLIED"
            return None
        except Exception:
            return None
        page.wait_for_timeout(1000)
    return None


def _submit_stepstone_application(page: Page, profile: dict, answers: dict, dry_run: bool) -> str:
    _ensure_stepstone_ready_to_submit(page, profile, answers)
    submit = _find_submit_button(page)
    if not submit:
        log.warning("Could not find StepStone submit button")
        return "FAILED"

    if dry_run:
        log.info("Dry run active; stopping before StepStone final submit")
        return "SKIPPED"

    before_url = page.url
    before_text = _scope_text(page)
    if not _click_action_button(submit):
        return "FAILED"

    result = _wait_for_stepstone_submit_result(page, before_url, before_text)
    if result:
        return result

    try:
        root = page.locator("[data-testid='smart-apply-form-container']").first.element_handle(timeout=1000)
    except Exception:
        root = page
    try:
        repaired = _repair_required_fields(root, profile, answers)
        if repaired <= 0:
            repaired = _rewrite_default_a_fields_to_four(root)
    except PlaywrightError as exc:
        if "Cannot find context with specified id" in str(exc) or "Target page" in str(exc):
            log.info("StepStone page changed after submit; treating application as submitted")
            return "APPLIED"
        raise
    if repaired > 0:
        log.info("StepStone validation blocked submit; repaired %d field(s) and retrying", repaired)
        page.wait_for_timeout(500)
        submit = _find_submit_button(page)
        if submit and _click_action_button(submit):
            result = _wait_for_stepstone_submit_result(page, before_url, before_text)
            if result:
                return result

    if _has_success(page):
        return "APPLIED"

    if _stepstone_page_changed_after_submit(page, before_url, before_text):
        return "APPLIED"
    return "SKIPPED"


def _stepstone_effective_answers(config_dict: dict) -> dict:
    answers = dict(config_dict.get("answers", {}) or {})
    location = str(config_dict.get("location", "") or "").strip()
    if location:
        answers.setdefault("location", location)
        answers.setdefault("Gewünschter Arbeitsort", location)
    return answers


def _apply_to_stepstone_job(
    context,
    job_url: str,
    config_dict: dict,
    dry_run: bool,
    referer: str | None = None,
) -> str:
    job_page = context.new_page()
    application_page = None
    application_attempt = None
    try:
        if not _goto_stepstone_job_page(job_page, job_url, referer=referer):
            return "SKIPPED"
        _dismiss_cookie_banner(job_page)

        title_keyword = str(config_dict.get("title_keyword", "") or "").strip()
        if not _job_title_matches(job_page, job_url, title_keyword):
            log.info("Skipping StepStone job because title does not match keyword: %s", job_url)
            return "SKIPPED"

        job = page_job_metadata(job_page, job_url)
        history = config_dict.get("_application_history") or ApplicationHistory(config_dict)
        application_attempt = history.attempt(job, "stepstone", dry_run=dry_run)
        if not application_attempt.allowed:
            return "SKIPPED"
        application_page = _click_application_start_button(job_page, context)
        if not application_page:
            log.info("StepStone job has no clickable application start button: %s", job_url)
            return "SKIPPED"

        application_page.wait_for_timeout(2000)

        profile = config_dict.get("profile", {}) or {}
        answers = _stepstone_effective_answers(config_dict)
        resume_path = str(Path(config_dict.get("resume_path", "")).resolve())

        _replace_main_cv_if_present(application_page, resume_path)
        _fill_stepstone_fields(application_page, profile, answers)
        result = _submit_stepstone_application(application_page, profile, answers, dry_run=dry_run)
        if not dry_run and result == "APPLIED":
            application_attempt.success(job | {"application_url": application_page.url})
        return "READY" if dry_run and result == "APPLIED" else result
    except HistoryError:
        raise
    except PlaywrightTimeoutError as exc:
        log.warning("StepStone timeout while applying to %s: %s", job_url, exc)
        return "FAILED"
    except Exception as exc:
        log.exception("StepStone error while applying to %s: %s", job_url, exc)
        return "FAILED"
    finally:
        if application_attempt:
            application_attempt.close()
        for page_to_close in (application_page, job_page):
            try:
                if page_to_close and not page_to_close.is_closed():
                    page_to_close.close()
            except Exception:
                pass


def _go_to_next_results_page(page: Page) -> bool:
    selectors = [
        "a[aria-label='Nächste']",
        "a[aria-label='Naechste']",
        "a[aria-label='Next']",
        "button[aria-label='Nächste']",
        "button[aria-label='Next']",
    ]
    for selector in selectors:
        try:
            control = page.locator(selector).first
            if control.count() and control.is_visible(timeout=1000):
                control.click(timeout=3000)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    return False


def search_and_apply(
    config_dict: dict,
    max_applications: int = 1,
    title_keyword: str = "",
    headless: bool = False,
    dry_run: bool = False,
    setup: bool = False,
) -> dict:
    """Search StepStone and apply to quick-apply jobs."""
    job_title = str(config_dict.get("job_title", "") or "").strip()
    location = str(config_dict.get("location", "") or "").strip()
    raw_delay = config_dict.get("delay", 0)
    max_pages = max(int(config_dict.get("max_scan_pages", 3) or 3), 1)

    if title_keyword and not config_dict.get("title_keyword"):
        config_dict = dict(config_dict)
        config_dict["title_keyword"] = title_keyword

    try:
        apply_delay_seconds = max(0.0, float(raw_delay))
    except (TypeError, ValueError):
        log.warning("Invalid StepStone apply delay %r; defaulting to 0 seconds", raw_delay)
        apply_delay_seconds = 0.0

    if not job_title or not location:
        log.error("job_title and location are required")
        return {"applied": 0, "skipped": 0, "failed": 0}

    applied = 0
    skipped = 0
    failed = 0
    seen_urls: set[str] = set()
    if not setup:
        config_dict = {**config_dict, "_application_history": ApplicationHistory(config_dict)}
    ready = 0

    with sync_playwright() as p:
        profile_dir = config.CHROME_WORKER_DIR / "stepstone-search"
        browser = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            **_stepstone_browser_launch_options(config_dict),
        )
        page = browser.new_page()

        try:
            setup_url = str(config_dict.get("start_url", "") or "").strip() or STEPSTONE_BASE_URL
            if setup:
                _hold_setup_session_open(page, setup_url)
                return {"applied": 0, "skipped": 0, "failed": 0}

            _open_stepstone_search(page, config_dict)
            _ensure_quick_apply_filter(page, config_dict)

            scanned_pages = 0
            while applied + ready < max_applications and scanned_pages < max_pages:
                scanned_pages += 1
                urls = [url for url in _find_stepstone_job_urls(page) if url not in seen_urls]
                log.info("Found %d StepStone job card(s) on page %d", len(urls), scanned_pages)
                if not urls:
                    break

                for job_url in urls:
                    if applied + ready >= max_applications:
                        break
                    seen_urls.add(job_url)
                    log.info("Processing StepStone job: %s", job_url)
                    result = _apply_to_stepstone_job(
                        browser,
                        job_url,
                        config_dict,
                        dry_run=dry_run,
                        referer=page.url,
                    )
                    if result == "APPLIED":
                        applied += 1
                    elif result == "READY":
                        ready += 1
                    elif result == "SKIPPED":
                        skipped += 1
                    else:
                        failed += 1

                    if apply_delay_seconds > 0:
                        page.wait_for_timeout(int(apply_delay_seconds * 1000))

                if applied + ready < max_applications and not _go_to_next_results_page(page):
                    break
        finally:
            browser.close()

    log.info("Final StepStone results: applied=%d, skipped=%d, failed=%d", applied, skipped, failed)
    return {"applied": applied, "ready": ready, "skipped": skipped, "failed": failed}
