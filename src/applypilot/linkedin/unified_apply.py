"""Unified LinkedIn search and apply in a single browser session."""

import json
import logging
import random as random_module
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.application_history import ApplicationHistory, HistoryError, page_job_metadata
from applypilot.linkedin.filters import configured_title_exclusion, title_is_ignored
from applypilot.linkedin.intelligent_apply import PreparationError, intelligent_session
from applypilot.linkedin.pagination import advance_results_page, configured_max_scan_pages, wait_for_fresh_cards

log = logging.getLogger(__name__)

_FORM_INPUT_SELECTOR = (
    'input, select, textarea, [role="radio"]:not(:has(input[type="radio"])), '
    '[role="checkbox"]:not(:has(input[type="checkbox"])), '
    '[role="combobox"]:not(:has(input, select, textarea)), '
    '[aria-haspopup="listbox"]:not(:has(input, select, textarea)), '
    '[role="listbox"]:not(:has(input[type="radio"], select))'
)


_PHONE_COUNTRY_LABEL_TOKENS = (
    "country code",
    "dial code",
    "phone country",
    "code pays",
    "indicatif",
    "landesvorwahl",
    "prefisso telefonico",
    "prefisso",
)

_PHONE_NUMBER_LABEL_TOKENS = (
    "phone",
    "mobile",
    "contact number",
    "telephone",
    "telefon",
    "telefono",
    "telefonico",
    "telefonnummer",
    "handynummer",
    "mobilnummer",
    "cellulare",
)

_FOLLOW_COMPANY_LABEL_TOKENS = (
    "follow",
    "suivre",
    "abonnieren",
    "folgen",
)

_SELECT_PLACEHOLDER_TEXTS = {
    "select an option",
    "select",
    "choose",
    "choose one",
    "selectionnez une option",
    "sélectionnez une option",
    "option auswahlen",
    "option auswählen",
    "seleziona un'opzione",
    "seleziona un’opzione",
    "seleziona",
}

_NUMERIC_RANGE_VALIDATION_TEXTS = (
    "enter a whole number between 0 and 99",
)

_INVALID_TYPE_VALIDATION_TEXTS = (
    "invalid input", "invalid type", "invalid value", "invalid number",
    "enter a valid number", "enter a number", "must be a number", "must be numeric",
    "ungültige eingabe", "ungultige eingabe", "ungültiger typ", "ungultiger typ",
    "ongeldige invoer", "ongeldig type",
    *_NUMERIC_RANGE_VALIDATION_TEXTS,
)

_UPLOAD_FORMAT_ERROR_TEXTS = (
    "please upload an acceptable document format (jpg, jpeg, gif, png). change file",
    "please upload an acceptable document format",
)

_EASY_APPLY_LIMIT_TEXTS = (
    "you reached today s easy apply limit",
    "reached today s easy apply limit",
    "easy apply limit",
    "continue applying tomorrow",
)

_RESUME_UPLOAD_LABEL_TOKENS = (
    "resume",
    "cv",
    "curriculum vitae",
    "lebenslauf",
)

_COVER_LETTER_UPLOAD_LABEL_TOKENS = (
    "cover letter",
    "coverletter",
    "anschreiben",
    "motivationsschreiben",
    "motivation letter",
    "lettre de motivation",
)

_NON_RESUME_UPLOAD_LABEL_TOKENS = (
    "photo",
    "picture",
    "image",
    "headshot",
    "portrait",
    "profile photo",
    "profilfoto",
    "bild",
)

_PHOTO_UPLOAD_LABEL_TOKENS = _NON_RESUME_UPLOAD_LABEL_TOKENS

_IMAGE_FILE_ACCEPT_TOKENS = (
    ".jpg",
    ".jpeg",
    ".gif",
    ".png",
    ".webp",
    "image/jpg",
    "image/jpeg",
    "image/gif",
    "image/png",
    "image/webp",
    "image/*",
)

_REQUIRED_VALIDATION_TEXTS = (
    "required",
    "this field is required",
    "field is required",
    "required field",
    "dieses feld ist erforderlich",
    "pflichtfeld",
)

_CONSENT_LABEL_TOKENS = (
    "consent",
    "privacy",
    "data processing",
    "personal data",
    "confirm i have read",
    "accepted",
    "agree",
    "einwilligung",
    "datenschutz",
    "personenbezogene daten",
    "zustimmen",
)

_HISTORY_EDITOR_LABELS = {
    "experience": (
        "edit experience", "add experience", "edit work experience", "add work experience",
        "berufserfahrung bearbeiten", "berufserfahrung hinzufügen", "berufserfahrung hinzufugen",
    ),
    "education": (
        "edit education", "add education", "ausbildung bearbeiten", "ausbildung hinzufügen",
        "ausbildung hinzufugen",
    ),
}

_HISTORY_DELETE_LABELS = {
    "experience": (
        "delete experience", "delete work experience", "remove experience", "remove work experience",
        "berufserfahrung löschen", "berufserfahrung loschen",
    ),
    "education": ("delete education", "remove education", "ausbildung löschen", "ausbildung loschen"),
}

_EASY_APPLY_NEXT_BUTTON_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Review')",
    "button:has-text('Suivant')",
    "button:has-text('Continuer')",
    "button:has-text('Weiter')",
    "button:has-text('Überprüfen')",
    "button:has-text('Uberprufen')",
    "button[aria-label*='Next']",
    "button[aria-label*='Continue']",
]

_EASY_APPLY_SUBMIT_BUTTON_SELECTORS = [
    "button[aria-label='Submit application']",
    "[data-live-test-easy-apply-submit-button]",
    "button:has-text('Submit application')",
    "button:has-text('Envoyer la candidature')",
    "button:has-text('Bewerbung absenden')",
    "button:has-text('Submit')",
    "button:has-text('Apply')",
    "button:has-text('Send')",
    "button[type='submit']",
    "button:has-text('Finish')",
]

_EASY_APPLY_NEXT_BUTTON_LABELS = {
    "next",
    "continue",
    "review",
    "suivant",
    "continuer",
    "weiter",
    "uberprufen",
}

_EASY_APPLY_SUBMIT_BUTTON_LABELS = {
    "submit application",
    "submit",
    "apply",
    "send",
    "finish",
    "envoyer la candidature",
    "bewerbung absenden",
}

_YES_OPTION_LABELS = {
    "yes",
    "true",
    "ja",
    "oui",
}

_NO_OPTION_LABELS = {
    "no",
    "false",
    "nein",
    "non",
}

_LINKEDIN_NEXT_PAGE_SELECTORS = [
    "button[data-testid='pagination-controls-next-button-visible']",
    "button[aria-label*='View next page']",
    "button[aria-label*='next' i]",
    "a[aria-label*='next' i]",
    "button:has-text('Next')",
]

_JOB_DETAIL_TEXT_SELECTORS = [
    "[id^='JobDetails_AboutTheJob']",
    "#job-details",
    ".jobs-description-content__text",
    ".jobs-box__html-content",
    "[data-sdui-screen*='SemanticJobDetails']",
    "[data-sdui-screen*='JobDetails']",
    ".jobs-search__job-details",
    "main",
]

_GERMAN_LANGUAGE_TERMS = (
    "und",
    "oder",
    "der",
    "die",
    "das",
    "ein",
    "eine",
    "einer",
    "einen",
    "mit",
    "fur",
    "für",
    "wir",
    "suchen",
    "dich",
    "sie",
    "ihre",
    "deine",
    "aufgaben",
    "profil",
    "kenntnisse",
    "erfahrung",
    "berufserfahrung",
    "verantwortlich",
    "bewerbung",
    "arbeits",
    "deutsch",
)

_ENGLISH_LANGUAGE_TERMS = (
    "and",
    "or",
    "the",
    "with",
    "for",
    "we",
    "you",
    "your",
    "our",
    "looking",
    "experience",
    "skills",
    "responsibilities",
    "requirements",
    "role",
    "team",
    "business",
    "apply",
    "candidate",
    "communication",
)


def _hold_setup_session_open(page, search_url: str) -> None:
    """Keep the interactive LinkedIn browser session alive until manual abort."""
    log.info("Opening LinkedIn setup session: %s", search_url)
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)
    log.info("LinkedIn setup session is ready; waiting for manual abort")

    try:
        while True:
            if page.is_closed():
                break
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("LinkedIn setup session interrupted by user")


def _build_linkedin_search_url(config_dict: dict) -> str:
    """Build the LinkedIn Easy Apply search URL from config."""
    explicit_url = str(config_dict.get("search_url", "") or "").strip()
    search_params = dict(config_dict.get("search_params") or {})
    job_title = str(config_dict.get("job_title", "") or "").strip()
    location = str(config_dict.get("location", "") or "").strip()
    if explicit_url and config_dict.get("old_url") is True:
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
        use_search_results = config_dict.get("use_search_results_url") is True
        path = "/jobs/search-results" if use_search_results else "/jobs/search"
        url_params = {}

    params = url_params | {
        "keywords": job_title,
        "location": location,
        "f_AL": "true",
    }
    # search_params is the intentional override surface. Stale fields copied in
    # search_url are ignored so the config location remains authoritative.
    params.update(search_params)
    params.pop("currentJobId", None)

    return f"https://www.linkedin.com{path}/?{urlencode(params)}"


def _browser_launch_options(config_dict: dict) -> dict:
    """Build Playwright launch options for the configured browser."""
    options = {}

    browser_name = str(config_dict.get("browser", "") or "").strip().lower()
    if browser_name == "brave":
        brave_path = config.get_brave_path()
        log.info("Using Brave browser executable: %s", brave_path)
        options["executable_path"] = brave_path
    elif browser_name == "chrome":
        chrome_path = config.get_chrome_path()
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
        value = config_dict.get(config_key)
        if value not in (None, "", [], {}):
            options[option_key] = value

    headers = dict(config_dict.get("extra_http_headers") or {})
    accept_language = config_dict.get("accept_language")
    if accept_language:
        headers["Accept-Language"] = accept_language
    if headers:
        options["extra_http_headers"] = headers

    return options


def _extract_job_id_from_componentkey(value: str | None) -> str:
    """Extract a LinkedIn job id from newer SDUI job-card component keys."""
    match = re.search(r"job-card-component-ref-(\d+)", value or "")
    return match.group(1) if match else ""


def _normalize_linkedin_job_url(url: str) -> str:
    """Return an absolute LinkedIn URL for job links found in cards."""
    if not url:
        return ""
    if url.startswith("/"):
        return f"https://www.linkedin.com{url}"
    return url


def _find_linkedin_job_cards(page) -> list:
    """Find job cards across LinkedIn's legacy and newer jobs layouts."""
    selectors = [
        "[componentkey^='job-card-component-ref-']",
        "div.job-card-container",
    ]
    cards = []
    seen = set()

    for selector in selectors:
        try:
            for card in page.query_selector_all(selector):
                component_key = card.get_attribute("componentkey") or ""
                job_id = _extract_job_id_from_componentkey(component_key)
                key = job_id or f"{selector}:{id(card)}"
                if key in seen:
                    continue
                seen.add(key)
                cards.append(card)
        except Exception:
            continue

    return cards


def _config_flag_enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _ordered_linkedin_job_cards(job_cards: list, randomize: bool) -> list[tuple[int, object]]:
    ordered_cards = list(enumerate(job_cards))
    if randomize:
        random_module.shuffle(ordered_cards)
    return ordered_cards


def _extract_job_url_from_card(card) -> str:
    """Extract the LinkedIn job detail URL from a legacy or SDUI job card."""
    try:
        job_link = card.query_selector("a[href*='/jobs/view/']")
        if job_link:
            url = _normalize_linkedin_job_url(job_link.get_attribute("href") or "")
            if url:
                return url
    except Exception:
        pass

    try:
        job_id = _extract_job_id_from_componentkey(card.get_attribute("componentkey"))
        if job_id:
            return f"https://www.linkedin.com/jobs/view/{job_id}/"
    except Exception:
        pass

    try:
        child = card.query_selector("[componentkey^='job-card-component-ref-']")
        if child:
            job_id = _extract_job_id_from_componentkey(child.get_attribute("componentkey"))
            if job_id:
                return f"https://www.linkedin.com/jobs/view/{job_id}/"
    except Exception:
        pass

    return ""


def _clean_linkedin_card_text(value: str | None) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    for marker in ("(Verified job)", "(verified job)"):
        if marker in text:
            before, after = text.split(marker, 1)
            if after.strip() and before.strip().lower() == after.strip().lower():
                return f"{before.strip()} {marker}".strip()
    midpoint = len(text) // 2
    if len(text) % 2 == 0 and text[:midpoint].strip().lower() == text[midpoint:].strip().lower():
        return text[:midpoint].strip()
    return text


def _title_from_linkedin_label(value: str | None) -> str:
    text = _clean_linkedin_card_text(value)
    match = re.search(r"\b(?:job\s+)?title\s*,\s*(.+)", text, flags=re.IGNORECASE)
    return _clean_linkedin_card_text(match.group(1)) if match else text


def _looks_like_linkedin_job_title(value: str) -> bool:
    text = _clean_linkedin_card_text(value)
    if not text or len(text) > 220:
        return False
    lowered = text.lower()
    rejected = {
        "easy apply",
        "promoted",
        "viewed",
        "new",
        "verified",
        "verified job",
        "actively recruiting",
        "actively reviewing applicants",
    }
    if lowered in rejected:
        return False
    if lowered.startswith("posted ") or lowered.endswith(" ago"):
        return False
    if re.search(r"(?:€|\$|£|\b(?:eur|usd|gbp)\b|/\s*(?:yr|year|mo|month|hour|hr)\b)", lowered, flags=re.IGNORECASE):
        return False
    return bool(re.search(r"[a-zA-Z]", text))


def _extract_job_title_from_card(card) -> str:
    """Extract a job title from legacy and newer LinkedIn SDUI cards."""
    selectors = [
        "a[href*='/jobs/view/']",
        "h3",
        "h2",
        ".job-card-list__title",
        ".job-card-container__link",
        ".base-search-card__title",
        "[data-testid='job-card-title']",
        "[data-testid='job-item-title']",
        "[aria-label^='Job title']",
        "[aria-label*='Job title,']",
        "[aria-label^='Title']",
        "[aria-label*='Title,']",
        "p",
        "p span:not([aria-hidden='true'])",
        "p span",
    ]

    for selector in selectors:
        try:
            elements = card.query_selector_all(selector)
        except Exception:
            elements = []

        for elem in elements:
            for value in (
                _title_from_linkedin_label(elem.get_attribute("aria-label")),
                elem.get_attribute("title"),
                elem.text_content() or "",
            ):
                text = _clean_linkedin_card_text(value)
                if _looks_like_linkedin_job_title(text):
                    return text

    try:
        text = card.evaluate(
            """card => {
                const normalize = value => (value || "").replace(/\\s+/g, " ").trim();
                const bad = new Set([
                    "easy apply",
                    "promoted",
                    "viewed",
                    "new",
                        "verified",
                        "verified job",
                        "actively recruiting",
                        "actively reviewing applicants",
                    ]);
                const selectors = [
                    "a[href*='/jobs/view/']",
                    "h3",
                    "h2",
                    ".job-card-list__title",
                    ".job-card-container__link",
                    ".base-search-card__title",
                    "[data-testid='job-card-title']",
                    "[data-testid='job-item-title']",
                    "[aria-label*='Job title']",
                    "[aria-label*='Title,']",
                    "p",
                    "p span:not([aria-hidden='true'])",
                    "p span",
                ];
                for (const selector of selectors) {
                    for (const el of card.querySelectorAll(selector)) {
                        const aria = normalize(el.getAttribute("aria-label"))
                            .replace(/^((job\\s+)?title)\\s*,\\s*/i, "");
                        const title = normalize(el.getAttribute("title"));
                        const text = normalize(el.innerText || el.textContent);
                        for (const value of [aria, title, text]) {
                        const lower = value.toLowerCase();
                        if (
                            value
                            && value.length <= 220
                            && /[a-z]/i.test(value)
                            && !bad.has(lower)
                            && !lower.startsWith("posted ")
                            && !lower.endsWith(" ago")
                            && !/(?:€|\\$|£|\\b(?:eur|usd|gbp)\\b|\\/\\s*(?:yr|year|mo|month|hour|hr)\\b)/i.test(lower)
                        ) {
                                return value;
                            }
                        }
                    }
                }
                return "";
            }"""
        )
        text = _clean_linkedin_card_text(text)
        if _looks_like_linkedin_job_title(text):
            return text
    except Exception:
        pass

    # Some result cards are the title link itself, rather than its parent.
    try:
        if "/jobs/view/" in (card.get_attribute("href") or ""):
            text = _clean_linkedin_card_text(card.text_content() or "")
            if _looks_like_linkedin_job_title(text):
                return text
    except PlaywrightError as exc:
        log.debug("Could not read self-anchored LinkedIn job title: %s", exc)

    return ""


def _scroll_linkedin_job_card_into_view(card) -> None:
    """Ask LinkedIn's virtualized list to render a card's visible contents."""
    try:
        card.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
    except Exception:
        pass


def _click_linkedin_job_card(card) -> None:
    """Open a job from either a legacy anchor card or a newer clickable card."""
    job_link = card.query_selector("a[href*='/jobs/view/']")
    if job_link:
        job_link.click()
        return

    card.click()


def _linkedin_job_id(url: str) -> str | None:
    parsed = urlsplit(url)
    match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)", parsed.path)
    if match:
        return match[1]
    value = dict(parse_qsl(parsed.query)).get("currentJobId", "")
    return value if value.isdecimal() else None


def _find_easy_apply_control(page, *, job_url: str | None = None):
    """Find a live locator for the visible Easy Apply control in the detail pane.

    LinkedIn's newer SDUI layout renders Easy Apply as an anchor whose href
    points at /jobs/view/<id>/apply/?openSDUIApplyFlow=true rather than as the
    legacy button. Prefer that stable URL shape, then fall back to legacy
    button selectors.
    """
    selectors = [
        "[data-sdui-screen*='JobDetails'] a[href*='/jobs/view/'][href*='/apply/']",
        "[data-sdui-screen*='jobs.SemanticJobDetails'] a[aria-label*='Easy Apply']",
        "[data-sdui-screen*='jobs.SemanticJobDetails'] a[aria-label='LinkedIn Apply to this job']",
        "a[aria-label='LinkedIn Apply to this job'][href*='currentJobId=']",
        "a[aria-label='LinkedIn Apply to this job']",
        "main a[href*='/jobs/view/'][href*='/apply/'][href*='openSDUIApplyFlow=true']",
        "a[aria-label='Easy Apply to this job'][href*='currentJobId=']",
        "a[aria-label='Easy Apply to this job']",
        "a[aria-label*='Easy Apply'][href*='/apply/']",
        ".job-details-top-card button:has-text('Easy Apply')",
        "[data-test-id='jobs-details'] button:has-text('Easy Apply')",
        ".jobs-detail__main-content button:has-text('Easy Apply')",
        ".jobs-apply-button",
        "button[aria-label='Easy Apply job']",
        "button[aria-label*='Easy Apply']",
        "button:has-text('Easy Apply')",
    ]

    expected_id = _linkedin_job_id(job_url) if job_url else None
    for selector in selectors:
        try:
            matches = page.locator(selector)
            for index in range(matches.count()):
                elem = matches.nth(index)
                if not elem.is_visible():
                    continue
                try:
                    if not elem.is_enabled(timeout=500):
                        continue
                except Exception:
                    pass

                text = (elem.text_content(timeout=500) or "").strip().lower()
                aria = (elem.get_attribute("aria-label", timeout=500) or "").strip().lower()
                raw_href = elem.get_attribute("href", timeout=500) or ""
                href = raw_href.strip().lower()
                combined = f"{text} {aria} {href}"

                is_linkedin_apply = "linkedin apply to this job" in aria
                if "easy apply" not in combined and "/apply/" not in href and not is_linkedin_apply:
                    continue
                if "search-results" in href and "/jobs/view/" not in href and "currentjobid=" not in href:
                    continue
                control_id = _linkedin_job_id(raw_href)
                if expected_id and control_id and control_id != expected_id:
                    continue

                bounding_box = elem.bounding_box(timeout=500)
                if not bounding_box:
                    continue

                if expected_id and control_id:
                    # Pin job-bearing links as well as their visual selector, so
                    # another job cannot replace the link during action waiting.
                    return page.locator(f"{selector}[href={json.dumps(raw_href)}]:visible").first
                return elem
        except Exception:
            continue

    return None


def _current_linkedin_job_id(page) -> str | None:
    """Prefer rendered detail identity over a possibly stale search URL."""
    result = page.evaluate("""() => {
        const visible = el => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
                && (rect.width > 0 || rect.height > 0);
        };
        const ids = new Set();
        for (const el of document.querySelectorAll(
            "[id^='JobDetails_AboutTheJob_'], [componentkey^='JobDetails_AboutTheJob_']"
        )) {
            if (!visible(el)) continue;
            const match = (el.id || el.getAttribute('componentkey') || '').match(/JobDetails_AboutTheJob_(\\d+)$/);
            if (match) ids.add(match[1]);
        }
        if (ids.size) return {ids: [...ids]};
        for (const el of document.querySelectorAll(
            ".job-details-jobs-unified-top-card__job-title a[href*='/jobs/view/'], " +
            "[data-sdui-screen*='JobDetails'] h1 a[href*='/jobs/view/'], " +
            "[data-sdui-screen*='JobDetails'] p a[href*='/jobs/view/'], " +
            ".jobs-search__job-details h1 a[href*='/jobs/view/'], " +
            ".jobs-detail__main-content h1 a[href*='/jobs/view/'], " +
            ".jobs-search__job-details [data-job-id], .job-details-jobs-unified-top-card[data-job-id]"
        )) {
            if (!visible(el)) continue;
            const match = (el.getAttribute('href') || '').match(/\\/jobs\\/view\\/(?:[^/]*-)?(\\d+)(?:\\/|$)/);
            const id = el.getAttribute('data-job-id') || (match && match[1]);
            if (id && /^\\d+$/.test(id)) ids.add(id);
        }
        return {ids: [...ids]};
    }""")
    ids = result["ids"]
    if ids:
        return ids[0] if len(ids) == 1 else None
    return _linkedin_job_id(page.url)


def _restore_job_detail_header(page, job_id: str) -> None:
    """Bring a virtualized detail header back without changing the selected job."""
    page.evaluate("""jobId => {
        const section = document.querySelector(`[id='JobDetails_AboutTheJob_${jobId}'], ` +
            `[componentkey='JobDetails_AboutTheJob_${jobId}'], #job-details, .jobs-description-content__text`);
        for (let el = section && section.parentElement; el; el = el.parentElement) {
            if (el.scrollHeight > el.clientHeight + 2 && /auto|scroll|overlay/.test(getComputedStyle(el).overflowY)) {
                el.scrollTop = 0;
            }
        }
        if (document.scrollingElement) document.scrollingElement.scrollTop = 0;
    }""", job_id)


def _open_easy_apply_for_job(page, job_url: str, *, record=None, timeout_ms: int = 8000) -> None:
    """Recheck the job and reacquire its control after preparation/re-rendering."""
    job_id = _linkedin_job_id(job_url)
    if not job_id:
        raise ValueError("Cannot open Easy Apply without a LinkedIn job ID")
    def report(outcome, message):
        if record:
            record("easy_apply_open", outcome, message)
        else:
            log.info("Easy Apply job=%s %s: %s", job_id, outcome, message)

    deadline = time.monotonic() + timeout_ms / 1000
    clicks = 0
    reason = "The selected job or its Easy Apply control did not become available"
    report("START", f"Reacquiring the current Easy Apply control for job {job_id}")
    while time.monotonic() < deadline:
        current_id = _current_linkedin_job_id(page)
        if current_id and current_id != job_id:
            reason = f"Selected job changed from {job_id} to {current_id}; refusing application handoff"
            report("FAILED", reason)
            raise RuntimeError(reason)
        if current_id == job_id:
            _restore_job_detail_header(page, job_id)
            control = _find_easy_apply_control(page, job_url=job_url)
            if control:
                # Scrolling can itself trigger another asynchronous render.
                if _current_linkedin_job_id(page) != job_id:
                    continue
                clicks += 1
                try:
                    control.click(timeout=max(1, min(2000, int((deadline - time.monotonic()) * 1000))))
                except PlaywrightError as exc:
                    if not isinstance(exc, PlaywrightTimeoutError) and not re.search(
                        r"not attached|detached|not connected|not visible|not stable", str(exc), re.IGNORECASE,
                    ):
                        raise
                    if _get_active_modal(page):
                        report("SUCCESS", "Easy Apply dialog is already open after the click")
                        return
                    reason = str(exc).splitlines()[0]
                    report("RETRY", f"Control changed or was not actionable during click {clicks}/3; reacquiring")
                    if clicks >= 3:
                        break
                else:
                    report("SUCCESS", f"Clicked the current control for job {job_id}")
                    return
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms > 0:
            page.wait_for_timeout(min(250, remaining_ms))
    report("FAILED", reason)
    raise RuntimeError(f"Could not open Easy Apply for job {job_id}: {reason}")


_LINKEDIN_LOCATION_EDITOR_SELECTOR = ", ".join((
    "[componentkey='LocationTypeahead_TextEditor'][contenteditable='true']",
    "[contenteditable='true'][aria-label*='location' i]",
    "input[id*='jobs-search-box-location']",
    ".jobs-search-box__input--location input",
    "input[aria-label*='location' i]",
    "input[placeholder*='location' i]",
    "input[componentkey*='Location' i]",
))


def _normalize_location_text(value: str) -> str:
    """Normalize location labels for resilient text matching."""
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def _location_text_without_distance(value: str) -> str:
    normalized = _normalize_location_text(value)
    return re.sub(r"\b\d+\s*(?:mi|mile|miles|km|kilometer|kilometers)\b", "", normalized).strip()


def _linkedin_location_matches_config(active_location: str, config_location: str) -> bool:
    """Return True when LinkedIn's rendered location still points at the configured place."""
    active = _location_text_without_distance(active_location)
    expected = _normalize_location_text(config_location)
    if not active or not expected:
        return False

    if active == expected or active in expected or expected in active:
        return True

    primary_expected = _normalize_location_text(str(config_location).split(",", 1)[0])
    if primary_expected and (primary_expected in active or active in primary_expected):
        return True

    primary_token = primary_expected.split(" ", 1)[0] if primary_expected else ""
    return len(primary_token) > 2 and primary_token in active.split()


def _verify_active_linkedin_search_location(page, location: str) -> bool:
    """Abort guard: do not apply if LinkedIn rendered a different search location."""
    active_location = _active_linkedin_search_location(page)
    if not active_location:
        log.warning("Could not read LinkedIn search location; continuing with configured search URL")
        return True

    if _linkedin_location_matches_config(active_location, location):
        log.info("LinkedIn location matches config: %s", active_location)
        return True

    log.error(
        "LinkedIn location mismatch; refusing to apply. Page shows '%s', config expects '%s'",
        active_location,
        location,
    )
    return False


def _ensure_configured_linkedin_search_location(page, location: str) -> bool:
    """Ensure LinkedIn's rendered search location matches config, repairing mismatches."""
    active_location = _active_linkedin_search_location(page)
    if not active_location:
        log.warning("Could not read LinkedIn search location; continuing with configured search URL")
        return True

    if _linkedin_location_matches_config(active_location, location):
        log.info("LinkedIn location matches config: %s", active_location)
        return True

    log.warning(
        "LinkedIn location shows '%s', expected config location '%s'; correcting it",
        active_location,
        location,
    )
    if not _ensure_active_linkedin_search_location(page, location):
        log.error(
            "LinkedIn location mismatch could not be corrected. Page shows '%s', config expects '%s'",
            active_location,
            location,
        )
        return False

    page.wait_for_timeout(1500)
    return _verify_active_linkedin_search_location(page, location)


def _normalize_company_name(value: str) -> str:
    """Normalize company names for blacklist matching."""
    return _normalize_location_text(value)


def _is_blacklisted_company(company_name: str, blacklisted_companies: tuple[str, ...]) -> bool:
    """Return True when the company matches any configured blacklist entry."""
    normalized_company = _normalize_company_name(company_name)
    if not normalized_company:
        return False
    return any(
        blocked == normalized_company
        or blocked in normalized_company
        or normalized_company in blocked
        for blocked in blacklisted_companies
    )


def _company_name_from_aria_label(value: str | None) -> str:
    """Extract the company name from labels like 'Company, Reonic.'."""
    label = (value or "").strip()
    match = re.search(r"\bcompany\s*,\s*(.+?)[\s.]*$", label, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_company_name(card, page) -> str:
    """Best-effort company extraction from the job card or detail panel."""
    selectors = [
        "[aria-label^='Company,']",
        "[aria-label*='Company,']",
        "[componentkey^='auto-binding-'][aria-label*='Company,']",
        "[data-sdui-screen*='JobDetails'] [aria-label*='Company,']",
        ".artdeco-entity-lockup__subtitle span[aria-hidden='true']",
        ".job-card-container__company-name",
        ".job-card-container__primary-description",
        ".job-details-jobs-unified-top-card__company-name a",
        ".job-details-jobs-unified-top-card__company-name",
        ".jobs-unified-top-card__company-name a",
        ".jobs-unified-top-card__company-name",
    ]

    for scope in (card, page):
        for selector in selectors:
            try:
                elem = scope.query_selector(selector)
                if not elem:
                    continue
                company_from_aria = _company_name_from_aria_label(elem.get_attribute("aria-label"))
                if company_from_aria:
                    return company_from_aria
                text = (elem.text_content() or "").strip()
                if text:
                    return text
            except Exception:
                continue

    return ""


def _active_linkedin_search_keyword(page) -> str:
    """Read the visible LinkedIn semantic search input value."""
    selectors = [
        "input[data-testid='typeahead-input'][placeholder='Describe the job you want']",
        "input[componentkey='semanticSearchBox']",
        "input[aria-autocomplete='list'][placeholder*='job']",
    ]
    for selector in selectors:
        try:
            elem = page.query_selector(selector)
            if not elem:
                continue
            value = (elem.input_value() or elem.get_attribute("value") or "").strip()
            if value:
                return value
        except Exception:
            continue
    return ""


def _active_linkedin_search_location(page) -> str:
    """Read the committed location label, or the legacy inline location input."""
    try:
        location_text = page.evaluate(
            r"""editorSelector => {
                const visible = el => el && el.getClientRects().length > 0
                    && getComputedStyle(el).visibility !== 'hidden';
                const text = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                const controls = document.querySelectorAll(
                    'button[aria-label*="location" i], [role="button"][aria-label*="location" i], '
                    + '[aria-label*="location" i]:has(p)'
                );
                for (const control of controls) {
                    if (!visible(control)) continue;
                    const label = text([...control.querySelectorAll('p')].find(visible) || control);
                    if (label && !/^(?:(?:change|edit|search(?: by)?) )?location$/i.test(label)) return label;
                }
                for (const marker of document.querySelectorAll('svg[id^="location-marker"]')) {
                    if (!visible(marker)) continue;
                    let node = marker.parentElement;
                    for (let depth = 0; node && node !== document.body && depth < 5; depth++, node = node.parentElement) {
                        const label = [...node.querySelectorAll('p')].find(visible);
                        if (label && text(label)) return text(label);
                    }
                }
                // Contenteditable text is only a draft in a popover, not an applied location.
                const popovers = [...controls].flatMap(control =>
                    ['aria-controls', 'aria-owns'].flatMap(attr =>
                        (control.getAttribute(attr) || '').split(/\s+/).filter(Boolean)
                    ).map(id => document.getElementById(id)).filter(Boolean)
                );
                for (const input of document.querySelectorAll(editorSelector)) {
                    if (input.tagName !== 'INPUT' || !visible(input) || !input.value.trim()) continue;
                    if (input.closest('[role="dialog"], [role="listbox"]') || popovers.some(el => el.contains(input))) continue;
                    return input.value.trim();
                }
                return '';
            }""",
            _LINKEDIN_LOCATION_EDITOR_SELECTOR,
        )
        if location_text:
            return str(location_text).strip()
    except Exception:
        pass
    return ""


def _log_active_linkedin_search_state(page, job_title: str, location: str) -> None:
    """Log whether LinkedIn rendered the configured search terms."""
    active_keyword = _active_linkedin_search_keyword(page)
    active_location = _active_linkedin_search_location(page)

    if active_keyword and _normalize_location_text(job_title) not in _normalize_location_text(active_keyword):
        log.warning(
            "LinkedIn search input shows '%s', expected job_title '%s'",
            active_keyword,
            job_title,
        )
    elif active_keyword:
        log.info("LinkedIn search input: %s", active_keyword)

    if active_location and not _linkedin_location_matches_config(active_location, location):
        log.warning(
            "LinkedIn location shows '%s', expected config location '%s'",
            active_location,
            location,
        )
    elif active_location:
        log.info("LinkedIn location: %s", active_location)


def _dismiss_recruiter_upsell_if_present(page) -> bool:
    """Close LinkedIn's recruiter upsell overlay if it blocks the jobs page."""
    try:
        result = page.evaluate(
            """() => {
                const normalize = (value) => (value || "").toLowerCase().replace(/\\s+/g, " ").trim();
                const text = normalize(document.body && document.body.innerText);
                if (!text.includes("hire top talent faster")) return { ok: false };

                const candidates = [
                    ...document.querySelectorAll("button[aria-label*='Dismiss'], button[aria-label*='Close']"),
                    ...document.querySelectorAll("button")
                ];
                const closeButton = candidates.find((button) => {
                    const label = normalize(button.getAttribute("aria-label"));
                    const buttonText = normalize(button.textContent);
                    const rect = button.getBoundingClientRect();
                    const style = window.getComputedStyle(button);
                    if (rect.width <= 0 || rect.height <= 0 || style.visibility === "hidden" || style.display === "none") {
                        return false;
                    }
                    return label.includes("dismiss")
                        || label.includes("close")
                        || buttonText === "x"
                        || button.querySelector("svg#close-medium, svg#close-small");
                });
                if (!closeButton) return { ok: false, reason: "close_not_found" };
                closeButton.click();
                return { ok: true };
            }"""
        )
        if result.get("ok"):
            log.info("Dismissed LinkedIn recruiter upsell overlay")
            page.wait_for_timeout(500)
            return True
    except Exception as exc:
        log.debug("Could not dismiss LinkedIn recruiter upsell overlay: %s", exc)
    return False


def _ensure_active_linkedin_search_keyword(page, job_title: str, *, force_dropdown: bool = False) -> bool:
    """Set the visible LinkedIn search keyword when an old copied URL was used."""
    target = (job_title or "").strip()
    if not target:
        return False

    active_keyword = _active_linkedin_search_keyword(page)
    if not force_dropdown and active_keyword and _normalize_location_text(target) in _normalize_location_text(active_keyword):
        return True

    selectors = [
        "input[data-testid='typeahead-input'][placeholder='Describe the job you want']",
        "input[componentkey='semanticSearchBox']",
        "input[aria-autocomplete='list'][placeholder*='job']",
    ]
    for selector in selectors:
        try:
            field = page.locator(selector).first
            field.wait_for(state="visible", timeout=3000)
            field.click()
            if force_dropdown:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(target, delay=20)
            else:
                field.fill(target)
            page.wait_for_timeout(500)

            selected = page.evaluate(
                """() => {
                    const anchors = [...document.querySelectorAll("a[href*='/jobs/search-results/'][href*='keywords=']")];
                    const option = anchors.find((anchor) => {
                        if (anchor.closest("[componentkey='semanticSearchBoxLocationList']")) return false;
                        const rect = anchor.getBoundingClientRect();
                        const style = window.getComputedStyle(anchor);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
                    });
                    if (!option) return { ok: false };
                    const text = (option.textContent || "").trim();
                    option.click();
                    return { ok: true, text };
                }"""
            )
            if selected.get("ok"):
                log.info("Selecting top LinkedIn keyword result: %s", selected.get("text") or "<unknown>")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception as exc:
                    log.debug("LinkedIn keyword selection did not trigger a normal page load: %s", exc)
                page.wait_for_timeout(1500)
                return True

            updated_keyword = _active_linkedin_search_keyword(page)
            if _normalize_location_text(target) in _normalize_location_text(updated_keyword):
                return True
        except Exception:
            continue

    return False


def _ensure_easy_apply_search_filter(page) -> bool:
    """Click the jobs search Easy Apply filter pill when it is visible and unchecked."""
    try:
        result = page.evaluate(
            """() => {
                const normalize = (value) => (value || "").toLowerCase().replace(/\\s+/g, " ").trim();
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
                };

                const candidates = [
                    ...document.querySelectorAll("[componentkey*='APPLY_WITH_LINKEDIN_Easy Apply']"),
                    ...document.querySelectorAll("[aria-label='Filter by Easy Apply']"),
                    ...[...document.querySelectorAll("label")].filter((label) => normalize(label.textContent) === "easy apply"),
                ];

                for (const candidate of candidates) {
                    const pill = candidate.closest("[role='radio'], [role='button']") || candidate;
                    if (!visible(pill)) continue;
                    const input = pill.querySelector("input[type='checkbox']") || candidate.querySelector?.("input[type='checkbox']");
                    if (pill.getAttribute("aria-checked") === "true" || (input && input.checked)) {
                        return { ok: true, already: true };
                    }
                    const clickable = pill.querySelector("label") || pill;
                    clickable.scrollIntoView({ block: "center", inline: "center" });
                    clickable.click();
                    return { ok: true, clicked: true };
                }

                return { ok: false };
            }"""
        )
        if result.get("clicked"):
            log.info("Clicked LinkedIn Easy Apply search filter")
            page.wait_for_timeout(1500)
        return bool(result.get("ok"))
    except Exception as exc:
        log.debug("Could not click LinkedIn Easy Apply search filter: %s", exc)
        return False


def _wait_for_linkedin_location_match(page, location: str, *, timeout_ms: int = 5000) -> bool:
    """Allow asynchronous location selection/navigation to update the search header."""
    for elapsed in range(0, timeout_ms + 1, 200):
        if _linkedin_location_matches_config(_active_linkedin_search_location(page), location):
            return True
        if elapsed < timeout_ms:
            page.wait_for_timeout(200)
    return False


def _select_linkedin_location_suggestion(page, editor, location: str, *, timeout_ms: int = 3000) -> bool:
    """Select a matching location, never an unrelated top suggestion or keyword result."""
    for elapsed in range(0, timeout_ms + 1, 200):
        try:
            result = editor.evaluate(
                r"""(editor, target) => {
                    const visible = el => el && el.getClientRects().length > 0
                        && getComputedStyle(el).visibility !== 'hidden';
                    const normalize = value => (value || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
                        .toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
                    const expected = normalize(target);
                    const primary = normalize(target.split(',')[0]);
                    const scopes = [...document.querySelectorAll('[componentkey="semanticSearchBoxLocationList"]')];
                    for (const attr of ['aria-controls', 'aria-owns']) {
                        for (const id of (editor.getAttribute(attr) || '').split(/\s+/).filter(Boolean)) {
                            const scope = document.getElementById(id);
                            if (scope) scopes.push(scope);
                        }
                    }
                    const legacy = editor.closest('.jobs-search-box__input--location');
                    if (legacy) scopes.push(legacy);
                    if (!scopes.some(visible)) scopes.push(...document.querySelectorAll('[role="listbox"]'));
                    const options = [...new Set(scopes.filter(visible).flatMap(scope => [...scope.querySelectorAll(
                        '[role="option"], a[href*="/jobs/search"], li'
                    )]))].filter(el => visible(el) && !el.closest('[aria-disabled="true"], [disabled]'));
                    const score = el => {
                        const label = normalize(el.innerText || el.textContent);
                        if (label === expected) return 3;
                        if (` ${label} `.includes(` ${expected} `)) return 2;
                        return label === primary ? 1 : 0;
                    };
                    const match = options.filter(el => score(el) > 0).sort((a, b) => score(b) - score(a))[0];
                    if (!match) return { clicked: false };
                    const text = (match.innerText || match.textContent || '').trim();
                    match.scrollIntoView({block: 'nearest'});
                    match.click();
                    return { clicked: true, text };
                }""",
                location,
                timeout=1000,
            )
            if result.get("clicked"):
                log.info("Selecting LinkedIn location result: %s", result.get("text") or location)
                return True
        except PlaywrightError as exc:
            log.debug("Location suggestions changed while selecting: %s", exc)
            break
        if elapsed < timeout_ms:
            page.wait_for_timeout(200)
    return False


def _ensure_active_linkedin_search_location(page, location: str) -> bool:
    """Set the configured location through either the modern popover or legacy input."""
    target = (location or "").strip()
    if not target:
        return False

    active_location = _active_linkedin_search_location(page)
    if active_location and _linkedin_location_matches_config(active_location, target):
        return True

    try:
        editor = page.locator(f":is({_LINKEDIN_LOCATION_EDITOR_SELECTOR}):visible").first
        if not editor.count():
            # The reader and opener must support the same labelled controls: newer
            # layouts need not contain the old location-marker-small SVG at all.
            clicked = False
            for attempt in range(12):
                opened = page.evaluate(
                    r"""() => {
                        const visible = el => el && el.getClientRects().length > 0
                            && getComputedStyle(el).visibility !== 'hidden';
                        const controls = [...document.querySelectorAll(
                            'button[aria-label*="location" i], [role="button"][aria-label*="location" i], '
                            + '[aria-label*="location" i]:has(p)'
                        )].filter(visible);
                        let clickable = controls.find(control => {
                            const label = [...control.querySelectorAll('p')].find(visible) || control;
                            const text = (label.innerText || label.textContent || '').replace(/\s+/g, ' ').trim();
                            return text && !/^(?:(?:change|edit|search(?: by)?) )?location$/i.test(text);
                        });
                        const marker = [...document.querySelectorAll('svg[id^="location-marker"]')].find(visible);
                        if (!clickable && marker) clickable = marker.closest('[role="button"], button, a');
                        if (!clickable) clickable = controls[0];
                        if (!clickable && marker) {
                            let node = marker.parentElement;
                            while (node && node !== document.body) {
                                if (node.querySelector('p')) { clickable = node; break; }
                                node = node.parentElement;
                            }
                        }
                        if (!clickable) return { clicked: false, popupIds: [] };
                        const popupIds = ['aria-controls', 'aria-owns'].flatMap(attr =>
                            (clickable.getAttribute(attr) || '').split(/\s+/).filter(Boolean)
                        );
                        clickable.scrollIntoView({ block: 'center', inline: 'center' });
                        clickable.click();
                        return { clicked: true, popupIds };
                    }"""
                )
                clicked = opened.get("clicked", False)
                if clicked:
                    # A generic text editor is safe only within the location
                    # control's own popover, never elsewhere in the search page.
                    scoped_editors = [
                        f"[id={json.dumps(popup_id)}] :is(input:not([type]), input[type='text'], "
                        "input[type='search'], [contenteditable='true'])"
                        for popup_id in opened.get("popupIds", [])
                    ]
                    if scoped_editors:
                        editor = page.locator(
                            f":is({_LINKEDIN_LOCATION_EDITOR_SELECTOR}, {', '.join(scoped_editors)}):visible"
                        ).first
                if clicked or editor.count():
                    break
                if attempt < 11:
                    page.wait_for_timeout(250)
            if not clicked and not editor.count():
                log.warning("Could not find a visible LinkedIn location search control")
                return False
            editor.wait_for(state="visible", timeout=3000)

        editor.click(timeout=2000)
        try:
            editor.fill(target, timeout=2000)
        except PlaywrightError:
            # Some older contenteditable implementations require keyboard events.
            editor.press("ControlOrMeta+A", timeout=1000)
            editor.press("Backspace", timeout=1000)
            editor.press_sequentially(target, delay=20, timeout=2000)
        if not _select_linkedin_location_suggestion(page, editor, target):
            editor.press("Enter", timeout=2000)
        return _wait_for_linkedin_location_match(page, target)
    except Exception as exc:
        log.warning("Failed to update LinkedIn search location to '%s': %s", target, exc)

    return False


def _apply_exact_location_filter(page, location: str) -> bool:
    """Open LinkedIn filters and select the exact configured location."""
    target = _normalize_location_text(location)
    if not target:
        return False

    try:
        all_filters_btn = page.locator(
            "button[aria-label*='Show all filters'], "
            "button.search-reusables__all-filters-pill-button"
        ).first
        all_filters_btn.wait_for(state="visible", timeout=10000)
        all_filters_btn.click()

        location_fieldset = page.locator(
            "fieldset",
            has=page.locator("h3:has-text('Location'), legend:has-text('Location')")
        ).first
        location_fieldset.wait_for(state="visible", timeout=10000)
        match_result = location_fieldset.evaluate(
            """(fieldset, target) => {
                const normalize = (value) => (value || "")
                    .normalize("NFKD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, " ")
                    .trim();

                const options = [...fieldset.querySelectorAll("input[type='checkbox'][name='location-filter-value']")]
                    .map((input) => {
                        const label = input.id ? fieldset.querySelector(`label[for="${input.id}"]`) : null;
                        const text = normalize(label ? label.textContent : input.value);
                        return {
                            id: input.id || "",
                            text,
                            rawText: label ? (label.textContent || "").trim() : (input.value || ""),
                            checked: Boolean(input.checked),
                        };
                    });

                const match = options.find((option) => option.text === target)
                    || options.find((option) => option.text.includes(target) || target.includes(option.text));

                if (!match) {
                    return {
                        ok: false,
                        reason: "option_not_found",
                        options: options.map((option) => option.rawText).filter(Boolean).slice(0, 20),
                    };
                }

                const input = match.id ? fieldset.querySelector(`input#${match.id}`) : null;
                const label = match.id ? fieldset.querySelector(`label[for="${match.id}"]`) : null;
                if (!input) {
                    return { ok: false, reason: "input_not_found", label: match.rawText };
                }

                if (label) {
                    label.scrollIntoView({ block: "center" });
                    label.click();
                } else {
                    input.scrollIntoView({ block: "center" });
                    input.click();
                }

                return {
                    ok: true,
                    label: match.rawText,
                    checked: Boolean(input.checked),
                };
            }""",
            target,
        )

        if not match_result.get("ok"):
            options = ", ".join(match_result.get("options", []))
            log.warning(
                "Exact location filter '%s' was not found in LinkedIn filters (%s). Options seen: %s",
                location,
                match_result.get("reason", "unknown"),
                options or "none",
            )
            close_btn = page.locator("button[aria-label*='Dismiss'], button[aria-label*='Close']").first
            if close_btn.count():
                close_btn.click()
            return False

        page.wait_for_timeout(500)

        verified = location_fieldset.evaluate(
            """(fieldset, target) => {
                const normalize = (value) => (value || "")
                    .normalize("NFKD")
                    .replace(/[\\u0300-\\u036f]/g, "")
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, " ")
                    .trim();

                return [...fieldset.querySelectorAll("input[type='checkbox'][name='location-filter-value']")]
                    .some((input) => {
                        const label = input.id ? fieldset.querySelector(`label[for="${input.id}"]`) : null;
                        return normalize(label ? label.textContent : input.value) === target && input.checked;
                    });
            }""",
            target,
        )

        if not verified:
            log.warning("LinkedIn location filter '%s' was clicked but not checked", location)
            return False

        show_results_btn = page.locator(
            "button:has-text('Show results'), button[aria-label*='Apply current filters']"
        ).first
        show_results_btn.wait_for(state="visible", timeout=10000)
        show_results_btn.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        log.info(
            "Applied exact LinkedIn location filter: %s (matched '%s')",
            location,
            match_result.get("label", location),
        )
        return True
    except Exception as exc:
        log.warning("Failed to apply exact LinkedIn location filter '%s': %s", location, exc)
        return False


def _is_visible_enabled_control(element) -> bool:
    """Return True when a Playwright element handle is usable for a click."""
    try:
        if not element.is_visible() or not element.is_enabled():
            return False
    except Exception:
        return False

    try:
        if (element.get_attribute("aria-disabled") or "").strip().lower() == "true":
            return False
        if element.get_attribute("disabled") is not None:
            return False
    except Exception:
        pass

    try:
        box = element.bounding_box()
        if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
            return False
    except Exception:
        return False

    return True


def _find_linkedin_next_page_button(page):
    """Find the visible LinkedIn jobs pagination next button."""
    for selector in _LINKEDIN_NEXT_PAGE_SELECTORS:
        try:
            for button in page.query_selector_all(selector):
                if _is_visible_enabled_control(button):
                    return button
        except Exception:
            continue
    return None


def _linkedin_result_card_keys(cards) -> set[str]:
    """Use job identity, not tracking parameters or DOM nodes, to detect fresh results."""
    return {
        _linkedin_job_id(url) or url.split("?", 1)[0]
        for card in cards if (url := _extract_job_url_from_card(card))
    }


def _wait_for_linkedin_job_cards(page, previous_keys: set[str] | None = None) -> list:
    return wait_for_fresh_cards(
        page, read_cards=_find_linkedin_job_cards, card_keys=_linkedin_result_card_keys,
        previous_keys=previous_keys,
    )


def _next_linkedin_results_page(page, page_num: int, previous_keys: set[str]) -> tuple[list, str]:
    return advance_results_page(
        page, page_num, previous_keys, find_next=_find_linkedin_next_page_button,
        click_next=_click_action_button, wait_cards=_wait_for_linkedin_job_cards, logger=log,
    )


@intelligent_session
def search_and_apply(
    config_dict: dict,
    max_applications: int = 1,
    title_keyword: str = "",
    headless: bool = False,
    dry_run: bool = False,
    exact_location: bool = False,
    setup: bool = False,
) -> dict:
    """Search LinkedIn jobs and apply to them in a single browser session.

    Args:
        config_dict: Config with job_title, location, profile, answers, resume_path
        max_applications: Max number of jobs to apply to
        title_keyword: Filter jobs by this keyword (empty = apply to all)
        headless: Whether to run the browser headlessly
        dry_run: Whether to stop before the final submit step
        setup: Whether to keep the browser session open for manual changes only

    Returns:
        Dict with applied, skipped, failed counts, optional ready and stopped_reason
    """
    job_title = config_dict.get("job_title", "")
    location = config_dict.get("location", "")
    profile = config_dict.get("profile", {})
    answers = config_dict.get("answers", {})
    resume_path = config_dict.get("resume_path", "")
    photo_path = config_dict.get("photo_path", "")
    company_blacklist = config_dict.get("company_blacklist", [])
    to_ignore = configured_title_exclusion(config_dict) if not setup else ""
    raw_delay = config_dict.get("delay", 0)
    randomize_page_jobs = _config_flag_enabled(config_dict.get("random", False))
    max_pages = configured_max_scan_pages(config_dict, default=None) if not setup else None
    blacklisted_companies = tuple(
        _normalize_company_name(company)
        for company in company_blacklist
        if isinstance(company, str) and _normalize_company_name(company)
    )

    try:
        apply_delay_seconds = max(0.0, float(raw_delay))
    except (TypeError, ValueError):
        log.warning("Invalid LinkedIn apply delay %r; defaulting to 0 seconds", raw_delay)
        apply_delay_seconds = 0.0

    if not job_title or not location:
        log.error("job_title and location are required")
        return {"applied": 0, "skipped": 0, "failed": 0}

    applied = 0
    ready = 0
    skipped = 0
    failed = 0
    preparer = config_dict.get("_intelligent_preparer")
    seen_intelligent_jobs = set()
    history = None if setup else ApplicationHistory(config_dict)
    company_skipped = 0
    search_stats = {"pages_scanned": 0, "candidate_urls": 0, "stop_reason": ""}
    seen_result_pages = set()

    def summary(*, extra_failure=0, stopped_reason=None):
        result = {"applied": applied, "skipped": skipped, "failed": failed + extra_failure}
        result["company_cooldown_skipped"] = company_skipped
        result["search_stats"] = dict(search_stats)
        if preparer or dry_run:
            result["ready"] = ready
        if stopped_reason:
            result["stopped_reason"] = stopped_reason
            result["search_stats"]["stop_reason"] = stopped_reason
        return result

    try:
        with sync_playwright() as p:
            # Launch persistent context with saved LinkedIn session
            profile_dir = config.CHROME_WORKER_DIR / "linkedin-search"
            browser = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=headless,
                **_browser_launch_options(config_dict),
            )
            page = browser.new_page()

            # Navigate to LinkedIn jobs search
            search_url = _build_linkedin_search_url(config_dict)
            if setup:
                _hold_setup_session_open(page, search_url)
                return {"applied": 0, "skipped": 0, "failed": 0}

            log.info(f"Navigating to {search_url}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            _log_active_linkedin_search_state(page, job_title, location)
            use_old_url = config_dict.get("old_url") is True
            active_location_before_repair = _active_linkedin_search_location(page)
            location_was_repaired = bool(active_location_before_repair) and not _linkedin_location_matches_config(
                active_location_before_repair,
                location,
            )
            if not _ensure_configured_linkedin_search_location(page, location):
                return summary(extra_failure=1)
            if use_old_url or location_was_repaired:
                _ensure_easy_apply_search_filter(page)
            _log_active_linkedin_search_state(page, job_title, location)

            if exact_location:
                _apply_exact_location_filter(page, location)
                if not _ensure_configured_linkedin_search_location(page, location):
                    return summary(extra_failure=1)
                _log_active_linkedin_search_state(page, job_title, location)

            if _dismiss_recruiter_upsell_if_present(page) and use_old_url:
                _ensure_easy_apply_search_filter(page)

            # Process jobs on current page
            job_cards = None
            while applied + ready < max_applications:
                if _find_easy_apply_limit_modal(page):
                    log.warning("LinkedIn Easy Apply limit reached for today; stopping run")
                    return summary(stopped_reason="easy_apply_limit_reached")

                if _dismiss_recruiter_upsell_if_present(page) and use_old_url:
                    _ensure_easy_apply_search_filter(page)
                    if not _ensure_configured_linkedin_search_location(page, location):
                        return summary(extra_failure=1)
                log.info("Scanning LinkedIn Easy Apply results page %s/%s (applied: %s/%s)",
                         search_stats["pages_scanned"] + 1, max_pages or "all", applied, max_applications)

                # Find job cards. LinkedIn has both legacy card containers and
                # newer SDUI button-like cards keyed as job-card-component-ref-*.
                if job_cards is None:
                    job_cards = _wait_for_linkedin_job_cards(page)
                log.info(f"Found {len(job_cards)} job cards on page")

                if not job_cards:
                    search_stats["stop_reason"] = "initial_results_timeout"
                    log.warning("No readable LinkedIn job cards appeared within 10 seconds on the initial page")
                    break

                page_keys = _linkedin_result_card_keys(job_cards)
                page_fingerprint = frozenset(page_keys)
                if page_fingerprint in seen_result_pages:
                    search_stats["stop_reason"] = "repeated_results_page"
                    log.warning("LinkedIn returned a previously scanned results page; stopping to avoid a loop")
                    break
                seen_result_pages.add(page_fingerprint)
                search_stats["pages_scanned"] += 1

                ordered_job_cards = _ordered_linkedin_job_cards(job_cards, randomize_page_jobs)
                if randomize_page_jobs:
                    log.info(
                        "Random LinkedIn job order for this page: %s",
                        [idx for idx, _card in ordered_job_cards],
                    )

                for idx, card in ordered_job_cards:
                    if applied + ready >= max_applications:
                        break

                    prepared = None
                    application_attempt = None
                    try:
                        # Get job URL
                        job_url = _extract_job_url_from_card(card)
                        if not job_url:
                            log.debug(f"Card {idx}: No job URL found")
                            continue
                        search_stats["candidate_urls"] += 1
                        if preparer:
                            identity = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)", job_url)
                            job_key = identity[1] if identity else job_url
                            if job_key in seen_intelligent_jobs:
                                skipped += 1
                                continue
                            seen_intelligent_jobs.add(job_key)

                        _scroll_linkedin_job_card_into_view(card)
                        page.wait_for_timeout(250)

                        # Read only the title, excluding company/description text.
                        if title_keyword or to_ignore:
                            title_text = _extract_job_title_from_card(card)
                            if not title_text:
                                log.info(
                                    "Card %s: Skipping because a title filter is enabled but no title was found", idx,
                                )
                                skipped += 1
                                continue

                            if title_is_ignored(title_text, to_ignore):
                                log.info("Card %s: Skipping because title '%s' contains to_ignore '%s' "
                                         "(case-insensitive)", idx, title_text, to_ignore)
                                skipped += 1
                                continue

                            if title_keyword and title_keyword.lower() not in title_text.lower():
                                log.info(
                                    "Card %s: Skipping because title '%s' does not contain title_keyword '%s'",
                                    idx,
                                    title_text,
                                    title_keyword,
                                )
                                skipped += 1
                                continue

                        log.info(f"Card {idx}: Found job {job_url}")

                        # Click on job to open it
                        _click_linkedin_job_card(card)
                        page.wait_for_timeout(3000)

                        company_name = _extract_company_name(card, page)
                        if _is_blacklisted_company(company_name, blacklisted_companies):
                            log.info(
                                "Card %s: Skipping blacklisted company '%s'",
                                idx,
                                company_name,
                            )
                            skipped += 1
                            continue

                        # Look for Easy Apply in the job detail panel. Newer
                        # LinkedIn SDUI pages render this as an <a> with an
                        # /apply/ href rather than a legacy <button>.
                        if not _find_easy_apply_control(page):
                            log.info(f"Card {idx}: No Easy Apply control found in job detail")
                            skipped += 1
                            continue

                        details = None
                        if preparer:
                            from applypilot.linkedin.scrape import read_current_job

                            details = read_current_job(page, job_url)
                            if title_is_ignored(details.get("title", ""), to_ignore):
                                log.info("Card %s: Skipping because detail title '%s' contains to_ignore '%s' "
                                         "(case-insensitive)", idx, details["title"], to_ignore)
                                skipped += 1
                                continue
                            if _is_blacklisted_company(details.get("company", ""), blacklisted_companies):
                                skipped += 1
                                continue
                            job_detail_text = details["description"]
                        else:
                            job_detail_text = _extract_job_detail_text(page)
                        detected_language = _detect_job_language(job_detail_text)
                        if not _job_language_allowed(config_dict, detected_language):
                            log.info(
                                "Card %s: Skipping because only_english is enabled and detected language is %s",
                                idx,
                                detected_language or "unclear",
                            )
                            skipped += 1
                            continue

                        application_job = dict(details) if details else page_job_metadata(page, job_url)
                        application_job.update(url=job_url, company=(details or {}).get("company") or company_name)
                        application_attempt = history.attempt(application_job, "linkedin-apply", dry_run=dry_run)
                        if not application_attempt.allowed:
                            company_skipped += 1
                            skipped += 1
                            continue
                        if preparer:
                            prepared = preparer.prepare(details)
                            prepared.verify_published()
                            selected_resume_path = str(prepared.published_pdf)
                        else:
                            selected_resume_path, detected_language = _select_resume_path_for_job(
                                config_dict, job_detail_text, resume_path,
                            )
                        if prepared:
                            log.info("Card %s: Using verified job-specific résumé %s", idx, selected_resume_path)
                        elif detected_language and selected_resume_path != resume_path:
                            log.info(
                                "Card %s: Detected job language as %s; using resume %s",
                                idx,
                                detected_language,
                                selected_resume_path,
                            )
                        elif detected_language:
                            log.info(
                                "Card %s: Detected job language as %s; using fallback resume %s",
                                idx,
                                detected_language,
                                selected_resume_path,
                            )

                        log.info(f"Card {idx}: Clicking Easy Apply control")
                        _open_easy_apply_for_job(page, job_url, record=prepared.audit.record if prepared else None)
                        _wait_for_easy_apply_form_ready(page)

                        # Try to fill form (simplified version)
                        result = _fill_and_submit_form(
                            page,
                            profile,
                            answers,
                            selected_resume_path,
                            photo_path=photo_path,
                            dry_run=dry_run,
                            require_fresh_resume=bool(prepared),
                        )

                        if dry_run and result == "APPLIED":
                            result = "READY"
                        if result == "APPLIED":
                            applied += 1
                            application_attempt.success(application_job)
                            log.info(f"Successfully applied to job {idx}")
                        elif result == "READY":
                            ready += 1
                            log.info("Job %s ready; dry run stopped before submission", idx)
                        elif result == "LIMIT_REACHED":
                            if prepared:
                                prepared.finish("failed:easy_apply_limit_reached")
                            log.warning("LinkedIn Easy Apply limit reached for today; stopping run")
                            return summary(stopped_reason="easy_apply_limit_reached")
                        elif result == "SKIPPED":
                            skipped += 1
                            log.info(f"Skipped job {idx}")
                        else:
                            failed += 1
                            log.info(f"Failed to apply to job {idx}")

                        if prepared:
                            outcome = "ready" if result == "READY" else "applied" if result == "APPLIED" else (
                                "skipped" if result == "SKIPPED" else "failed"
                            )
                            prepared.finish(outcome)

                        _close_easy_apply_modal(page)
                        page.wait_for_timeout(1000)

                        if apply_delay_seconds > 0:
                            log.info(
                                "Waiting %.2f seconds before processing the next job",
                                apply_delay_seconds,
                            )
                            page.wait_for_timeout(int(apply_delay_seconds * 1000))

                    except HistoryError as e:
                        log.error("Application history error; stopping to prevent duplicate submissions: %s", e)
                        return summary(stopped_reason="history_error")
                    except PreparationError as e:
                        log.error("%s", e)
                        failed += 1
                        if prepared and not prepared.finalized:
                            prepared.finish("failed")
                        if e.fatal:
                            return summary(stopped_reason=f"intelligent_{e.stage}_failed")
                        _close_easy_apply_modal(page)
                    except Exception as e:
                        log.error(f"Error processing card {idx}: {e}")
                        failed += 1
                        if prepared:
                            prepared.finish("failed")
                        if preparer:
                            _close_easy_apply_modal(page)
                        continue
                    finally:
                        if application_attempt:
                            application_attempt.close()

                if applied + ready >= max_applications:
                    search_stats["stop_reason"] = "max_applications_reached"
                    break
                if max_pages is not None and search_stats["pages_scanned"] >= max_pages:
                    search_stats["stop_reason"] = "max_scan_pages_reached"
                    break
                job_cards, reason = _next_linkedin_results_page(page, search_stats["pages_scanned"], page_keys)
                if reason:
                    search_stats["stop_reason"] = reason
                    break

            browser.close()

    except Exception as e:
        log.error(f"Error in search_and_apply: {e}")
        import traceback
        traceback.print_exc()
        if preparer:
            return summary(extra_failure=1, stopped_reason="intelligent_run_failed")

    log.info("Final results: applied=%s, skipped=%s, failed=%s; pages_scanned=%s; scan_stop_reason=%s",
             applied, skipped, failed, search_stats["pages_scanned"], search_stats["stop_reason"])
    return summary()


def _smart_select_option(select_elem, value: str, label: str, log) -> bool:
    """Try to select an option in a dropdown with intelligent matching.

    Tries multiple patterns to match the value to available options.
    Returns True if selection succeeded, False otherwise.
    """
    try:
        # Log available options for debugging
        try:
            options = select_elem.query_selector_all("option")
            opt_texts = [f"{opt.text_content()}" for opt in options[:5]]  # First 5
            if len(options) > 5:
                opt_texts.append(f"... and {len(options) - 5} more")
            log.debug(f"Dropdown '{label}' has options: {opt_texts}")
        except:
            pass

        # First, try the value as-is
        try:
            select_elem.select_option(str(value))
            log.debug(f"Selected '{label}' with exact value: {value}")
            return True
        except Exception as e:
            log.debug(f"Exact match failed for '{label}': {e}")
            pass

        # For phone country codes, try without the + sign
        if any(token in label.lower() for token in _PHONE_COUNTRY_LABEL_TOKENS):
            value_str = str(value).replace("+", "")
            try:
                select_elem.select_option(value_str)
                log.debug(f"Selected '{label}' with value (no +): {value_str}")
                return True
            except:
                pass

        # Try to find by partial text match in available options
        options = select_elem.query_selector_all("option")
        value_lower = str(value).lower()
        for opt in options:
            opt_text = (opt.text_content() or "").lower()
            opt_value = (opt.get_attribute("value") or "").lower()

            # Try exact match on text or value
            if opt_text == value_lower or opt_value == value_lower:
                try:
                    select_elem.select_option(opt.get_attribute("value") or opt_text)
                    log.debug(f"Selected '{label}' by text/value match: {opt_text}")
                    return True
                except:
                    pass

            # Try partial match for country codes
            if any(token in label.lower() for token in _PHONE_COUNTRY_LABEL_TOKENS):
                if value_lower.replace("+", "") in opt_value or value_lower.replace("+", "") in opt_text:
                    try:
                        select_elem.select_option(opt.get_attribute("value") or opt_text)
                        log.debug(f"Selected '{label}' by partial match: {opt_text}")
                        return True
                    except:
                        pass

        log.debug(f"Could not find matching option in '{label}' for value: {value}")
        return False
    except Exception as e:
        log.debug(f"Error in smart select: {e}")
        return False


def _find_discard_confirmation_modal(page):
    """Return the visible top-layer discard-confirmation modal when present."""
    selectors = [
        "[data-test-modal-id='data-test-easy-apply-discard-confirmation']",
        "dialog[data-testid='dialog']",
        "dialog[open]",
        ".artdeco-modal-overlay--layer-confirmation[aria-hidden='false']",
        ".artdeco-modal-overlay--is-top-layer [role='alertdialog']",
        ".artdeco-modal[role='alertdialog']",
        "[role='alertdialog']",
    ]
    for selector in selectors:
        try:
            for modal in page.query_selector_all(selector):
                if not modal.is_visible():
                    continue
                modal_text = _normalize_lookup_text(_scope_text(modal))
                if (
                    "save this application" in modal_text
                    or "application will be discarded" in modal_text
                    or ("discard" in modal_text and "save" in modal_text and "application" in modal_text)
                ):
                    return modal
        except Exception:
            continue
    return None


def _is_easy_apply_limit_modal(scope) -> bool:
    """Return True when LinkedIn says today's Easy Apply limit has been reached."""
    modal_text = _normalize_lookup_text(_scope_text(scope))
    if not modal_text:
        return False

    if any(text in modal_text for text in _EASY_APPLY_LIMIT_TEXTS):
        return "easy apply" in modal_text and ("limit" in modal_text or "tomorrow" in modal_text)

    return False


def _find_easy_apply_limit_modal(page):
    """Return the visible daily Easy Apply limit modal when LinkedIn shows it."""
    selectors = [
        "[data-sdui-screen*='EasyApplyFuseLimitDialogModal']",
        "dialog[data-testid='dialog']",
        "dialog[open]",
        "[role='dialog']",
        "[role='alertdialog']",
    ]
    for selector in selectors:
        try:
            for modal in page.query_selector_all(selector):
                if not modal.is_visible():
                    continue
                if _is_easy_apply_limit_modal(modal):
                    return modal
        except Exception:
            continue
    return None


def _get_active_modal(page):
    """Return the visible Easy Apply modal/dialog if one is open."""
    limit_modal = _find_easy_apply_limit_modal(page)
    if limit_modal:
        return limit_modal

    discard_modal = _find_discard_confirmation_modal(page)
    if discard_modal:
        return discard_modal

    selectors = [
        "div.jobs-easy-apply-modal",
        "[data-testid='dialog-content']:has([data-sdui-screen*='EasyApply'])",
        "[data-sdui-screen*='EasyApply']",
        ".artdeco-modal[role='dialog']",
        ".artdeco-modal[role='alertdialog']",
        "[role='dialog']",
        "[role='alertdialog']",
        "dialog",
    ]
    for selector in selectors:
        try:
            for modal in page.query_selector_all(selector):
                if modal.is_visible():
                    return modal
        except Exception:
            continue
    return None


def _scope_has_easy_apply_controls(scope) -> bool:
    """Return True when a modal scope has fields or action buttons worth handling."""
    if _is_easy_apply_limit_modal(scope):
        return True

    try:
        field_count = len(scope.query_selector_all(_FORM_INPUT_SELECTOR))
    except Exception:
        field_count = 0

    if field_count > 0:
        return True

    # SDUI can expose only an attachment file-picker button while Next is
    # disabled. This is still an actionable form step, even without an input.
    if _find_document_upload_buttons(scope):
        return True

    return any((
        _find_easy_apply_action_button(
            scope,
            _EASY_APPLY_NEXT_BUTTON_SELECTORS,
            _EASY_APPLY_NEXT_BUTTON_LABELS,
        ),
        _find_easy_apply_action_button(
            scope,
            _EASY_APPLY_SUBMIT_BUTTON_SELECTORS,
            _EASY_APPLY_SUBMIT_BUTTON_LABELS,
        ),
    ))


def _wait_for_easy_apply_form_ready(page, timeout_ms: int = 8000):
    """Wait until LinkedIn's Easy Apply modal has loaded usable fields or buttons."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    last_modal = None

    while time.monotonic() < deadline:
        modal = _get_active_modal(page)
        if modal:
            last_modal = modal
            if _scope_has_easy_apply_controls(modal):
                return modal
        page.wait_for_timeout(300)

    return last_modal


def _close_easy_apply_modal(page) -> None:
    """Dismiss the Easy Apply modal if it is still open."""
    def _find_button_by_text(scope, labels: set[str]):
        try:
            buttons = scope.query_selector_all("button, input[type='button'], input[type='submit']")
        except Exception:
            return None

        for button in buttons:
            try:
                if not button.is_visible() or not button.is_enabled():
                    continue
                raw_text = button.evaluate(
                    """el => [
                        el.getAttribute('aria-label') || '',
                        el.innerText || '',
                        el.textContent || '',
                        el.value || ''
                    ].join(' ')"""
                )
                normalized_text = _normalize_lookup_text(raw_text)
                if normalized_text in labels:
                    return button
            except Exception:
                continue
        return None

    def _discard_confirmation_if_present() -> bool:
        modal = _find_discard_confirmation_modal(page)
        if not modal:
            return False

        try:
            modal_text = _normalize_lookup_text(_scope_text(modal))
        except Exception:
            modal_text = ""

        if "save this application" not in modal_text and "application will be discarded" not in modal_text:
            return False

        selectors = [
            "button[data-control-name='discard_application_confirm_btn']",
            "button[data-test-dialog-secondary-btn]",
            "button[componentkey]",
            "button[aria-label*='Discard']",
            "button:has-text('Discard')",
            "button:has-text('Discard application')",
        ]
        for selector in selectors:
            try:
                btn = None
                if selector == "button[componentkey]":
                    btn = _find_button_by_text(modal, {"discard", "discard application"})
                else:
                    btn = modal.query_selector(selector) or page.query_selector(selector)
                if btn and btn.is_visible() and btn.is_enabled():
                    if not _click_action_button(btn):
                        continue
                    page.wait_for_timeout(500)
                    return True
            except Exception:
                continue

        btn = _find_button_by_text(modal, {"discard", "discard application"})
        if btn and _click_action_button(btn):
            page.wait_for_timeout(500)
            return True

        return False

    def _modal_is_closed() -> bool:
        try:
            return _get_active_modal(page) is None
        except Exception:
            return True

    modal = _get_active_modal(page)
    if not modal:
        return

    if _discard_confirmation_if_present() or _modal_is_closed():
        return

    selectors = [
        "button[data-test-modal-close-btn]",
        "button[data-test-dialog-close-btn]",
        "button[data-control-name='cancel_application']",
        "button[aria-label='Dismiss']",
        "button[aria-label='Close']",
        "button[aria-label*='Dismiss']",
        "button[aria-label*='Close']",
        "button:has-text('Done')",
        "button:has-text('Dismiss')",
        "button:has-text('Close')",
    ]
    for selector in selectors:
        try:
            btn = modal.query_selector(selector) or page.query_selector(selector)
            if btn and btn.is_visible():
                if not _click_action_button(btn):
                    continue
                page.wait_for_timeout(500)
                if _discard_confirmation_if_present() or _modal_is_closed():
                    return
        except Exception:
            continue

    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            if _discard_confirmation_if_present() or _modal_is_closed():
                return
        except Exception:
            pass


def _is_safety_reminder_modal(scope) -> bool:
    """Return True when LinkedIn shows the pre-apply safety reminder modal."""
    scope_text = _normalize_lookup_text(_scope_text(scope))
    return (
        "job search safety reminder" in scope_text
        or "safety reminder" in scope_text
        or ("continue applying" in scope_text and "review job post" in scope_text)
    )


def _continue_past_safety_reminder(scope, page) -> bool:
    """Click through LinkedIn's pre-apply safety reminder modal."""
    if not _is_safety_reminder_modal(scope):
        return False

    selectors = [
        "#jobs-apply-button-id",
        "button[aria-label*='continue the apply process']",
        "button:has-text('Continue applying')",
    ]
    for selector in selectors:
        try:
            button = scope.query_selector(selector)
            if button and button.is_visible() and button.is_enabled():
                if _click_action_button(button):
                    page.wait_for_timeout(1500)
                    return True
        except Exception:
            continue
    return False


def _element_current_text(element) -> str:
    """Best-effort current value/text for an input-like element."""
    try:
        return (
            element.evaluate(
                """el => {
                    if (el.tagName === 'SELECT') {
                        const opt = el.selectedOptions && el.selectedOptions[0];
                        return (opt && (opt.textContent || opt.value)) || '';
                    }
                    return el.value || el.textContent || '';
                }"""
            ) or ""
        ).strip()
    except Exception:
        return ""


def _extract_field_label(scope, input_elem) -> str:
    """Best-effort field/question label for LinkedIn form elements."""
    field_id = input_elem.get_attribute("id") or ""
    placeholder = input_elem.get_attribute("placeholder") or ""

    try:
        question_text = input_elem.evaluate(
            """el => {
                const choice = el.matches('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]');
                if (!choice) {
                    const labels = [...(el.labels || [])].map(label => label.innerText || label.textContent || '').join(' ').trim();
                    if (labels) return labels;
                    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\\s+/)
                        .map(id => el.ownerDocument.getElementById(id)?.textContent || '').join(' ').trim();
                    if (labelled) return labelled;
                }
                const fieldset = el.closest('[role="radiogroup"], [role="group"], fieldset');
                if (!fieldset) return '';
                const labelled = (fieldset.getAttribute('aria-labelledby') || '').split(/\\s+/)
                    .map(id => el.ownerDocument.getElementById(id)?.textContent || '').join(' ').trim();
                const legend = fieldset.querySelector('legend');
                const heading = labelled || fieldset.getAttribute('aria-label') || (legend && legend.textContent);
                if (heading) return heading;
                const container = fieldset.parentElement;
                if (!container) return '';
                const question = [...container.children].find((child) => child !== fieldset && (child.innerText || '').trim());
                return question ? (question.innerText || question.textContent || '') : '';
            }"""
        ) or ""
        if question_text.strip():
            return question_text.strip()
    except Exception:
        pass

    try:
        legend_text = input_elem.evaluate(
            """el => {
                const fieldset = el.closest('fieldset');
                if (!fieldset) return '';
                const legend = fieldset.querySelector('legend');
                return legend ? (legend.textContent || '') : '';
            }"""
        ) or ""
        if legend_text.strip():
            return legend_text.strip()
    except Exception:
        pass

    if field_id:
        try:
            label = scope.query_selector(f"label[for='{field_id}']")
            if label:
                text = (label.text_content() or "").strip()
                if text:
                    return text
        except Exception:
            pass

    try:
        aria_label = input_elem.get_attribute("aria-label") or ""
        if aria_label.strip():
            return aria_label.strip()
    except Exception:
        pass

    try:
        wrapper_label = input_elem.evaluate(
            """el => {
                const wrapper = el.closest('div');
                if (!wrapper) return '';
                const label = wrapper.querySelector('label');
                return label ? (label.textContent || '') : '';
            }"""
        ) or ""
        if wrapper_label.strip():
            return wrapper_label.strip()
    except Exception:
        pass

    return placeholder.strip()


def _scope_text(scope) -> str:
    """Best-effort lowercase text snapshot for the current modal step."""
    try:
        return " ".join((scope.text_content() or "").lower().split())
    except Exception:
        return ""


def _extract_job_detail_text(page) -> str:
    """Best-effort job detail text used for per-job resume language selection."""
    for selector in _JOB_DETAIL_TEXT_SELECTORS:
        try:
            for elem in page.query_selector_all(selector):
                text = " ".join((elem.text_content() or "").split())
                if len(text) >= 120:
                    return text
        except Exception:
            continue

    try:
        return " ".join((page.text_content("body") or "").split())
    except Exception:
        try:
            return " ".join((page.content() or "").split())
        except Exception:
            return ""


def _language_term_score(text: str, terms: tuple[str, ...]) -> int:
    normalized = _normalize_lookup_text(text)
    if not normalized:
        return 0

    padded = f" {normalized} "
    score = 0
    for term in terms:
        normalized_term = _normalize_lookup_text(term)
        if normalized_term:
            score += padded.count(f" {normalized_term} ")
    return score


def _detect_job_language(text: str) -> str:
    """Return 'german', 'english', or '' based on the visible job description text."""
    if not text:
        return ""

    german_score = _language_term_score(text, _GERMAN_LANGUAGE_TERMS)
    english_score = _language_term_score(text, _ENGLISH_LANGUAGE_TERMS)

    if re.search(r"[äöüßÄÖÜ]", text):
        german_score += 2

    if german_score >= english_score + 2 and german_score >= 3:
        return "german"
    if english_score >= german_score + 2 and english_score >= 3:
        return "english"
    return ""


def _configured_language_resume_paths(config_dict: dict) -> dict[str, str]:
    resume_paths = config_dict.get("resume_paths", {})
    if not isinstance(resume_paths, dict):
        resume_paths = {}

    english_path = (
        resume_paths.get("english")
        or resume_paths.get("en")
        or config_dict.get("english_resume_path")
        or ""
    )
    german_path = (
        resume_paths.get("german")
        or resume_paths.get("de")
        or config_dict.get("german_resume_path")
        or ""
    )

    return {
        "english": str(english_path or "").strip(),
        "german": str(german_path or "").strip(),
    }


def _resume_language_detection_enabled(config_dict: dict) -> bool:
    configured_value = config_dict.get("auto_select_resume_by_job_language")
    if configured_value is not None:
        return _config_flag_enabled(configured_value)

    return any(_configured_language_resume_paths(config_dict).values())


def _only_english_jobs_enabled(config_dict: dict) -> bool:
    return _config_flag_enabled(config_dict.get("only_english", False))


def _job_language_allowed(config_dict: dict, detected_language: str) -> bool:
    if not _only_english_jobs_enabled(config_dict):
        return True

    return detected_language == "english"


def _select_resume_path_for_job(config_dict: dict, job_detail_text: str, fallback_resume_path: str) -> tuple[str, str]:
    """Pick a language-specific resume path when configured and confidently detected."""
    if not _resume_language_detection_enabled(config_dict):
        return fallback_resume_path, ""

    resume_paths = _configured_language_resume_paths(config_dict)
    detected_language = _detect_job_language(job_detail_text)

    if detected_language and resume_paths.get(detected_language):
        return resume_paths[detected_language], detected_language

    return fallback_resume_path, detected_language


def _collect_field_questions(scope, inputs=None) -> list[str]:
    """Return unique visible field questions/labels for logging stuck form pages."""
    if inputs is None:
        try:
            inputs = scope.query_selector_all(_FORM_INPUT_SELECTOR)
        except Exception:
            inputs = []

    questions: list[str] = []
    seen = set()
    for input_elem in inputs:
        try:
            field_type = (input_elem.get_attribute("type") or "").lower()
            if field_type in {"hidden", "file"}:
                continue
            label_text = " ".join(_extract_field_label(scope, input_elem).split())
            if not label_text:
                continue
            label_key = _normalize_lookup_text(label_text)
            if not label_key or label_key in seen:
                continue
            seen.add(label_key)
            questions.append(label_text)
        except Exception:
            continue
    return questions


def _form_step_signature(scope, inputs=None) -> str:
    """Build a stable signature for detecting repeated Easy Apply pages."""
    questions = _collect_field_questions(scope, inputs)
    if questions:
        return "|".join(_normalize_lookup_text(question) for question in questions)
    return _scope_text(scope)[:500]


def _log_stuck_form_questions(questions: list[str]) -> None:
    if not questions:
        log.warning("Stuck on repeated Easy Apply page, but no field questions could be extracted")
        return

    log.warning("Stuck on repeated Easy Apply page with these questions:")
    for question in questions[:25]:
        log.warning("  - %s", question)
    if len(questions) > 25:
        log.warning("  ... and %d more", len(questions) - 25)


def _has_upload_format_error(scope) -> bool:
    """Return True when the current step shows the resume upload format error."""
    scope_text = _scope_text(scope)
    return any(text in scope_text for text in _UPLOAD_FORMAT_ERROR_TEXTS)


def _file_accept_tokens(input_elem) -> list[str]:
    accept = (input_elem.get_attribute("accept") or "").strip().lower()
    return [token.strip() for token in accept.split(",") if token.strip()]


def _file_input_context_text(scope, input_elem) -> str:
    """Return nearby upload text, including labels that sit outside the input wrapper."""
    field_id = input_elem.get_attribute("id") or ""
    # React's generated _r_cv_ ID is not a semantic "CV" label.
    descriptive_id = "" if re.fullmatch(r"(?:_r_[A-Za-z0-9]+_|:r[A-Za-z0-9]+:)", field_id) else field_id
    parts: list[str] = [descriptive_id, input_elem.get_attribute("name") or ""]

    if field_id:
        try:
            label = scope.query_selector(f"label[for='{field_id}']")
            if label:
                parts.append(label.text_content() or "")
        except Exception:
            pass

    try:
        parts.append(input_elem.get_attribute("aria-label") or "")
        parts.append(input_elem.evaluate(
            """el => [
                ...[...(el.labels || [])].map(label => label.innerText || label.textContent || ''),
                ...(el.getAttribute('aria-labelledby') || '').split(/\\s+/)
                    .map(id => el.ownerDocument.getElementById(id)?.textContent || ''),
            ].join(' ')"""
        ) or "")
    except Exception:
        pass

    explicit = " ".join(part for part in parts if part).strip()
    normalized = _normalize_lookup_text(explicit)
    if any(token in normalized for token in (
        *_RESUME_UPLOAD_LABEL_TOKENS, *_COVER_LETTER_UPLOAD_LABEL_TOKENS, *_PHOTO_UPLOAD_LABEL_TOKENS,
    )):
        return explicit

    try:
        parts.append(input_elem.evaluate(
            """el => {
                for (let container = el.parentElement; container; container = container.parentElement) {
                    // Never let another upload field's label classify this input.
                    if (container.querySelectorAll('input[type="file"]').length > 1) break;
                    if (container.querySelector('#easyApplyUploadedCoverLetterRef')) return 'cover letter';
                    const text = (container.innerText || container.textContent || '').trim();
                    if (text) return text;
                    if (container.matches('form, [role="dialog"], body, .jobs-document-upload__container')) break;
                }
                return '';
            }"""
        ) or "")
    except Exception:
        pass

    return " ".join(part for part in parts if part).strip()


def _accept_tokens_allow_resume_path(tokens: list[str], resume_path: str) -> bool:
    if not tokens:
        return True

    suffix = Path(resume_path).suffix.lower()
    if suffix in {".pdf", ".doc", ".docx"}:
        mime = {".pdf": "application/pdf", ".doc": "application/msword",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[suffix]
        return any(token in {suffix, mime, "application/*", "*/*"} for token in tokens)

    return True


def _accept_tokens_allow_photo_path(tokens: list[str], photo_path: str) -> bool:
    if not tokens:
        return True

    suffix = Path(photo_path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".gif", ".png", ".webp"}:
        return any(
            token == suffix
            or token in _IMAGE_FILE_ACCEPT_TOKENS
            or token.startswith("image/")
            for token in tokens
        )

    return False


def _accepts_only_images(tokens: list[str]) -> bool:
    return bool(tokens) and all(token.startswith("image/") or token in _IMAGE_FILE_ACCEPT_TOKENS for token in tokens)


def _is_required_upload_input(scope, input_elem) -> bool:
    """Return True when LinkedIn marks a file upload field required or invalid."""
    try:
        if input_elem.get_attribute("required") is not None:
            return True
    except Exception:
        pass

    try:
        return bool(input_elem.evaluate(
            """el => {
                const container = el.closest(
                    '[data-test-form-element], .full-width, fieldset, form, .jobs-document-upload__container'
                );
                const root = container || el.parentElement;
                if (!root) return false;
                const text = (root.innerText || root.textContent || '').toLowerCase();
                if (text.includes('file is required') || text.includes('a file is required')) return true;
                if (root.querySelector('[role="alert"], .artdeco-inline-feedback--error')) return true;
                return !!root.querySelector(
                    '.jobs-document-upload__title--is-required, [data-test-form-element] [class*="required"]'
                );
            }"""
        ))
    except Exception:
        return False


def _is_resume_upload_input(scope, input_elem, resume_path: str) -> bool:
    """Recognize the actual résumé field, distinct from other PDF attachments."""
    tokens = _file_accept_tokens(input_elem)
    context_text = _file_input_context_text(scope, input_elem)
    normalized_context = _normalize_lookup_text(context_text)

    if any(token in normalized_context for token in _COVER_LETTER_UPLOAD_LABEL_TOKENS):
        return False

    if _accepts_only_images(tokens):
        log.info("Skipping file upload field because it only accepts images: %s", context_text or ",".join(tokens))
        return False

    if any(token in normalized_context for token in _NON_RESUME_UPLOAD_LABEL_TOKENS):
        log.info("Skipping non-resume upload field: %s", context_text or "<unknown>")
        return False

    if not _accept_tokens_allow_resume_path(tokens, resume_path):
        log.info(
            "Skipping file upload field because accept='%s' does not allow %s",
            ",".join(tokens),
            Path(resume_path).suffix.lower() or "this resume file",
        )
        return False

    # A transcript, certificate, or other PDF must not satisfy the separate
    # fresh-résumé requirement just because it accepts the same file format.
    return any(_contains_lookup_phrase(normalized_context, token) for token in _RESUME_UPLOAD_LABEL_TOKENS)


def _is_photo_upload_input(scope, input_elem, photo_path: str = "") -> bool:
    """Return True for photo/image upload fields that are suitable for a configured photo."""
    tokens = _file_accept_tokens(input_elem)
    context_text = _file_input_context_text(scope, input_elem)
    normalized_context = _normalize_lookup_text(context_text)

    if not _accepts_only_images(tokens):
        return False

    if not any(token in normalized_context for token in _PHOTO_UPLOAD_LABEL_TOKENS):
        return False

    if photo_path and not _accept_tokens_allow_photo_path(tokens, photo_path):
        log.info(
            "Skipping photo upload field because accept='%s' does not allow %s",
            ",".join(tokens),
            Path(photo_path).suffix.lower() or "this photo file",
        )
        return False

    return True


def _find_resume_upload_input(scope, resume_path: str):
    try:
        file_inputs = scope.query_selector_all("input[type='file']")
    except Exception:
        return None

    for input_elem in file_inputs:
        if _is_resume_upload_input(scope, input_elem, resume_path):
            return input_elem
    return None


def _find_cover_letter_upload_input(scope):
    for input_elem in scope.query_selector_all("input[type='file']"):
        context = _normalize_lookup_text(_file_input_context_text(scope, input_elem))
        if any(token in context for token in _COVER_LETTER_UPLOAD_LABEL_TOKENS):
            return input_elem
    return None


def _find_cover_letter_upload_button(scope):
    for button in _find_document_upload_buttons(scope):
        label = _upload_button_label(button)
        if any(token in _normalize_lookup_text(label) for token in _COVER_LETTER_UPLOAD_LABEL_TOKENS):
            return button
    return None


def _upload_button_label(button) -> str:
    return button.evaluate(
        """el => [el.getAttribute('aria-label') || '', el.innerText || el.textContent || '',
                  el.value || ''].join(' ')"""
    ) or ""


def _find_document_upload_buttons(scope) -> list:
    """Find file-picker controls by their action, not the attachment's name."""
    buttons = []
    for button in scope.query_selector_all("button, [role='button'], input[type='button']"):
        if not button.is_visible() or not button.is_enabled():
            continue
        normalized = _normalize_lookup_text(_upload_button_label(button))
        if any(_contains_lookup_phrase(normalized, token) for token in (
            "upload", "hochladen", "téléverser", "televerser", "joindre", "attach", "browse",
            "choose file", "select file", "change file", "replace file", "add file", "add document",
            "add attachment", "datei auswählen", "datei auswahlen", "datei wählen", "datei hinzufugen",
        )) or ("add" in normalized.split() and any(
            token in normalized for token in (*_RESUME_UPLOAD_LABEL_TOKENS, *_COVER_LETTER_UPLOAD_LABEL_TOKENS)
        )):
            buttons.append(button)
    return buttons


def _upload_control_key(scope, control) -> str:
    """Identify a field across retries without depending on React's generated IDs."""
    return control.evaluate(
        """(el, scope) => {
            const path = [];
            for (let node = el; node && node !== scope; node = node.parentElement) {
                const siblings = node.parentElement ? [...node.parentElement.children] : [node];
                path.unshift(node.tagName + ':' + siblings.filter(n => n.tagName === node.tagName).indexOf(node));
            }
            const heading = scope.querySelector('h1, h2, h3, [role="heading"]')?.textContent || '';
            const isFile = el.matches('input[type="file"]');
            let question = '';
            for (let parent = el.parentElement; parent; parent = parent.parentElement) {
                const title = [...parent.children].filter(node =>
                    node.matches('legend, h2, h3, p, [role="heading"]') &&
                    (node.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)).at(-1);
                if (title) { question = title.textContent || ''; break; }
                if (parent === scope) break;
            }
            let label = el.getAttribute('aria-label') || (isFile
                ? [...(el.labels || [])].map(n => n.textContent || '').join(' ') || el.name
                : question ? '' : el.innerText || el.textContent || el.value || '');
            // Selected filenames are state, not part of the question's identity.
            for (const file of el.files || []) label = label.replaceAll(file.name, '');
            return JSON.stringify([isFile ? 'file' : 'button', heading.trim(), question.trim(),
                label.trim().replace(/\\s+/g, ' '), path.join('/')]);
        }""", scope,
    )


def _upload_button_input(button):
    """Resolve a button's own file input without crossing into another upload field."""
    handle = button.evaluate_handle(
        """el => {
            for (const id of [el.getAttribute('for'), el.getAttribute('aria-controls')]) {
                const input = id && el.ownerDocument.getElementById(id);
                if (input?.matches('input[type="file"]')) return input;
            }
            for (let parent = el.parentElement; parent; parent = parent.parentElement) {
                if (parent.matches('form, [role="dialog"], body')) break;
                const inputs = [...parent.querySelectorAll('input[type="file"]')];
                if (inputs.length > 1) return null;
                if (inputs.length === 1) return inputs[0];
            }
            return null;
        }"""
    )
    return handle.as_element()


def _upload_application_attachments(
    page, scope, resume_path: str, photo_path: str, completed: set[str],
) -> tuple[bool, bool]:
    """Upload the current CV for every document request; return success and fresh-CV status."""
    resume_uploaded = False
    try:
        # Reacquire after every change: an upload can rerender all remaining fields.
        for _ in range(50):
            scope = _get_active_modal(page) or scope
            buttons = _find_document_upload_buttons(scope)
            local_button_inputs = {}
            input_button_counts: dict[str, int] = {}
            for button in buttons:
                linked_input = _upload_button_input(button)
                if linked_input is not None and scope.evaluate("(root, input) => root.contains(input)", linked_input):
                    input_key = _upload_control_key(scope, linked_input)
                    local_button_inputs[_upload_control_key(scope, button)] = input_key
                    input_button_counts[input_key] = input_button_counts.get(input_key, 0) + 1
            pending = None
            for input_elem in scope.query_selector_all("input[type='file']"):
                if not input_elem.is_enabled():
                    continue
                key = _upload_control_key(scope, input_elem)
                # A shared picker needs the individual button click to establish
                # which document slot receives the selected file.
                if input_button_counts.get(key, 0) > 1:
                    continue
                if key not in completed:
                    pending = (input_elem, None, key)
                    break
            if pending is None:
                for button in buttons:
                    key = _upload_control_key(scope, button)
                    if key in completed:
                        continue
                    input_key = local_button_inputs.get(key)
                    if input_key in completed and input_button_counts.get(input_key, 0) == 1:
                        completed.add(key)
                        continue
                    pending = (None, button, key)
                    break
            if pending is None:
                return True, resume_uploaded

            input_elem, button, key = pending
            label = (_upload_button_label(button) if button is not None
                     else _file_input_context_text(scope, input_elem))
            chooser = None
            if input_elem is None:
                with page.expect_file_chooser(timeout=3000) as event:
                    button.click(timeout=1000)
                chooser = event.value
                input_elem = chooser.element

            tokens = _file_accept_tokens(input_elem)
            if _accepts_only_images(tokens):
                # Keep the existing configured-photo behavior for image-only fields.
                if not photo_path:
                    if _is_required_upload_input(scope, input_elem):
                        raise ValueError(f"Image-only upload {label!r} cannot accept the CV and no photo is configured")
                    log.info("Skipping optional image-only upload that cannot accept a CV: %s", label)
                    completed.add(key)
                    continue
                upload_path = photo_path
                if not _accept_tokens_allow_photo_path(tokens, upload_path):
                    raise ValueError(f"Image-only upload {label!r} does not accept the configured photo")
            else:
                upload_path = resume_path
                if not _accept_tokens_allow_resume_path(tokens, upload_path):
                    raise ValueError(f"Upload field {label!r} accept={','.join(tokens)!r} does not allow the selected CV")
            if not upload_path or not Path(upload_path).is_file():
                raise ValueError(f"Upload {label!r} requested, but the configured file does not exist")

            if button is not None:
                normalized = _normalize_lookup_text(label)
                is_resume = upload_path == resume_path and any(
                    _contains_lookup_phrase(normalized, token) for token in _RESUME_UPLOAD_LABEL_TOKENS
                )
            else:
                is_resume = upload_path == resume_path and _is_resume_upload_input(scope, input_elem, resume_path)
            input_key = _upload_control_key(scope, input_elem)
            log.info("Attachment requested (%s); uploading %s", label or "file", upload_path)
            # SDUI may reuse one picker for several document slots. Selecting the
            # identical CV again otherwise need not dispatch a new change event.
            input_elem.evaluate("el => { if (el.files?.length) el.value = ''; }")
            if chooser is not None:
                chooser.set_files(upload_path, timeout=5000)
            else:
                input_elem.set_input_files(upload_path, timeout=5000)
            page.wait_for_timeout(2000)
            if _has_upload_format_error(_get_active_modal(page) or scope):
                raise ValueError(f"Upload {label!r} was rejected by the form")
            completed.update((key, input_key))
            resume_uploaded = resume_uploaded or is_resume
            log.info("Attachment upload SUCCESS: %s", label or "file")
        raise ValueError("Too many attachment controls appeared on one form step")
    except (PlaywrightError, ValueError, OSError) as exc:
        log.error("Attachment upload FAILED: %s", str(exc).splitlines()[0])
        return False, resume_uploaded


def _find_photo_upload_input(scope, photo_path: str = ""):
    try:
        file_inputs = scope.query_selector_all("input[type='file']")
    except Exception:
        return None

    for input_elem in file_inputs:
        if _is_photo_upload_input(scope, input_elem, photo_path):
            return input_elem
    return None


def _has_required_photo_upload_without_file(scope) -> bool:
    try:
        file_inputs = scope.query_selector_all("input[type='file']")
    except Exception:
        return False

    for input_elem in file_inputs:
        try:
            if _is_photo_upload_input(scope, input_elem) and _is_required_upload_input(scope, input_elem):
                return True
        except Exception:
            continue
    return False


def _is_review_submit_step(scope) -> bool:
    """Return True when the Easy Apply modal is on the final review step."""
    scope_text = _scope_text(scope)
    if not any(
        text in scope_text
        for text in [
            "review your application",
            "review application",
            "vérifiez votre candidature",
            "verifiez votre candidature",
            "bewerbung überprüfen",
            "bewerbung uberprufen",
        ]
    ):
        return False

    return _find_easy_apply_action_button(
        scope,
        _EASY_APPLY_SUBMIT_BUTTON_SELECTORS,
        _EASY_APPLY_SUBMIT_BUTTON_LABELS,
    ) is not None


def _find_easy_apply_next_button(scope):
    """Find the next-step button without overriding the final review submit."""
    if _is_review_submit_step(scope):
        return None

    return _find_easy_apply_action_button(
        scope,
        _EASY_APPLY_NEXT_BUTTON_SELECTORS,
        _EASY_APPLY_NEXT_BUTTON_LABELS,
    )


def _uncheck_follow_company_checkbox(scope) -> bool:
    """Uncheck the follow-company checkbox on the review step."""
    checkboxes = scope.query_selector_all("input[type='checkbox']")
    for checkbox in checkboxes:
        try:
            checkbox_id = checkbox.get_attribute("id") or ""
            checkbox_name = (checkbox.get_attribute("name") or "").strip().lower()
            label_text = _choice_option_text(checkbox, "checkbox")
            aria_checked = ""
            label = None
            try:
                aria_checked = (checkbox.evaluate(
                    """el => {
                        const control = el.closest('[role="checkbox"]');
                        return control ? (control.getAttribute('aria-checked') || '') : '';
                    }"""
                ) or "").strip().lower()
            except Exception:
                aria_checked = ""

            if checkbox_id:
                label = scope.query_selector(f"label[for='{checkbox_id}']")
                if label and not label_text:
                    label_text = (label.text_content() or "").strip()

            label_lower = _normalize_lookup_text(label_text)
            should_uncheck = checkbox_id == "follow-company-checkbox" or checkbox_name == "follow-company"
            if not should_uncheck and not any(token in label_lower for token in _FOLLOW_COMPANY_LABEL_TOKENS):
                continue

            if aria_checked == "false":
                return False

            is_checked = aria_checked == "true"
            if not is_checked:
                try:
                    is_checked = checkbox.is_checked()
                except Exception:
                    is_checked = checkbox.get_attribute("checked") is not None

            if not is_checked:
                return False

            if _click_choice_input(scope, checkbox, "checkbox", checked=False):
                return True

            if checkbox_id and label and label.is_visible():
                label.click(timeout=1000)
            else:
                checkbox.uncheck(timeout=1000, force=True)
            return True
        except Exception:
            continue
    return False


def _match_experience_years(label_lower: str, answers: dict) -> str | None:
    years_experience = answers.get("years_experience", {}) or {}
    for tech, years in sorted(years_experience.items(), key=lambda item: len(item[0]), reverse=True):
        if _contains_lookup_phrase(_normalize_lookup_text(label_lower), _normalize_lookup_text(tech)):
            return str(years)
    return None


def _match_language_level(label_lower: str, answers: dict) -> str | None:
    """Resolve language proficiency questions from *_level keys in config."""
    for key, value in answers.items():
        if not key.endswith("_level") or not value:
            continue
        language = key[:-6].replace("_", " ").lower().strip()
        if language and language in label_lower:
            return str(value)
    return None


def _infer_threshold_yes_no(label_lower: str, profile: dict, answers: dict) -> str | None:
    """Infer yes/no answers for threshold and location screening questions."""
    years_match = re.search(r"at least\s+(\d+)\s+years?", label_lower)
    if years_match:
        required_years = int(years_match.group(1))
        matched_years = _match_experience_years(label_lower, answers)
        if matched_years is not None:
            try:
                return "Yes" if float(matched_years) >= required_years else "No"
            except ValueError:
                pass

    location_blob = " ".join(
        str(x).lower()
        for x in [
            profile.get("city", ""),
            profile.get("country", ""),
            answers.get("location", ""),
        ]
        if x
    )
    if any(x in label_lower for x in ["live in germany", "inside the european union", "in the eu", "within the eu"]):
        if any(x in location_blob for x in ["berlin", "germany", "deutschland"]):
            return "Yes"

    return None


def _safe_years_value(years: str | None) -> float | None:
    try:
        return float(str(years))
    except Exception:
        return None


def _normalize_lookup_text(value: str) -> str:
    """Normalize labels/config keys for loose localized matching."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    normalized = normalized.replace("_", " ").replace("-", " ")
    normalized = re.sub(r"[^\w\s+]", " ", normalized)
    return " ".join(normalized.split())


def _contains_lookup_phrase(text: str, phrase: str) -> bool:
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def _lookup_direct_mapping(label_text: str, mapping: dict) -> str | None:
    """Return a scalar config value when the field label matches a config key."""
    label_norm = _normalize_lookup_text(label_text)
    if not label_norm:
        return None

    best_value = None
    best_score = -1
    for key, value in (mapping or {}).items():
        if isinstance(value, (dict, list)) or value in (None, ""):
            continue

        key_norm = _normalize_lookup_text(key)
        if not key_norm:
            continue

        score = -1
        if label_norm == key_norm:
            score = 300 + len(key_norm)
        elif _contains_lookup_phrase(label_norm, key_norm):
            score = 200 + len(key_norm)
        elif _contains_lookup_phrase(key_norm, label_norm):
            score = 100 + len(label_norm)

        if score > best_score:
            best_score = score
            best_value = str(value)

    return best_value if best_score >= 0 else None


def _lookup_salary_answer(answers: dict) -> str | None:
    """Return a configured compensation answer for loosely worded salary fields."""
    salary_tokens = ("salary", "compensation", "gehalt", "vergütung", "verguetung")
    for key, value in (answers or {}).items():
        if isinstance(value, (dict, list)) or value in (None, ""):
            continue
        key_norm = _normalize_lookup_text(key)
        if any(token in key_norm for token in salary_tokens):
            return str(value)
    return None


def _fallback_text_answer(label_text: str, profile: dict, answers: dict) -> str:
    """Use the requested simple default for unmatched text fields."""
    return "a"


def _infer_question_answer(label_text: str, profile: dict, answers: dict) -> str | None:
    """Infer an answer from config for common LinkedIn screening questions."""
    label_lower = " ".join(label_text.lower().split())

    direct_profile_value = _lookup_direct_mapping(label_text, profile)
    if direct_profile_value is not None:
        return direct_profile_value

    direct_answer_value = _lookup_direct_mapping(label_text, answers)
    if direct_answer_value is not None:
        return direct_answer_value

    if any(x in label_lower for x in _PHONE_COUNTRY_LABEL_TOKENS) or (
        any(x in label_lower for x in _PHONE_NUMBER_LABEL_TOKENS)
        and any(x in label_lower for x in ["country", "code", "dial", "vorwahl", "pays", "indicatif"])
    ):
        return str(profile.get("phone_country_code", "+44"))
    if any(x in label_lower for x in _PHONE_NUMBER_LABEL_TOKENS) and "city" not in label_lower:
        return profile.get("phone_number")
    if any(x in label_lower for x in ["first name", "given name", "vorname", "prénom", "prenom"]):
        return profile.get("first_name")
    if any(x in label_lower for x in ["last name", "family name", "surname", "nachname", "nom"]):
        return profile.get("last_name")
    if any(x in label_lower for x in ["email", "e-mail"]):
        return profile.get("email")
    if any(x in label_lower for x in ["salary", "compensation", "base salary", "gehalt", "vergütung", "verguetung"]):
        return _lookup_salary_answer(answers)
    if any(x in label_lower for x in ["city", "current location", "current city", "where are you located", "stadt", "wohnort", "ville"]) and "phone" not in label_lower:
        return profile.get("city")
    if "visa" in label_lower or "sponsorship" in label_lower:
        return answers.get("visa_sponsorship", "No")
    if any(x in label_lower for x in [
        "authorized to work",
        "work authorization",
        "legally authorized",
        "right to work",
        "legally work",
        "rechtlich befugt",
        "arbeitsberechtigt",
        "arbeitserlaubnis",
        "in diesem land zu arbeiten",
    ]):
        return answers.get("authorized_to_work", "Yes")
    if any(x in label_lower for x in [
        "onsite",
        "remote",
        "hybrid",
        "vor ort",
        "abwechselnd",
        "mobil",
    ]):
        return answers.get("onsite") or "Yes"
    if "financial services" in label_lower or "fintech" in label_lower:
        quant_years = _safe_years_value((answers.get("years_experience", {}) or {}).get("Quantitative Finance"))
        return "Yes" if quant_years and quant_years > 0 else "No"
    if "oltp" in label_lower:
        return "No"
    if "llm frameworks" in label_lower or "langchain" in label_lower:
        llm_years = _safe_years_value((answers.get("years_experience", {}) or {}).get("LLMs / Generative AI"))
        return "Experimented personally" if llm_years and llm_years > 0 else "No experience"
    if "based in berlin" in label_lower or "open to relocating" in label_lower:
        city = str(profile.get("city", "")).lower()
        if "berlin" in city:
            return "Currently based in Berlin"
        if any(x in city for x in ["germany", "deutschland"]):
            return "Based in Germany (or EU) and open to relocating to Berlin"

    threshold_answer = _infer_threshold_yes_no(label_lower, profile, answers)
    if threshold_answer is not None:
        return threshold_answer

    language_level = _match_language_level(label_lower, answers)
    if language_level is not None:
        return language_level

    experience_years = _match_experience_years(label_lower, answers)
    if experience_years is not None:
        return experience_years

    if any(x in label_lower for x in [
        "willing to move forward", "would you like to proceed", "are you interested",
        "are you willing", "can you start", "are you available",
        "konnen sie sich vorstellen", "können sie sich vorstellen",
        "sind sie bereit", "waren sie bereit", "wären sie bereit",
    ]):
        return "Yes"

    return None


def _country_code_option_matches(candidate: str, option_text: str, option_value: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    normalized = f"{option_text} {option_value}".lower()

    if candidate and (option_text.strip() == candidate or option_value.strip() == candidate):
        return True
    if digits and (option_text.strip() == digits or option_value.strip() == digits):
        return True
    if digits and re.search(rf"(^|\D){re.escape(digits)}($|\D)", normalized):
        return True
    return False


def _extract_country_code_digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _extract_country_name(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\(\+\d+\)", "", text).strip()
    return _normalize_lookup_text(text)


def _country_code_profile_candidates(profile: dict) -> list[str]:
    keys = [
        "phone_country_dropdown",
        "phone_country",
        "phone_country_name",
        "phone_country_code",
        "Country Code",
        "Phone Country",
        "Phone Country Code",
        "Landesvorwahl",
    ]
    values: list[str] = []
    for key in keys:
        value = profile.get(key)
        if not value:
            continue
        value_str = str(value).strip()
        if value_str and value_str not in values:
            values.append(value_str)
    return values


def _resolve_country_code_option(input_elem, profile: dict) -> str | None:
    candidates = _collect_select_candidates(input_elem)
    if not candidates:
        return None

    profile_values = _country_code_profile_candidates(profile)
    if not profile_values:
        return None

    exact_targets = {_normalize_lookup_text(value) for value in profile_values if value}
    name_targets = {_extract_country_name(value) for value in profile_values if _extract_country_name(value)}
    digit_targets = {_extract_country_code_digits(value) for value in profile_values if _extract_country_code_digits(value)}

    # 1. Exact full-option match, e.g. "United Kingdom (+44)".
    for text, value in candidates:
        option_targets = {
            _normalize_lookup_text(text),
            _normalize_lookup_text(value),
        }
        if option_targets & exact_targets:
            return value

    # 2. Exact country-name match, e.g. "United Kingdom".
    name_matches: list[str] = []
    for text, value in candidates:
        option_name = _extract_country_name(text) or _extract_country_name(value)
        if option_name and option_name in name_targets:
            name_matches.append(value)
    if len(name_matches) == 1:
        return name_matches[0]

    # 3. Dial-code-only match, but only when unambiguous.
    digit_matches: list[str] = []
    for text, value in candidates:
        if any(_country_code_option_matches(digits, text, value) for digits in digit_targets):
            digit_matches.append(value)
    unique_digit_matches = list(dict.fromkeys(digit_matches))
    if len(unique_digit_matches) == 1:
        return unique_digit_matches[0]

    if unique_digit_matches:
        log.warning(
            "Phone country code is ambiguous for config values %s; matching options: %s. "
            "Set profile.phone_country to the exact country name or full dropdown label.",
            profile_values,
            [text for text, value in candidates if value in unique_digit_matches],
        )

    return None


def _collect_select_candidates(input_elem) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    try:
        options = input_elem.query_selector_all("option")
    except Exception:
        return candidates

    for opt in options:
        if not opt.is_enabled():
            continue
        text = (opt.text_content() or "").strip()
        value = (opt.get_attribute("value") or "").strip()
        if not text or _is_choice_placeholder(text):
            continue
        candidates.append((text, value or text))
    return candidates


def _is_choice_placeholder(text: str) -> bool:
    normalized = _normalize_lookup_text(text)
    # "I choose not to identify" is a real answer, not a "Choose" placeholder.
    return not normalized or normalized in {
        _normalize_lookup_text(placeholder) for placeholder in _SELECT_PLACEHOLDER_TEXTS
    }


def _is_custom_choice_control(input_elem, label_text: str) -> bool:
    role = (input_elem.get_attribute("role") or "").lower()
    popup = (input_elem.get_attribute("aria-haspopup") or "").lower()
    if role == "listbox":
        return True
    if role != "combobox" and popup != "listbox":
        return False
    tag = input_elem.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        return False
    # Keep editable address/phone typeaheads on their existing profile-fill path.
    if tag == "input" and input_elem.get_attribute("readonly") is None and any(
        token in label_text.lower() for token in ("city", "location", "based", "phone", "country code")
    ):
        return False
    return True


def _custom_choice_options(input_elem) -> list:
    """Read this control's visible options, including portals outside the modal."""
    handle = input_elem.evaluate_handle(
        """el => {
            const doc = el.ownerDocument;
            const visible = node => node && node.getClientRects().length > 0 &&
                getComputedStyle(node).visibility !== 'hidden';
            const control = el.closest('[role="combobox"], [aria-haspopup="listbox"]') || el;
            const ids = [control, el].flatMap(node => ['aria-controls', 'aria-owns']
                .flatMap(attr => (node.getAttribute(attr) || '').split(/\\s+/).filter(Boolean)));
            let roots = el.matches('[role="listbox"]') ? [el] : ids.map(id => doc.getElementById(id));
            roots = roots.filter(visible);
            if (!roots.length && !ids.length) {
                roots = [...control.querySelectorAll('[role="listbox"]')].filter(visible);
                if (!roots.length) {
                    const open = [...doc.querySelectorAll('[role="listbox"]')].filter(visible);
                    if (open.length === 1) roots = open;
                }
            }
            return [...new Set(roots.flatMap(root => [...root.querySelectorAll('[role="option"], option')]))]
                .filter(option => visible(option) && !option.disabled && option.getAttribute('aria-disabled') !== 'true');
        }"""
    )
    try:
        return [prop.as_element() for prop in handle.get_properties().values() if prop.as_element() is not None]
    finally:
        handle.dispose()


def _custom_choice_text(input_elem) -> str:
    return input_elem.evaluate(
        """el => {
            if (!el.isConnected) return '';
            if (el.matches('[role="listbox"]')) {
                return [...el.querySelectorAll('[aria-selected="true"]')].map(node => node.textContent || '').join(' ');
            }
            if (el.value) return el.value;
            const copy = el.cloneNode(true);
            for (const popup of copy.querySelectorAll('[role="listbox"]')) popup.remove();
            return el.getAttribute('aria-valuetext') || copy.textContent || '';
        }"""
    ) or ""


def _answer_custom_choice(page, scope, input_elem, label_text: str, profile: dict, answers: dict) -> bool:
    """Open, choose and verify a custom dropdown with bounded, short waits."""
    input_id = input_elem.get_attribute("id") or ""

    def current_control():
        return (page.query_selector(f'[id={json.dumps(input_id)}]') if input_id else None) or input_elem

    try:
        if not input_elem.is_visible() or not input_elem.is_enabled():
            return False
        options = _custom_choice_options(input_elem)
        if not options:
            input_elem.click(timeout=500)
            for _ in range(8):
                options = _custom_choice_options(current_control())
                if options:
                    break
                page.wait_for_timeout(50)
        candidates = [
            ((option.text_content() or option.get_attribute("aria-label") or "").strip(), str(index))
            for index, option in enumerate(options)
        ]
        candidates = [(text, index) for text, index in candidates if not _is_choice_placeholder(text)]
        current_text = _normalize_lookup_text(_custom_choice_text(current_control()))
        current = next((index for text, index in candidates if _normalize_lookup_text(text) == current_text), None)
        inferred = _infer_question_answer(label_text, profile, answers)
        selected = _best_option_value(label_text, candidates, inferred, profile, answers, current_value=current)
        if selected is None:
            log.warning("Choice answer FAILED: question=%r; no enabled dropdown options found", label_text)
            return False
        option = options[int(selected)]
        selected_text = next(text for text, index in candidates if index == selected)
        option.click(timeout=500)
        selected_norm = _normalize_lookup_text(selected_text)
        success = False
        for _ in range(4):
            actual = _normalize_lookup_text(_custom_choice_text(current_control()))
            if (actual and (actual == selected_norm or _contains_lookup_phrase(actual, selected_norm))) or (
                option.get_attribute("aria-selected") == "true"
            ):
                success = True
                break
            page.wait_for_timeout(25)
        log.info("Choice answer %s: question=%r; option=%r", "SUCCESS" if success else "FAILED", label_text, selected_text)
        return success
    except PlaywrightError as exc:
        log.warning("Choice answer FAILED: question=%r; %s", label_text, exc.message.splitlines()[0])
        return False


def _choose_dropdown_value(input_elem, label_text: str, profile: dict, answers: dict) -> str | None:
    """Choose a deterministic dropdown value from config or scoring heuristics."""
    label_lower = label_text.lower()
    candidates = _collect_select_candidates(input_elem)
    if not candidates:
        return None

    if any(x in label_lower for x in _PHONE_COUNTRY_LABEL_TOKENS) or (
        "phone" in label_lower and "country" in label_lower
    ):
        resolved = _resolve_country_code_option(input_elem, profile)
        if resolved:
            return resolved

    inferred_value = _infer_question_answer(label_text, profile, answers)
    if inferred_value:
        inferred_lower = str(inferred_value).lower().strip()
        for text, value in candidates:
            haystack = f"{text} {value}".lower()
            if inferred_lower and inferred_lower in haystack:
                return value

    return _fallback_select_value(input_elem, label_text, profile, answers)


def _is_numeric_input(input_elem) -> bool:
    input_type = (input_elem.get_attribute("type") or "").lower()
    mode = (input_elem.get_attribute("inputmode") or "").lower()
    pattern = input_elem.get_attribute("pattern") or ""
    return input_type == "number" or mode in {"numeric", "decimal"} or "\\d" in pattern or "[0-9]" in pattern


def _text_field_answer(input_elem, label_text: str, profile: dict, answers: dict) -> str:
    value = _infer_question_answer(label_text, profile, answers)
    numeric = _is_numeric_input(input_elem) or bool(re.search(
        r"\b(?:how many|number of|anzahl|wie viele)\b", _normalize_lookup_text(label_text),
    ))
    if numeric:
        # Do not try to type language levels, free text, or 'a' into a numeric
        # control just because an imperfect label matched another config key.
        return str(value).strip() if value is not None and re.fullmatch(r"-?\d+(?:\.\d+)?", str(value).strip()) else "1"
    return str(value) if value is not None else _fallback_text_answer(label_text, profile, answers)


def _fill_text_input(input_elem, text_value: str) -> str:
    """Fill quickly; use '1' if an 'a' fallback is rejected by a numeric input."""
    if text_value == "a" and _is_numeric_input(input_elem):
        text_value = "1"
    try:
        input_elem.fill(text_value, timeout=500)
        return text_value
    except Exception:
        if text_value != "a":
            raise

    input_elem.fill("1", timeout=500)
    return "1"


@dataclass
class _TypeFallbackState:
    tried: set[str] = field(default_factory=set)
    value: str = ""


def _invalid_type_inputs(scope) -> list:
    """Find editable text/number fields with their own visible type-validation feedback."""
    handle = scope.evaluate_handle(
        """(root, messages) => {
            const selector = 'input, textarea';
            const shown = node => {
                const style = getComputedStyle(node);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                    (node.getClientRects().length > 0 || (style.display === 'contents' &&
                        [...node.children].some(child => child.getClientRects().length > 0)));
            };
            return [...root.querySelectorAll(selector)].filter(input => {
                if (!shown(input) || input.disabled || input.readOnly || input.getAttribute('role') === 'combobox') return false;
                if (input.tagName !== 'TEXTAREA' && !['text', 'number', 'tel', 'search', 'email', 'url'].includes(input.type)) return false;
                const texts = [];
                const collect = node => {
                    if (node && shown(node)) texts.push((typeof node.innerText === 'string'
                        ? node.innerText : node.textContent || '').toLowerCase());
                };
                for (const attr of ['aria-describedby', 'aria-errormessage']) {
                    for (const id of (input.getAttribute(attr) || '').split(/\\s+/).filter(Boolean)) {
                        collect(input.ownerDocument.getElementById(id));
                    }
                }
                // A shared form/fieldset error must not mark unrelated inputs invalid.
                for (let parent = input.parentElement; parent && root.contains(parent); parent = parent.parentElement) {
                    if (parent.querySelectorAll('input:not([type="hidden"]):not([type="file"]), select, textarea').length > 1) break;
                    for (const error of parent.querySelectorAll(
                        '[role="alert"], [data-testid="text-input-helper-text"], [data-testid="input-error"], ' +
                        '.artdeco-inline-feedback--error, [aria-live="assertive"]')) collect(error);
                    if (parent === root) break;
                }
                const validity = input.validity;
                const nativeTypeError = validity && (validity.badInput || validity.typeMismatch || validity.patternMismatch ||
                    validity.rangeUnderflow || validity.rangeOverflow || validity.stepMismatch);
                return !!nativeTypeError || texts.some(text => messages.some(message => text.includes(message)));
            });
        }""", _INVALID_TYPE_VALIDATION_TEXTS,
    )
    try:
        return [prop.as_element() for prop in handle.get_properties().values() if prop.as_element() is not None]
    finally:
        handle.dispose()


def _type_fallback_key(scope, input_elem, label_text: str, step_signature: str) -> str:
    """Keep per-field attempts across React ID changes, without mixing different questions."""
    path = input_elem.evaluate(
        """(input, root) => {
            const component = input.closest('[componentkey^="easyApplyFieldFocus_"]');
            if (component) return component.getAttribute('componentkey');
            const path = [];
            for (let node = input; node && node !== root; node = node.parentElement) {
                const siblings = node.parentElement ? [...node.parentElement.children] : [node];
                path.unshift(node.tagName + ':' + siblings.filter(other => other.tagName === node.tagName).indexOf(node));
            }
            return path.join('/');
        }""", scope,
    )
    return json.dumps([step_signature, _normalize_lookup_text(label_text), path])


def _type_fallback_candidates(input_elem, label_text: str, profile: dict, answers: dict) -> list[tuple[str, str]]:
    """Try an applicable configured answer, then the requested numeric/text defaults."""
    inferred = _infer_question_answer(label_text, profile, answers)
    candidates = []
    if inferred is not None:
        candidates.append((str(inferred), "configured"))
    candidates.extend((("1", "number"), ("a", "text")))
    seen = set()
    compatible = []
    for value, source in candidates:
        if value in seen:
            continue
        seen.add(value)
        allowed = input_elem.evaluate(
            """(input, value) => {
                const copy = input.cloneNode();
                copy.value = value;
                // Native type/pattern/range constraints are checked before typing.
                return copy.value === value && (!copy.validity || copy.validity.valid);
            }""", value,
        )
        if allowed:
            compatible.append((value, source))
    return compatible


def _rotate_invalid_type_fields(page, scope, states: dict[str, _TypeFallbackState], profile: dict, answers: dict) -> int:
    """Change each rejected field once per pass; never repeat a failed value."""
    changed = 0
    processed = set()
    step_signature = None
    for _ in range(100):
        scope = _get_active_modal(page) or scope
        invalid = _invalid_type_inputs(scope)
        if not invalid:
            return changed
        if step_signature is None:
            step_signature = _form_step_signature(scope)
        pending = None
        for input_elem in invalid:
            label_text = _extract_field_label(scope, input_elem)
            key = _type_fallback_key(scope, input_elem, label_text, step_signature)
            if key not in processed:
                pending = (input_elem, label_text, key)
                break
        if pending is None:
            return changed
        input_elem, label_text, key = pending
        processed.add(key)
        state = states.setdefault(key, _TypeFallbackState())
        state.tried.add(_element_current_text(input_elem).strip())
        for value, source in _type_fallback_candidates(input_elem, label_text, profile, answers):
            if value in state.tried:
                continue
            state.tried.add(value)
            try:
                input_elem.fill(value, timeout=500)
                input_elem.evaluate("el => el.blur()")
            except PlaywrightError:
                log.info("Invalid-input fallback RETRY: question=%r; rejected %s format", label_text, source)
                continue
            state.value = value
            changed += 1
            log.info("Invalid-input fallback RETRY: question=%r; trying %s format; attempt=%d",
                     label_text, source, len(state.tried) - 1)
            break
        else:
            raise RuntimeError(f"Invalid-input fallback exhausted for {label_text!r}; no untried compatible value remains")
    raise RuntimeError("Too many invalid fields appeared on one Easy Apply step")


def _preserved_type_fallback(scope, input_elem, label_text: str, step_signature: str,
                             states: dict[str, _TypeFallbackState]) -> str | None:
    if not states:
        return None
    state = states.get(_type_fallback_key(scope, input_elem, label_text, step_signature))
    if state is None:
        return None
    return _element_current_text(input_elem).strip() or state.value


def _retry_invalid_type_validation(page, previous_signature: str, button_name: str,
                                   states: dict[str, _TypeFallbackState], profile: dict, answers: dict) -> bool:
    """Repair rejected types on the same step and retry its live Next/Submit control."""
    scope = _get_active_modal(page)
    if scope is None or _form_step_signature(scope) != previous_signature:
        return False
    if _delete_open_history_entries(page, scope):
        scope = _get_active_modal(page) or scope
    changed = _rotate_invalid_type_fields(page, scope, states, profile, answers)
    if not changed:
        return False
    scope = _get_active_modal(page) or scope
    if button_name == "Next":
        button = _find_easy_apply_next_button(scope)
    else:
        button = _find_easy_apply_action_button(scope, _EASY_APPLY_SUBMIT_BUTTON_SELECTORS, _EASY_APPLY_SUBMIT_BUTTON_LABELS)
    if button is None or not _click_action_button(button):
        return False
    page.wait_for_timeout(300)
    return True


def _rewrite_default_a_fields_to_four(scope, replacement: str = "4") -> int:
    """Replace fallback 'a' values (legacy StepStone default: '4')."""
    rewritten = 0
    candidates = scope.query_selector_all("input, textarea")
    for input_elem in candidates:
        try:
            input_type = (input_elem.get_attribute("type") or "text").lower()
            if input_type in {"hidden", "checkbox", "radio", "file", "submit", "button"}:
                continue
            if not input_elem.is_visible() or input_elem.is_disabled():
                continue
            current_value = _element_current_text(input_elem).strip()
            if current_value != "a":
                continue
            input_elem.fill(replacement, timeout=500)
            rewritten += 1
        except Exception:
            continue
    return rewritten


def _rewrite_range_validation_fields_to_four(scope, replacement: str = "4") -> int:
    """Replace values in visible inputs that are blocked by 0..99 validation feedback."""
    rewritten = 0
    candidates = scope.query_selector_all("input, textarea")
    for input_elem in candidates:
        try:
            input_type = (input_elem.get_attribute("type") or "text").lower()
            if input_type in {"hidden", "checkbox", "radio", "file", "submit", "button"}:
                continue
            if not input_elem.is_visible() or input_elem.is_disabled():
                continue

            validation_text = input_elem.evaluate(
                """(node) => {
                    const wrappers = [
                        node.closest('.fb-dash-form-element'),
                        node.closest('.jobs-easy-apply-form-section__grouping'),
                        node.closest('.artdeco-text-input'),
                        node.closest('fieldset'),
                        node.parentElement,
                    ].filter(Boolean);

                    const texts = [];
                    const describedByIds = (node.getAttribute('aria-describedby') || '')
                        .split(/\\s+/)
                        .map((value) => value.trim())
                        .filter(Boolean);

                    for (const id of describedByIds) {
                        const describedBy = node.ownerDocument.getElementById(id);
                        if (describedBy && describedBy.innerText) {
                            texts.push(describedBy.innerText);
                        }
                    }

                    const errorMessageId = node.getAttribute('aria-errormessage');
                    if (errorMessageId) {
                        const errorMessage = node.ownerDocument.getElementById(errorMessageId);
                        if (errorMessage && errorMessage.innerText) {
                            texts.push(errorMessage.innerText);
                        }
                    }

                    for (const wrapper of wrappers) {
                        if (wrapper.innerText) {
                            texts.push(wrapper.innerText);
                        }
                    }

                    return texts.join('\\n').toLowerCase();
                }"""
            )
            if not any(text in validation_text for text in _NUMERIC_RANGE_VALIDATION_TEXTS):
                continue

            current_value = _element_current_text(input_elem).strip()
            if current_value == replacement:
                continue

            input_elem.fill(replacement, timeout=500)
            rewritten += 1
        except Exception:
            continue
    return rewritten


def _retry_numeric_validation_with_one(page, previous_scope_text: str, button, button_name: str) -> bool:
    """Retry the current step when it appears blocked by a 0..99 numeric validation."""
    modal = _get_active_modal(page)
    scope = modal or page
    current_scope_text = _scope_text(scope)
    if current_scope_text != previous_scope_text:
        return False
    if "0" not in current_scope_text or "99" not in current_scope_text:
        return False

    rewritten = _rewrite_range_validation_fields_to_four(scope, replacement="1")
    if rewritten <= 0:
        rewritten = _rewrite_default_a_fields_to_four(scope, replacement="1")
    if rewritten <= 0:
        return False

    log.info(
        "Step unchanged after %s and 0/99 validation detected; rewrote %d field(s) to '1'",
        button_name,
        rewritten,
    )
    page.wait_for_timeout(300)
    if not _click_action_button(button):
        return False
    page.wait_for_timeout(2000)
    return True


def _score_option(label_text: str, option_text: str, inferred_value: str | None, profile: dict, answers: dict) -> int:
    label_lower = " ".join(label_text.lower().split())
    label_norm = _normalize_lookup_text(label_text)
    option_lower = " ".join(option_text.lower().split())
    option_norm = _normalize_lookup_text(option_text)
    score = 0

    if inferred_value:
        inferred_lower = str(inferred_value).lower()
        inferred_norm = _normalize_lookup_text(inferred_value)
        if option_norm == inferred_norm:
            score += 120
        elif inferred_norm and inferred_norm in option_norm:
            score += 90
        if inferred_norm in _YES_OPTION_LABELS and option_norm in _YES_OPTION_LABELS:
            score += 130
        if inferred_norm in _NO_OPTION_LABELS and option_norm in _NO_OPTION_LABELS:
            score += 130

        numeric_value = _safe_years_value(str(inferred_value))
        if numeric_value is not None:
            year_range = _parse_year_range(option_text)
            if year_range and year_range[0] <= numeric_value <= year_range[1]:
                score += 110

    city = str(profile.get("city", "")).lower()
    phone_country = str(profile.get("phone_country_code", ""))
    authorized = str(answers.get("authorized_to_work", "")).lower()
    sponsorship = str(answers.get("visa_sponsorship", "")).lower()

    if any(x in label_lower for x in ["location", "city", "where are you located", "current location"]):
        if city and city in option_lower:
            score += 100
        if "berlin" in city and ("berlin" in option_lower or "germany" in option_lower):
            score += 80

    if any(x in label_lower for x in _PHONE_COUNTRY_LABEL_TOKENS) or ("phone" in label_lower and "country" in label_lower):
        if _country_code_option_matches(phone_country, option_text, option_text):
            score += 120

    if any(x in label_lower for x in ["visa", "sponsorship"]):
        if sponsorship == "no":
            if option_norm in _NO_OPTION_LABELS or "do not require" in option_lower or "no sponsorship" in option_lower:
                score += 120
            if "sponsorship" in option_lower and any(x in option_lower for x in ["yes", "require", "need"]):
                score -= 120
        elif sponsorship == "yes":
            if option_norm in _YES_OPTION_LABELS or "require" in option_lower or "need sponsorship" in option_lower:
                score += 120

    if any(x in label_norm for x in [
        "authorized to work",
        "work authorization",
        "legally authorized",
        "right to work",
        "legally work",
        "rechtlich befugt",
        "arbeitsberechtigt",
        "arbeitserlaubnis",
        "in diesem land zu arbeiten",
    ]):
        if authorized == "yes":
            if option_norm in _YES_OPTION_LABELS:
                score += 110
            if any(x in option_lower for x in ["eu", "germany", "german", "citizen", "citizenship", "permanent", "authorized", "work permit"]):
                score += 95
            if option_norm in _NO_OPTION_LABELS or any(x in option_lower for x in ["need sponsorship", "require sponsorship", "none"]):
                score -= 120
        elif authorized == "no":
            if option_norm in _NO_OPTION_LABELS:
                score += 110

    if any(x in label_norm for x in [
        "open to relocating",
        "based in berlin",
        "willing",
        "konnen sie sich vorstellen",
        "sind sie bereit",
        "abwechselnd",
        "vor ort",
        "remote",
        "hybrid",
    ]):
        if option_norm in _YES_OPTION_LABELS or any(x in option_lower for x in ["open to relocating", "berlin", "germany"]):
            score += 80
        if "outside" in option_lower:
            score -= 30

    if "email" in label_lower and profile.get("email") and str(profile.get("email")).lower() in option_lower:
        score += 120

    if "no experience" in option_lower or option_lower == "none":
        score -= 10

    return score


def _best_option_value(label_text: str, candidates: list[tuple[str, str]], inferred_value: str | None,
                       profile: dict, answers: dict, *, current_value: str | None = None) -> str | None:
    if not candidates:
        return None

    best_text, best_value = candidates[0]
    best_score = _score_option(label_text, best_text, inferred_value, profile, answers)
    for text, value in candidates[1:]:
        score = _score_option(label_text, text, inferred_value, profile, answers)
        if score > best_score:
            best_text, best_value, best_score = text, value, score
    if best_score <= 0:
        return _fallback_choice_value(candidates, current_value=current_value)
    return best_value


def _fallback_choice_value(candidates: list[tuple[str, str]], *, current_value: str | None = None) -> str | None:
    """Prefer Yes, then Ja; otherwise keep an answer or pick a random option."""
    if not candidates:
        return None

    for preferred in ("yes", "ja"):
        for text, value in candidates:
            if preferred in {_normalize_lookup_text(text), _normalize_lookup_text(value)}:
                return value

    if current_value is not None and any(value == current_value for _, value in candidates):
        return current_value
    return random_module.choice(candidates)[1]


def _choice_option_text(input_elem, role: str) -> str:
    """Read the option label, not an opaque input value or the whole question."""
    try:
        return (input_elem.evaluate(
            """(el, role) => {
                const control = el.closest(`[role="${role}"]`);
                const labels = [...(el.labels || [])].map(label => label.innerText || label.textContent || '').join(' ').trim();
                const labelled = (el.getAttribute('aria-labelledby') || '').split(/\\s+/)
                    .map(id => el.ownerDocument.getElementById(id)?.textContent || '').join(' ').trim();
                const wrapper = el.closest('label');
                const explicit = labels || labelled || el.getAttribute('aria-label') ||
                    (control && (control.innerText || control.textContent || '').trim()) ||
                    (wrapper && (wrapper.innerText || wrapper.textContent || '').trim()) || '';
                if (explicit) return explicit;
                // SDUI can put the text in a sibling <p> while the associated
                // <label> is an empty visual checkbox/radio proxy. Stop climbing
                // before another choice is included, or we reach the question.
                const boundary = el.closest('fieldset, [role="radiogroup"], [role="group"]');
                const selector = `input[type="${role}"], [role="${role}"]:not(:has(input[type="${role}"]))`;
                for (let row = el.parentElement; row && row !== boundary; row = row.parentElement) {
                    if (row.matches('form, [role="dialog"], body') || row.querySelectorAll(selector).length > 1) break;
                    const text = (row.innerText || row.textContent || '').trim();
                    if (text) return text;
                }
                return '';
            }""",
            role,
        ) or "").strip()
    except Exception:
        return ""


def _choice_is_checked(input_elem) -> bool | None:
    """Read live UI state; SDUI's ARIA wrapper overrides stale native state."""
    try:
        state = input_elem.evaluate(
            """el => {
                if (!el.isConnected) return 'detached';
                const control = el.closest('[role="radio"], [role="checkbox"]');
                return control ? (control.getAttribute('aria-checked') || '') : '';
            }"""
        )
        if state in {"true", "false"}:
            return state == "true"
        if state == "detached":
            return None
        return input_elem.is_checked()
    except Exception:
        return None


def _click_choice_input(scope, input_elem, role: str, *, checked: bool = True) -> bool:
    """Click a choice and verify it, with no default 30-second locator waits."""
    input_id = input_elem.get_attribute("id") or ""
    if input_id:
        input_elem = scope.query_selector(f'[id={json.dumps(input_id)}]') or input_elem

    def current_state():
        # A controlled form can replace the input during its click handler.
        current = scope.query_selector(f'[id={json.dumps(input_id)}]') if input_id else None
        return _choice_is_checked(current or input_elem)

    if current_state() is checked:
        return True

    try:
        input_elem.evaluate(
            """(el, role) => {
                if (!el.isConnected || el.disabled) return false;
                const control = el.closest(`[role="${role}"]`);
                const target = control || el.labels?.[0] || el.closest('label') || el;
                if (target.getAttribute('aria-disabled') === 'true') return false;
                target.click();
                return true;
            }""",
            role,
        )
        if current_state() is checked:
            return True
    except Exception:
        pass

    # Some components require a real pointer click, rather than HTMLElement.click.
    # Resolve from the existing handle immediately; never wait on a missing locator.
    try:
        target_handle = input_elem.evaluate_handle(
            """(el, role) => el.closest(`[role="${role}"]`) || el.labels?.[0] || el.closest('label') || el""",
            role,
        )
        try:
            target = target_handle.as_element()
            if target and target.is_visible():
                target.click(timeout=500)
        finally:
            target_handle.dispose()
        if current_state() is checked:
            return True
    except Exception:
        pass

    try:
        input_elem.set_checked(checked, timeout=500, force=True)
        return current_state() is checked
    except Exception:
        return False


def _choice_question_options(input_elem, role: str) -> tuple[str, list]:
    """Find this question's peers, including unnamed native and ARIA choices.

    All lookups are immediate and anchored on a real element. No DOM tokens,
    classifier, polling, or long locator timeouts are used.
    """
    handle = None
    try:
        handle = input_elem.evaluate_handle(
            """(el, role) => {
                const selector = `input[type="${role}"], [role="${role}"]:not(:has(input[type="${role}"]))`;
                const controls = root => [...root.querySelectorAll(selector)];
                const group = el.closest(role === 'radio' ? '[role="radiogroup"], fieldset' : '[role="group"], fieldset');
                const name = el.getAttribute('name');
                let options;
                if (role === 'checkbox' && el.required) {
                    // Native required checkboxes (e.g. independent consents)
                    // must each be checked, even inside a broader fieldset.
                    options = [el];
                } else if (group) {
                    options = controls(group);
                    // A broad fieldset can contain several named questions.
                    if (role === 'radio' && name && !group.matches('[role="radiogroup"]') && !group.querySelector('legend') &&
                        options.some(option => option.name && option.name !== name)) {
                        options = options.filter(option => option.name === name);
                    }
                } else if (name) {
                    options = controls(el.ownerDocument).filter(option => option.getAttribute('name') === name && option.form === el.form);
                } else if (role === 'radio') {
                    for (let parent = el.parentElement; parent; parent = parent.parentElement) {
                        if (parent.matches('form, [role="dialog"], body') || parent.querySelector('fieldset, [role="radiogroup"]')) break;
                        const peers = controls(parent);
                        if (peers.length > 1) { options = peers; break; }
                    }
                }
                options = (options || [el]).filter(option => {
                    const target = option.closest(`[role="${role}"]`) || option.labels?.[0] || option;
                    const shown = node => {
                        const style = getComputedStyle(node);
                        const box = node.getClientRects().length > 0 || (style.display === 'contents' &&
                            [...node.querySelectorAll('*')].some(child => child.getClientRects().length > 0));
                        return box && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    return !option.disabled && target.getAttribute('aria-disabled') !== 'true' &&
                        (shown(target) || shown(option));
                });
                // A structural key isolates identically labelled Yes/No groups
                // without changing the page or relying on optional name/id attrs.
                const path = [];
                for (let node = options[0]; node?.parentElement; node = node.parentElement) {
                    path.unshift([...node.parentElement.children].indexOf(node));
                }
                return {key: path.join('/'), options};
            }""",
            role,
        )
        key_handle = handle.get_property("key")
        options_handle = handle.get_property("options")
        try:
            key = key_handle.json_value()
            radios = [prop.as_element() for prop in options_handle.get_properties().values()]
            return f"{role}:{key}", [radio for radio in radios if radio is not None]
        finally:
            key_handle.dispose()
            options_handle.dispose()
    except Exception:
        return "", []
    finally:
        if handle is not None:
            handle.dispose()


def _radio_question_options(input_elem) -> tuple[str, list]:
    return _choice_question_options(input_elem, "radio")


def _checkbox_question_options(input_elem) -> tuple[str, list]:
    return _choice_question_options(input_elem, "checkbox")


def _answer_checkbox_question(scope, checkboxes: list, label_text: str, profile: dict, answers: dict) -> bool:
    """Answer a checkbox question once, not each unnamed option independently."""
    if not checkboxes:
        return False
    candidates = [
        (_choice_option_text(checkbox, "checkbox") or checkbox.get_attribute("value") or "", str(index))
        for index, checkbox in enumerate(checkboxes)
    ]
    inferred = _infer_question_answer(label_text, profile, answers)
    if len(checkboxes) == 1:
        checkbox = checkboxes[0]
        option_norm = _normalize_lookup_text(candidates[0][0])
        if (checkbox.get_attribute("id") == "follow-company-checkbox" or
                checkbox.get_attribute("name") == "follow-company" or
                any(option_norm.startswith(token + " ") for token in _FOLLOW_COMPANY_LABEL_TOKENS)):
            return False
        desired = _normalize_lookup_text(str(inferred or ""))
        if desired in _NO_OPTION_LABELS:
            return _click_choice_input(scope, checkbox, "checkbox", checked=False)
        if _choice_is_checked(checkbox):
            return True
        if desired not in _YES_OPTION_LABELS and not _field_requires_answer(checkbox, label_text):
            return False
        selected = "0"
    else:
        current = next((str(index) for index, checkbox in enumerate(checkboxes) if _choice_is_checked(checkbox)), None)
        selected = _best_option_value(label_text, candidates, inferred, profile, answers, current_value=current)
        if selected is None:
            return False
        # Yes/No questions sometimes use checkboxes too. Avoid contradictory
        # selections, while preserving true multi-select answers in other groups.
        if all(_normalize_lookup_text(text) in _YES_OPTION_LABELS | _NO_OPTION_LABELS for text, _ in candidates):
            for index, checkbox in enumerate(checkboxes):
                if str(index) != selected and _choice_is_checked(checkbox):
                    if not _click_choice_input(scope, checkbox, "checkbox", checked=False):
                        return False
    success = _click_choice_input(scope, checkboxes[int(selected)], "checkbox")
    log.info("Checkbox answer %s: question=%r; option=%r", "SUCCESS" if success else "FAILED",
             label_text, candidates[int(selected)][0])
    return success


def _answer_radio_question(scope, radios: list, label_text: str, profile: dict, answers: dict) -> bool:
    """Use a configured answer, Yes/Ja, or one stable random choice."""
    candidates = [
        (_choice_option_text(radio, "radio") or radio.get_attribute("value") or "", str(index))
        for index, radio in enumerate(radios)
    ]
    if not candidates:
        return False

    if any(_normalize_lookup_text(text) == "pdf" or re.search(r"\.(?:pdf|docx?)\b", text, re.I)
           for text, _ in candidates):
        # Upload cards are document choices, not screening questions. The fresh
        # résumé upload logic owns them; never switch to an older stored PDF.
        return False

    inferred = _infer_question_answer(label_text, profile, answers)
    current = next((str(index) for index, radio in enumerate(radios) if _choice_is_checked(radio)), None)
    selected = _best_option_value(label_text, candidates, inferred, profile, answers, current_value=current)
    if selected is None:
        return False
    index = int(selected)
    success = _click_choice_input(scope, radios[index], "radio")
    log.info("Radio answer %s: question=%r; option=%r", "SUCCESS" if success else "FAILED",
             label_text, candidates[index][0])
    return success


def _field_requires_answer(input_elem, label_text: str) -> bool:
    """Return True when a field looks required or is currently failing validation."""
    label_norm = _normalize_lookup_text(label_text)
    if label_text.rstrip().endswith("*") or any(token in label_norm for token in _CONSENT_LABEL_TOKENS):
        return True

    try:
        if input_elem.get_attribute("required") is not None or input_elem.get_attribute("aria-required") == "true":
            return True
    except Exception:
        pass

    try:
        validation_text = input_elem.evaluate(
            """(el) => {
                const texts = [];
                const collect = (node) => {
                    if (node && (node.innerText || node.textContent)) {
                        texts.push(node.innerText || node.textContent);
                    }
                };

                const fieldset = el.closest('fieldset');
                const container = el.closest('fieldset, [data-testid], div');
                collect(fieldset);
                collect(container);

                for (const attr of ['aria-describedby', 'aria-errormessage']) {
                    const value = el.getAttribute(attr) || fieldset?.getAttribute(attr) || '';
                    for (const id of value.split(/\\s+/).filter(Boolean)) {
                        collect(el.ownerDocument.getElementById(id));
                    }
                }

                return texts.join('\\n').toLowerCase();
            }"""
        )
        return any(text in str(validation_text or "") for text in _REQUIRED_VALIDATION_TEXTS)
    except Exception:
        return False


def _has_required_validation(scope) -> bool:
    try:
        scope_text = _scope_text(scope).lower()
    except Exception:
        return False
    return any(text in scope_text for text in _REQUIRED_VALIDATION_TEXTS)


def _select_radio_group(scope, group_name: str, desired_value: str) -> bool:
    """Select a radio option by group name and desired value/label."""
    if not group_name:
        return False

    desired = _normalize_lookup_text(desired_value)
    radios = scope.query_selector_all(f"input[type='radio'][name='{group_name}']")

    for radio in radios:
        try:
            option_value = _normalize_lookup_text(radio.get_attribute("value") or "")
            radio_id = radio.get_attribute("id") or ""
            label_text = _normalize_lookup_text(_choice_option_text(radio, "radio"))
            if radio_id:
                label = scope.query_selector(f"label[for='{radio_id}']")
                if label and not label_text:
                    label_text = _normalize_lookup_text(label.text_content() or "")

            if desired not in {option_value, label_text} and not (
                desired in _YES_OPTION_LABELS and (option_value in _YES_OPTION_LABELS or label_text in _YES_OPTION_LABELS)
            ) and not (
                desired in _NO_OPTION_LABELS and (option_value in _NO_OPTION_LABELS or label_text in _NO_OPTION_LABELS)
            ):
                continue

            return _click_choice_input(scope, radio, "radio")
        except Exception:
            continue

    return False


def _radio_group_has_checked_option(scope, group_name: str) -> bool:
    """Return True when LinkedIn already selected an option in a radio group."""
    if not group_name:
        return False

    try:
        radios = scope.query_selector_all(f"input[type='radio'][name='{group_name}']")
    except Exception:
        return False

    for radio in radios:
        try:
            aria_checked = (radio.evaluate(
                """el => {
                    const control = el.closest('[role="radio"]');
                    return control ? (control.getAttribute('aria-checked') || '') : '';
                }"""
            ) or "").strip().lower()
            if aria_checked == "true":
                return True
            if aria_checked == "false":
                continue
        except Exception:
            pass

        try:
            if radio.is_checked():
                return True
        except Exception:
            try:
                if radio.get_attribute("checked") is not None:
                    return True
            except Exception:
                continue
    return False


def _select_best_radio_option(scope, group_name: str, label_text: str, inferred_value: str | None, profile: dict, answers: dict) -> bool:
    if not group_name:
        return False

    radios = scope.query_selector_all(f"input[type='radio'][name='{group_name}']")
    candidates: list[tuple[str, str]] = []
    for radio in radios:
        try:
            radio_id = radio.get_attribute("id") or ""
            option_label = _choice_option_text(radio, "radio") or (radio.get_attribute("value") or "").strip()
            if radio_id:
                label = scope.query_selector(f"label[for='{radio_id}']")
                if label and not option_label:
                    option_label = (label.text_content() or "").strip() or option_label
            if option_label:
                candidates.append((option_label, option_label))
        except Exception:
            continue

    best_value = _best_option_value(label_text, candidates, inferred_value, profile, answers)
    if not best_value:
        best_value = _fallback_choice_value(candidates)
    if not best_value:
        return False
    return _select_radio_group(scope, group_name, best_value)


def _select_checkbox_option(scope, group_name: str, desired_value: str) -> bool:
    """Select a checkbox option by group name and desired label/value."""
    if not group_name:
        return False

    desired = _normalize_lookup_text(desired_value)
    checkboxes = scope.query_selector_all(f"input[type='checkbox'][name='{group_name}']")
    for checkbox in checkboxes:
        try:
            option_value = _normalize_lookup_text(checkbox.get_attribute("value") or "")
            checkbox_id = checkbox.get_attribute("id") or ""
            label_text = _normalize_lookup_text(_choice_option_text(checkbox, "checkbox"))
            if checkbox_id:
                label = scope.query_selector(f"label[for='{checkbox_id}']")
                if label and not label_text:
                    label_text = _normalize_lookup_text(label.text_content() or "")

            if desired not in {option_value, label_text} and not (
                desired in _YES_OPTION_LABELS and (option_value in _YES_OPTION_LABELS or label_text in _YES_OPTION_LABELS)
            ) and not (
                desired in _NO_OPTION_LABELS and (option_value in _NO_OPTION_LABELS or label_text in _NO_OPTION_LABELS)
            ):
                continue

            return _click_choice_input(scope, checkbox, "checkbox")
        except Exception:
            continue
    return False


def _select_best_checkbox_option(scope, group_name: str, label_text: str, inferred_value: str | None, profile: dict, answers: dict) -> bool:
    if not group_name:
        return False

    checkboxes = scope.query_selector_all(f"input[type='checkbox'][name='{group_name}']")
    candidates: list[tuple[str, str]] = []
    for checkbox in checkboxes:
        try:
            checkbox_id = checkbox.get_attribute("id") or ""
            option_label = _choice_option_text(checkbox, "checkbox") or (checkbox.get_attribute("value") or "").strip()
            if checkbox_id:
                label = scope.query_selector(f"label[for='{checkbox_id}']")
                if label and not option_label:
                    option_label = (label.text_content() or "").strip() or option_label
            if option_label:
                candidates.append((option_label, option_label))
        except Exception:
            continue

    best_value = _best_option_value(label_text, candidates, inferred_value, profile, answers)
    if not best_value:
        best_value = _fallback_choice_value(candidates)
    if not best_value:
        return False
    return _select_checkbox_option(scope, group_name, best_value)


def _current_form_input(scope, input_elem, position: int):
    """Refresh a field replaced by a controlled form, without locator polling."""
    if input_elem.evaluate("el => el.isConnected"):
        return input_elem
    input_id = input_elem.get_attribute("id") or ""
    if input_id:
        current = scope.query_selector(f'[id={json.dumps(input_id)}]')
        if current:
            return current
    current_inputs = scope.query_selector_all(_FORM_INPUT_SELECTOR)
    return current_inputs[position] if position < len(current_inputs) else None


def _open_history_editors(scope) -> list:
    """Find only expanded education/experience editors inside this application scope."""
    handle = scope.evaluate_handle(
        """(root, labels) => {
            const normalized = text => (text || '').normalize('NFKC').toLowerCase()
                .replace(/[_-]/g, ' ').replace(/[^\\p{L}\\p{N}\\s+]/gu, ' ').trim().replace(/\\s+/g, ' ');
            const shown = el => el.isConnected && el.getClientRects().length > 0 &&
                getComputedStyle(el).visibility !== 'hidden';
            const buttonLabels = button => [button.getAttribute('aria-label'), button.innerText,
                button.textContent, button.value].filter(Boolean).map(normalized);
            const headingSelector = 'h1, h2, h3, p, legend, [role="heading"]';
            const headings = [...root.querySelectorAll(headingSelector)].filter(el =>
                shown(el) && !el.closest('button, [role="button"]') &&
                Object.values(labels.edit).some(values => values.includes(normalized(el.textContent))));
            const editors = [];
            const seen = new Set();
            for (const heading of headings) {
                const kind = Object.keys(labels.edit).find(key =>
                    labels.edit[key].includes(normalized(heading.textContent)));
                for (let parent = heading.parentElement; parent && root.contains(parent); parent = parent.parentElement) {
                    // Never cross into a sibling entry or the whole application
                    // looking for an unrelated Delete button.
                    if (headings.filter(node => parent.contains(node)).length > 1) break;
                    const fields = [...parent.querySelectorAll(
                        'input:not([type="hidden"]):not([type="file"]):not([type="button"]):not([type="submit"]), ' +
                        'select, textarea, [role="combobox"]')].filter(shown);
                    const buttons = [...parent.querySelectorAll('button, [role="button"], input[type="button"]')]
                        .filter(shown);
                    const hasSave = buttons.some(button => buttonLabels(button).some(text =>
                        ['save', 'speichern'].includes(text)));
                    if (fields.length && hasSave) {
                        const deletes = buttons.filter(button => buttonLabels(button).some(text =>
                            labels.delete[kind].includes(text) || ['delete', 'remove', 'löschen', 'loschen'].includes(text)));
                        if (!seen.has(parent)) editors.push({kind, editor: parent,
                            button: deletes.length === 1 ? deletes[0] : null});
                        seen.add(parent);
                        break;
                    }
                    if (parent === root) break;
                }
            }
            return editors;
        }""",
        {"edit": _HISTORY_EDITOR_LABELS, "delete": _HISTORY_DELETE_LABELS},
    )
    editors = []
    try:
        for item in handle.get_properties().values():
            try:
                kind = item.get_property("kind")
                try:
                    entry_kind = kind.json_value()
                finally:
                    kind.dispose()
                editor = item.get_property("editor").as_element()
                button_handle = item.get_property("button")
                button = button_handle.as_element()
                if button is None:
                    button_handle.dispose()
                editors.append((entry_kind, editor, button))
            finally:
                item.dispose()
        return editors
    finally:
        handle.dispose()


def _history_delete_confirmation(page, application_scope, kind: str, existing_dialogs=()):
    """Confirm only an entry-deletion dialog opened by our scoped Delete click."""
    for dialog in page.query_selector_all('[role="alertdialog"], [role="dialog"], dialog[open]'):
        if not dialog.is_visible() or dialog.evaluate(
            "(el, application) => el === application || el.contains(application)", application_scope,
        ):
            continue
        if any(dialog.evaluate("(el, previous) => el === previous", previous) for previous in existing_dialogs):
            continue
        text = _normalize_lookup_text(_scope_text(dialog))
        if not any(token in text for token in (kind, "entry", "eintrag", "berufserfahrung", "ausbildung")):
            continue
        if not any(token in text for token in ("delete", "remove", "löschen", "loschen")):
            continue
        # This feature removes application entries, never LinkedIn profile data.
        if any(token in text for token in ("from your profile", "on your profile", "aus ihrem profil")):
            continue
        labels = {*_HISTORY_DELETE_LABELS[kind], "delete", "remove", "löschen", "loschen"}
        for button in dialog.query_selector_all('button, [role="button"]'):
            if not button.is_visible() or not button.is_enabled():
                continue
            button_text = _normalize_lookup_text(button.inner_text())
            aria = _normalize_lookup_text(button.get_attribute("aria-label") or "")
            if button_text in labels or aria in labels:
                return button
    return None


def _delete_open_history_entries(page, scope) -> int:
    """Remove expanded application history entries before any generic field filling."""
    if scope is page:
        # A missing Easy Apply modal is never permission to delete page/profile entries.
        return 0
    deleted = 0
    for _ in range(20):
        if not scope.evaluate("el => el.isConnected"):
            scope = _get_active_modal(page) if page is not None else None
            if scope is None:
                raise RuntimeError("Easy Apply form disappeared while deleting an open history entry")
        editors = _open_history_editors(scope)
        if not editors:
            return deleted
        if page is None:
            page = scope.owner_frame().page
        kind, _, button = editors[0]
        if button is None or not button.is_enabled():
            raise RuntimeError(f"Open {kind} entry has no unique enabled Delete control; refusing to fill it")
        before = sum(entry_kind == kind for entry_kind, _, _ in editors)
        log.info("Easy Apply history entry START: deleting open %s instead of filling it", kind)
        existing_dialogs = [dialog for dialog in page.query_selector_all(
            '[role="alertdialog"], [role="dialog"], dialog[open]',
        ) if dialog.is_visible()]
        button.click(timeout=1500)
        confirmed = False
        for _ in range(20):
            if not scope.evaluate("el => el.isConnected"):
                scope = _get_active_modal(page)
                if scope is None:
                    raise RuntimeError("Easy Apply form disappeared before entry deletion could be verified")
            remaining = _open_history_editors(scope)
            if sum(entry_kind == kind for entry_kind, _, _ in remaining) < before:
                deleted += 1
                log.info("Easy Apply history entry SUCCESS: deleted open %s", kind)
                break
            if not confirmed:
                confirmation = _history_delete_confirmation(page, scope, kind, existing_dialogs)
                if confirmation is not None:
                    confirmation.click(timeout=1500)
                    confirmed = True
                    continue
            page.wait_for_timeout(100)
        else:
            raise RuntimeError(f"Delete {kind} did not close/remove the open entry; refusing to fill it")
    raise RuntimeError("Too many open Easy Apply education/experience entries; refusing to fill them")


def _repair_required_fields(scope, profile: dict, answers: dict, *, page=None,
                            type_fallbacks: dict[str, _TypeFallbackState] | None = None) -> int:
    """Fill required fields left invalid after LinkedIn shows validation errors."""
    repaired = _delete_open_history_entries(page, scope)
    if repaired:
        active_page = page or scope.owner_frame().page
        scope = _get_active_modal(active_page) or scope
    step_signature = _form_step_signature(scope) if type_fallbacks else ""
    processed_choice_groups = set()

    try:
        inputs = scope.query_selector_all(_FORM_INPUT_SELECTOR)
    except Exception:
        return 0

    for position, input_elem in enumerate(inputs):
        try:
            input_elem = _current_form_input(scope, input_elem, position)
            if input_elem is None:
                continue
            field_type = (input_elem.get_attribute("type") or "").lower()
            field_role = (input_elem.get_attribute("role") or "").lower()
            custom_choice = field_role in {"combobox", "listbox"} or input_elem.get_attribute("aria-haspopup") == "listbox"
            if field_role in {"radio", "checkbox"}:
                field_type = field_role
            if field_type in {"file", "hidden", "button", "submit", "reset", "image"} and not custom_choice:
                continue
            label_text = _extract_field_label(scope, input_elem)
            if not _field_requires_answer(input_elem, label_text):
                continue

            if custom_choice and _is_custom_choice_control(input_elem, label_text):
                active_page = page or input_elem.owner_frame().page
                if _answer_custom_choice(active_page, scope, input_elem, label_text, profile, answers):
                    repaired += 1
                continue

            try:
                tag_name = input_elem.evaluate("el => el.tagName").lower()
            except Exception:
                tag_name = "unknown"

            if field_type in {"radio", "checkbox"}:
                choice_group_key, choices = _choice_question_options(input_elem, field_type)
                if choice_group_key in processed_choice_groups:
                    continue
                processed_choice_groups.add(choice_group_key)
                answer_choice = _answer_radio_question if field_type == "radio" else _answer_checkbox_question
                if answer_choice(scope, choices, label_text, profile, answers):
                    repaired += 1
                continue

            if tag_name == "select":
                current_text = _element_current_text(input_elem).lower()
                if current_text and _normalize_lookup_text(current_text) not in _SELECT_PLACEHOLDER_TEXTS:
                    continue
                selected_value = _choose_dropdown_value(input_elem, label_text, profile, answers)
                if selected_value and _try_fill_select(input_elem, str(selected_value), label_text):
                    repaired += 1
                continue

            current_text = _element_current_text(input_elem).strip()
            preserved = _preserved_type_fallback(scope, input_elem, label_text, step_signature, type_fallbacks or {})
            if preserved is not None:
                if current_text != preserved:
                    _fill_text_input(input_elem, preserved)
                    repaired += 1
                continue
            if current_text and current_text.lower() != "a":
                continue
            if not input_elem.is_visible() or input_elem.is_disabled():
                continue
            text_value = _text_field_answer(input_elem, label_text, profile, answers)
            _fill_text_input(input_elem, text_value)
            repaired += 1
        except Exception as exc:
            log.debug("Could not repair required field: %s", exc)
            continue

    return repaired


def _retry_required_validation(page, previous_scope_text: str, button, button_name: str, profile: dict, answers: dict,
                               *, type_fallbacks: dict[str, _TypeFallbackState] | None = None) -> bool:
    """Retry after a click leaves the Easy Apply step blocked by required validation."""
    modal = _get_active_modal(page)
    scope = modal or page
    current_scope_text = _scope_text(scope)
    if current_scope_text != previous_scope_text and not _has_required_validation(scope):
        return False
    if not _has_required_validation(scope):
        return False

    repaired = _repair_required_fields(scope, profile, answers, page=page, type_fallbacks=type_fallbacks)
    if repaired <= 0:
        return False

    log.info(
        "Step still shows required validation after %s; repaired %d field(s) and retrying",
        button_name,
        repaired,
    )
    page.wait_for_timeout(300)
    # Removing an entry can rerender the entire application, including Next/Submit.
    current_scope = _get_active_modal(page) or scope
    if button_name == "Next":
        button = _find_easy_apply_next_button(current_scope) or button
    elif button_name == "Submit":
        button = _find_easy_apply_action_button(
            current_scope, _EASY_APPLY_SUBMIT_BUTTON_SELECTORS, _EASY_APPLY_SUBMIT_BUTTON_LABELS,
        ) or button
    if not _click_action_button(button):
        return False
    page.wait_for_timeout(2000)
    return True


def _parse_year_range(option_text: str) -> tuple[float, float] | None:
    text = option_text.lower().strip()
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", text)
    if range_match:
        return float(range_match.group(1)), float(range_match.group(2))

    plus_match = re.search(r"(\d+(?:\.\d+)?)\s*\+", text)
    if plus_match:
        return float(plus_match.group(1)), float("inf")

    exact_match = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    if exact_match and "year" in text:
        value = float(exact_match.group(1))
        return value, value

    return None


def _fallback_select_value(input_elem, label_text: str, profile: dict, answers: dict) -> str | None:
    """Choose a deterministic fallback option when inference fails."""
    candidates = _collect_select_candidates(input_elem)
    inferred_value = _infer_question_answer(label_text, profile, answers)
    current_text = _normalize_lookup_text(_element_current_text(input_elem))
    current = next((value for text, value in candidates if _normalize_lookup_text(text) == current_text), None)
    return _best_option_value(label_text, candidates, inferred_value, profile, answers, current_value=current)


def _try_fill_select(input_elem, value: str, label_text: str) -> bool:
    """Fill a select quickly without Playwright's long default waits."""
    current_text = _element_current_text(input_elem).lower()
    value_lower = str(value).lower()
    label_lower = label_text.lower()

    if current_text:
        if any(x in label_lower for x in _PHONE_COUNTRY_LABEL_TOKENS) or (
            "phone" in label_lower and "country" in label_lower
        ):
            if _country_code_option_matches(str(value), current_text, ""):
                log.debug(f"Dropdown '{label_text}' already looks set to '{current_text}'")
                return True
        elif value_lower in current_text or value_lower.replace("+", "") in current_text:
            log.debug(f"Dropdown '{label_text}' already looks set to '{current_text}'")
            return True

    for candidate in [str(value), str(value).replace("+", "")]:
        try:
            input_elem.select_option(value=candidate, timeout=1000)
            return True
        except Exception:
            pass

        try:
            input_elem.select_option(label=candidate, timeout=1000)
            return True
        except Exception:
            pass

    try:
        options = input_elem.query_selector_all("option")
    except Exception:
        options = []

    normalized = [value_lower, value_lower.replace("+", "")]
    numeric_value = _safe_years_value(value)
    for opt in options:
        try:
            opt_text = (opt.text_content() or "").strip()
            opt_value = (opt.get_attribute("value") or "").strip()
            if any(x in label_lower for x in _PHONE_COUNTRY_LABEL_TOKENS) or (
                "phone" in label_lower and "country" in label_lower
            ):
                if not _country_code_option_matches(str(value), opt_text, opt_value):
                    continue
                input_elem.select_option(value=opt_value or opt_text, timeout=1000)
                return True
            if numeric_value is not None:
                year_range = _parse_year_range(opt_text) or _parse_year_range(opt_value)
                if year_range and year_range[0] <= numeric_value <= year_range[1]:
                    input_elem.select_option(value=opt_value or opt_text, timeout=1000)
                    return True
            haystack = f"{opt_text} {opt_value}".lower()
            if any(token and token in haystack for token in normalized):
                input_elem.select_option(value=opt_value or opt_text, timeout=1000)
                return True
        except Exception:
            continue

    return False


def _click_action_button(button) -> bool:
    """Click an action button with minimal scrolling/retries."""
    if not button:
        return False
    try:
        button.evaluate(
            """el => el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})"""
        )
    except Exception:
        pass

    try:
        button.click(timeout=1000, force=True)
        return True
    except Exception:
        pass

    try:
        button.evaluate("el => el.click()")
        return True
    except Exception:
        return False


def _button_label_matches(button, labels: set[str]) -> bool:
    """Return True when a button's visible or accessible label matches."""
    try:
        raw_label = button.evaluate(
            """el => [
                el.getAttribute('aria-label') || '',
                el.innerText || '',
                el.textContent || '',
                el.value || ''
            ].join(' ')"""
        )
    except Exception:
        raw_label = ""

    label = _normalize_lookup_text(raw_label)
    if not label:
        return False

    return any(candidate == label or candidate in label for candidate in labels)


def _first_enabled_button_for_selectors(scope, selectors: list[str]):
    """Find the first visible and enabled button matching any selector."""
    for selector in selectors:
        try:
            for button in scope.query_selector_all(selector):
                if button.is_visible() and button.is_enabled():
                    return button
        except Exception:
            continue
    return None


def _first_enabled_button_for_labels(scope, labels: set[str]):
    """Find an enabled button by text/aria label without relying on generated classes."""
    try:
        buttons = scope.query_selector_all("button, input[type='button'], input[type='submit']")
    except Exception:
        return None

    for button in buttons:
        try:
            if button.is_visible() and button.is_enabled() and _button_label_matches(button, labels):
                return button
        except Exception:
            continue
    return None


def _scroll_easy_apply_scope_to_end(scope) -> bool:
    """Scroll nested Easy Apply containers so footer action buttons are reachable."""
    try:
        return bool(scope.evaluate(
            """root => {
                const doc = root.ownerDocument || document;
                const candidates = [
                    root,
                    root.querySelector('[data-testid="dialog-content"]'),
                    root.querySelector('[data-sdui-screen*="EasyApply"]'),
                    root.querySelector('[data-component-type="LazyColumn"]'),
                    ...root.querySelectorAll('[data-testid="dialog-content"], [data-component-type="LazyColumn"]'),
                    ...root.querySelectorAll('div, section, form')
                ].filter(Boolean);

                let scrolled = false;
                const seen = new Set();
                for (const node of candidates) {
                    if (seen.has(node)) continue;
                    seen.add(node);
                    const style = doc.defaultView.getComputedStyle(node);
                    const canScroll = /(auto|scroll|overlay)/.test(style.overflowY)
                        || node.scrollHeight > node.clientHeight + 4;
                    if (!canScroll) continue;

                    const before = node.scrollTop;
                    node.scrollTop = node.scrollHeight;
                    if (node.scrollTop !== before) {
                        scrolled = true;
                    }
                }

                return scrolled;
            }"""
        ))
    except Exception:
        return False


def _find_easy_apply_action_button(scope, selectors: list[str], labels: set[str]):
    """Find an Easy Apply action button, scrolling long SDUI modal content if needed."""
    button = _first_enabled_button_for_selectors(scope, selectors)
    if button:
        return button

    button = _first_enabled_button_for_labels(scope, labels)
    if button:
        return button

    if _scroll_easy_apply_scope_to_end(scope):
        button = _first_enabled_button_for_selectors(scope, selectors)
        if button:
            return button
        return _first_enabled_button_for_labels(scope, labels)

    return None


def _confirm_typeahead_input(page, input_elem, value: str) -> bool:
    """Accept the first matching suggestion for LinkedIn typeahead inputs."""
    try:
        role = (input_elem.get_attribute("role") or "").lower()
        autocomplete = (input_elem.get_attribute("aria-autocomplete") or "").lower()
        if role != "combobox" and autocomplete != "list":
            return False

        input_elem.focus()
        input_elem.press("Control+A")
        input_elem.fill(str(value), timeout=1000)
        page.wait_for_timeout(2000)
        input_elem.press("ArrowDown")
        page.wait_for_timeout(2000)
        input_elem.press("Enter")
        return True
    except Exception:
        return False


def _application_submission_confirmed(page) -> bool:
    text = " ".join(page.inner_text("body").casefold().split())
    return any(phrase in text for phrase in (
        "application sent", "application submitted", "application successfully submitted",
        "application has been sent", "application has been submitted", "application was submitted",
        "thank you for applying", "thank you for your application", "thanks for applying",
        "bewerbung wurde gesendet", "bewerbung wurde eingereicht", "vielen dank für ihre bewerbung",
    ))


def _fill_and_submit_form(
    page,
    profile: dict,
    answers: dict,
    resume_path: str = "",
    photo_path: str = "",
    dry_run: bool = False,
    require_fresh_resume: bool = False,
) -> str:
    """Fill and submit the multi-step Easy Apply form.

    Returns:
        "APPLIED", "READY" (intelligent dry run), "SKIPPED", "FAILED", or "LIMIT_REACHED"
    """
    try:
        max_steps = 30
        max_repeated_step_attempts = 10
        step = 0
        repeated_step_counts: dict[str, int] = {}
        blocked_progress_signature = ""
        blocked_progress_attempts = 0
        resume_uploaded = False
        completed_uploads: set[str] = set()
        type_fallbacks: dict[str, _TypeFallbackState] = {}

        while step < max_steps:
            step += 1
            log.info(f"Form step {step}")
            page.wait_for_timeout(1000)

            modal = _wait_for_easy_apply_form_ready(page, timeout_ms=5000)
            scope = modal or page

            if modal and _is_easy_apply_limit_modal(modal):
                log.warning("LinkedIn says today's Easy Apply limit has been reached")
                return "LIMIT_REACHED"

            if not modal:
                content = page.content().lower()
                if _application_submission_confirmed(page):
                    return "APPLIED"
                if "easy apply limit" in content and "tomorrow" in content:
                    log.warning("LinkedIn says today's Easy Apply limit has been reached")
                    return "LIMIT_REACHED"
                log.warning("Easy Apply modal is no longer visible")
                return "FAILED"

            if _continue_past_safety_reminder(scope, page):
                log.info("Dismissed LinkedIn safety reminder and continued applying")
                continue

            if _delete_open_history_entries(page, scope):
                scope = _get_active_modal(page) or scope

            if _has_upload_format_error(scope):
                log.info("Upload format validation is visible; skipping this job")
                return "SKIPPED"

            if _rotate_invalid_type_fields(page, scope, type_fallbacks, profile, answers):
                scope = _get_active_modal(page) or scope

            # Find all input fields on this step
            inputs = scope.query_selector_all(_FORM_INPUT_SELECTOR)
            log.info(f"Found {len(inputs)} form fields on step {step}")
            step_signature = _form_step_signature(scope, inputs)
            if step_signature:
                repeated_step_counts[step_signature] = repeated_step_counts.get(step_signature, 0) + 1
                if repeated_step_counts[step_signature] > max_repeated_step_attempts:
                    log.warning(
                        "Easy Apply page repeated more than %d times; skipping this job",
                        max_repeated_step_attempts,
                    )
                    _log_stuck_form_questions(_collect_field_questions(scope, inputs))
                    _close_easy_apply_modal(page)
                    return "SKIPPED"

            # Log field types for debugging
            field_types = {}
            for inp in inputs:
                try:
                    tag = inp.evaluate("el => el.tagName").lower()
                    field_types[tag] = field_types.get(tag, 0) + 1
                except:
                    pass
            if field_types:
                log.debug(f"Field types: {field_types}")

            # Every document request gets this job's CV, regardless of its label
            # (résumé, transcript, cover letter, certificate, or an unseen upload).
            uploads_ok, uploaded_resume_now = _upload_application_attachments(
                page, scope, resume_path, photo_path, completed_uploads,
            )
            if not uploads_ok:
                return "FAILED"
            resume_uploaded = resume_uploaded or uploaded_resume_now
            scope = _get_active_modal(page) or scope

            if _uncheck_follow_company_checkbox(scope):
                if _is_review_submit_step(scope):
                    log.info("Unchecked follow-company checkbox on review step")
                else:
                    log.info("Unchecked follow-company checkbox")

            processed_choice_groups = set()

            # Fill fields
            for position, input_elem in enumerate(inputs):
                try:
                    if not scope.evaluate("el => el.isConnected"):
                        scope = _get_active_modal(page) or scope
                    input_elem = _current_form_input(scope, input_elem, position)
                    if input_elem is None:
                        continue
                    field_name = input_elem.get_attribute("name") or ""
                    field_type = (input_elem.get_attribute("type") or "").lower()
                    field_role = (input_elem.get_attribute("role") or "").lower()
                    custom_choice = field_role in {"combobox", "listbox"} or input_elem.get_attribute("aria-haspopup") == "listbox"
                    if field_role in {"radio", "checkbox"}:
                        field_type = field_role
                    if field_type in {"file", "hidden", "button", "submit", "reset", "image"} and not custom_choice:
                        continue
                    label_text = _extract_field_label(scope, input_elem)
                    if custom_choice and _is_custom_choice_control(input_elem, label_text):
                        _answer_custom_choice(page, scope, input_elem, label_text, profile, answers)
                        continue
                    choice_group_key = ""
                    if field_type in {"radio", "checkbox"}:
                        choice_group_key, choices = _choice_question_options(input_elem, field_type)
                        if choice_group_key in processed_choice_groups:
                            continue

                    label_lower = label_text.lower()
                    log.debug(f"Field: {label_text[:40]}, type={field_type}, name={field_name}")

                    # Fill field
                    # Get element tag name to handle select elements
                    try:
                        tag_name = input_elem.evaluate("el => el.tagName").lower()
                    except:
                        tag_name = "unknown"

                    if field_type in {"radio", "checkbox"}:
                        processed_choice_groups.add(choice_group_key)
                        answer_choice = _answer_radio_question if field_type == "radio" else _answer_checkbox_question
                        answer_choice(scope, choices, label_text, profile, answers)
                        continue
                    elif tag_name == "select":
                        current_text = _element_current_text(input_elem).lower()
                        if "email" in label_lower and current_text:
                            log.info(f"Leaving prefilled email dropdown unchanged: '{current_text}'")
                            continue

                        value = _choose_dropdown_value(input_elem, label_text, profile, answers)
                        if not value:
                            log.warning(f"Could not find selectable options for dropdown '{label_text}'")
                            continue

                        log.info(f"Attempting to fill dropdown '{label_text}' with value '{value}'")
                        success = _try_fill_select(input_elem, str(value), label_text)

                        if not success:
                            log.warning(f"Could not fill dropdown '{label_text}' with '{value}' - skipping")
                            continue
                        log.info(f"Successfully selected '{label_text}' = '{value}'")
                    else:
                        if not input_elem.is_visible() or input_elem.is_disabled():
                            continue
                        current_text = _element_current_text(input_elem)
                        if "email" in label_lower and current_text:
                            log.info(f"Leaving prefilled email field unchanged: '{current_text}'")
                            continue

                        preserved = _preserved_type_fallback(scope, input_elem, label_text, step_signature, type_fallbacks)
                        if preserved is not None and current_text.strip() == preserved:
                            continue
                        text_value = preserved if preserved is not None else _text_field_answer(input_elem, label_text, profile, answers)
                        used_typeahead = False
                        if any(x in label_lower for x in ["location", "city"]) or (
                            (input_elem.get_attribute("role") or "").lower() == "combobox"
                        ):
                            used_typeahead = _confirm_typeahead_input(page, input_elem, text_value)

                        if not used_typeahead:
                            text_value = _fill_text_input(input_elem, text_value)
                        log.debug(f"Filled: {label_text[:30]} = {text_value[:20]}")

                except Exception as e:
                    log.debug(f"Could not fill field: {e}")
                    continue

            # LinkedIn can render follow-company state on the visible SDUI wrapper,
            # while the nested input still exposes a stale checked attribute.
            _uncheck_follow_company_checkbox(scope)

            scope_text_before_action = _scope_text(scope)

            if not photo_path and _has_required_photo_upload_without_file(scope):
                log.info("Required photo upload field is still empty and no photo_path is configured; skipping this job")
                return "SKIPPED"

            if _has_upload_format_error(scope):
                log.info("Upload format validation is still visible before proceeding; skipping this job")
                return "SKIPPED"

            is_review_submit_step = _is_review_submit_step(scope)
            if is_review_submit_step:
                log.info("Easy Apply review step detected; using final submit action")

            # Look for Next/Continue/Review button to go to next step.
            next_btn = _find_easy_apply_next_button(scope)
            if next_btn:
                log.info(f"Clicking Next button on step {step}")
                if not _click_action_button(next_btn):
                    log.warning(f"Could not click Next button on step {step}")
                    return "FAILED"
                page.wait_for_timeout(2000)
                retried_invalid_type = _retry_invalid_type_validation(page, step_signature, "Next", type_fallbacks, profile, answers)
                if not retried_invalid_type and not _retry_required_validation(
                    page,
                    scope_text_before_action,
                    next_btn,
                    "Next",
                    profile,
                    answers,
                    type_fallbacks=type_fallbacks,
                ):
                    _retry_numeric_validation_with_one(
                        page,
                        scope_text_before_action,
                        next_btn,
                        "Next",
                    )
                current_modal = _wait_for_easy_apply_form_ready(page, timeout_ms=1500)
                current_scope = current_modal or page
                if current_modal and _is_easy_apply_limit_modal(current_modal):
                    log.warning("LinkedIn says today's Easy Apply limit has been reached")
                    return "LIMIT_REACHED"
                current_signature = _form_step_signature(current_scope)
                if current_modal and step_signature and current_signature == step_signature:
                    if blocked_progress_signature == step_signature:
                        blocked_progress_attempts += 1
                    else:
                        blocked_progress_signature = step_signature
                        blocked_progress_attempts = 1

                    if blocked_progress_attempts > max_repeated_step_attempts:
                        log.warning(
                            "Easy Apply did not advance after %d %s attempts; skipping this job",
                            blocked_progress_attempts,
                            "Next",
                        )
                        _log_stuck_form_questions(_collect_field_questions(current_scope))
                        _close_easy_apply_modal(page)
                        return "SKIPPED"
                else:
                    blocked_progress_signature = ""
                    blocked_progress_attempts = 0
                continue

            # Look for final Submit/Apply button
            submit_btn = _find_easy_apply_action_button(
                scope,
                _EASY_APPLY_SUBMIT_BUTTON_SELECTORS,
                _EASY_APPLY_SUBMIT_BUTTON_LABELS,
            )
            if submit_btn:
                if require_fresh_resume and not resume_uploaded:
                    log.error("The generated résumé was never uploaded; refusing to submit a saved résumé")
                    return "FAILED"
                if dry_run:
                    log.info("Dry run active; stopping before final submit")
                    return "READY" if require_fresh_resume else "SKIPPED"

                log.info(f"Clicking Submit button on step {step}")
                if not _click_action_button(submit_btn):
                    log.warning(f"Could not click Submit button on step {step}")
                    return "FAILED"
                page.wait_for_timeout(2000)
                retried_invalid_type = _retry_invalid_type_validation(page, step_signature, "Submit", type_fallbacks, profile, answers)
                if not retried_invalid_type and not _retry_required_validation(
                    page,
                    scope_text_before_action,
                    submit_btn,
                    "Submit",
                    profile,
                    answers,
                    type_fallbacks=type_fallbacks,
                ):
                    _retry_numeric_validation_with_one(
                        page,
                        scope_text_before_action,
                        submit_btn,
                        "Submit",
                    )

                # Check for success message
                page.wait_for_timeout(1000)
                content = page.content().lower()
                if _application_submission_confirmed(page):
                    log.info("Application submitted successfully!")
                    return "APPLIED"

                current_modal = _get_active_modal(page)
                if current_modal:
                    if _is_easy_apply_limit_modal(current_modal):
                        log.warning("LinkedIn says today's Easy Apply limit has been reached")
                        return "LIMIT_REACHED"
                    current_signature = _form_step_signature(current_modal)
                    if step_signature and current_signature == step_signature:
                        if blocked_progress_signature == step_signature:
                            blocked_progress_attempts += 1
                        else:
                            blocked_progress_signature = step_signature
                            blocked_progress_attempts = 1

                        if blocked_progress_attempts > max_repeated_step_attempts:
                            log.warning(
                                "Easy Apply did not advance after %d %s attempts; skipping this job",
                                blocked_progress_attempts,
                                "Submit",
                            )
                            _log_stuck_form_questions(_collect_field_questions(current_modal))
                            _close_easy_apply_modal(page)
                            return "SKIPPED"

                if current_modal:
                    continue
                log.warning("No application confirmation was found after Submit")
                return "FAILED"

            # If the modal is still open but we cannot advance, treat it as a failure.
            if not inputs:
                log.info("Easy Apply form has no fields or action buttons yet; waiting for LinkedIn to finish rendering")
                page.wait_for_timeout(2000)
                continue

            log.warning(f"Could not find an actionable button on step {step}")
            return "FAILED"

        _close_easy_apply_modal(page)
        return "FAILED"

    except Exception as e:
        log.error(f"Error filling form: {e}")
        import traceback
        traceback.print_exc()
        _close_easy_apply_modal(page)
        return "FAILED"
