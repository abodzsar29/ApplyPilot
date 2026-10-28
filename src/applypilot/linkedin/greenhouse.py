"""Bounded Greenhouse prefill using config followed by user-requested defaults."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from applypilot.linkedin.jobboards import load_jobboard_filter
from applypilot.linkedin.non_easy_questions import choose_fallback_option, fallback_text
from applypilot.linkedin.unified_apply import (
    _custom_choice_options,
    _extract_field_label,
    _match_experience_years,
    _normalize_lookup_text,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfiguredAnswer:
    value: str
    source: str


def _scalar(value) -> str | None:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (str, int, float)) and str(value).strip():
        return str(value).strip()
    return None


def _yes_no(value: str | None) -> bool | None:
    normalized = _normalize_lookup_text(value)
    if normalized in {"yes", "true", "ja", "1"}:
        return True
    if normalized in {"no", "false", "nein", "nee", "0"}:
        return False
    return None


def _mapping_answer(question: str, mapping: dict, source: str, *, exact: bool = True) -> ConfiguredAnswer | None:
    normalized = _normalize_lookup_text(question)
    for key in sorted(mapping, key=len, reverse=True):
        key_normalized = _normalize_lookup_text(key)
        value = _scalar(mapping[key])
        matches = normalized == key_normalized if exact else bool(
            key_normalized and re.search(r"(?<!\w)" + re.escape(key_normalized) + r"(?!\w)", normalized)
        )
        if value is not None and matches:
            return ConfiguredAnswer(value, f"{source}.{key}")
    return None


def configured_answer(question: str, profile: dict, answers: dict) -> ConfiguredAnswer | None:
    """Resolve configured facts only; do not invent age, consent, or employment."""
    overrides = answers.get("screening_overrides") or {}
    found = _mapping_answer(question, overrides, "answers.screening_overrides", exact=False)
    if found:
        return found
    for mapping, source in ((answers, "answers"), (profile, "profile")):
        found = _mapping_answer(question, mapping, source)
        if found:
            return found

    normalized = _normalize_lookup_text(question)
    authorization = any(token in normalized for token in (
        "authorized to work", "authorised to work", "right to work", "work authorization",
        "legally permitted to work", "legally authorized", "legally authorised",
    ))
    sponsorship = "sponsor" in normalized or "visa" in normalized
    if authorization:
        authorized = _scalar(answers.get("authorized_to_work"))
        if sponsorship and any(token in normalized for token in ("without", "no sponsorship", "not require")):
            permitted = _yes_no(authorized)
            needs_visa = _yes_no(_scalar(answers.get("visa_sponsorship")))
            if permitted is False or needs_visa is True:
                return ConfiguredAnswer("No", "answers.authorized_to_work + answers.visa_sponsorship")
            if permitted is True and needs_visa is False:
                return ConfiguredAnswer("Yes", "answers.authorized_to_work + answers.visa_sponsorship")
            return None
        if authorized is not None:
            return ConfiguredAnswer(authorized, "answers.authorized_to_work")
    elif sponsorship and any(token in normalized for token in ("require", "need", "sponsor")):
        value = _scalar(answers.get("visa_sponsorship"))
        if value is not None:
            return ConfiguredAnswer(value, "answers.visa_sponsorship")

    for mapping, source in ((answers, "answers"), (profile, "profile")):
        found = _mapping_answer(question, mapping, source, exact=False)
        if found:
            return found

    aliases = (
        (("first name", "given name"), "first_name"),
        (("last name", "family name", "surname"), "last_name"),
        (("e mail", "email"), "email"),
        (("location city", "current location", "current city", "where are you based"), "city"),
        (("phone", "mobile"), "phone_number"),
        (("linkedin profile",), "linkedin_url"),
    )
    for phrases, key in aliases:
        if any(phrase in normalized for phrase in phrases):
            value = _scalar(profile.get(key))
            if value is not None:
                return ConfiguredAnswer(value, f"profile.{key}")
    if any(word in normalized for word in ("proficien", "language", "fluency", "speak", "level")):
        for key, configured in answers.items():
            language = _normalize_lookup_text(key[:-6]) if key.endswith("_level") else ""
            value = _scalar(configured)
            if value is not None and language and re.search(r"(?<!\w)" + re.escape(language) + r"(?!\w)", normalized):
                return ConfiguredAnswer(value, f"answers.{key}")
    if any(token in normalized for token in ("years", "experience", "how long")):
        value = _match_experience_years(normalized, answers)
        if value is not None:
            if normalized.startswith(("do you", "have you", "are you", "can you")):
                try:
                    years = float(value)
                except ValueError:
                    pass
                else:
                    threshold = re.search(r"(at least|more than)\s+(\d+(?:\.\d+)?)\s+years?", normalized)
                    qualified = years > 0
                    if threshold:
                        required = float(threshold[2])
                        qualified = years >= required if threshold[1] == "at least" else years > required
                    value = "Yes" if qualified else "No"
            return ConfiguredAnswer(value, "answers.years_experience")
    return None


def _matches(candidate: str, desired: str) -> bool:
    actual, target = _normalize_lookup_text(candidate), _normalize_lookup_text(desired)
    if actual == target:
        return True
    # Configured agreement can be represented by Yes/Ja on another board.
    agreement = {"i agree": True, "i consent": True, "i accept": True, "i decline": False}
    actual_bool, target_bool = agreement.get(actual, _yes_no(actual)), agreement.get(target, _yes_no(target))
    if actual_bool is not None and target_bool is not None:
        return actual_bool == target_bool
    # Location suggestions can add a region: Berlin, Berlin, Germany.
    return bool(target and set(target.split()).issubset(actual.split()))


def _selection_state(control) -> dict:
    return control.evaluate("""el => {
        const wrapper = el.closest('.select') || el.parentElement;
        const selected = [...wrapper.querySelectorAll('.select__single-value, .select__multi-value__label')]
            .map(node => node.textContent.trim()).filter(Boolean);
        const emptyMirror = [...wrapper.querySelectorAll('input[aria-hidden="true"][required]')]
            .some(node => !node.value);
        return {selected, invalid: el.getAttribute('aria-invalid') === 'true', emptyMirror};
    }""")


def _committed(control, expected: str) -> bool:
    state = _selection_state(control)
    return not state["invalid"] and not state["emptyMirror"] and any(
        _matches(text, expected) for text in state["selected"]
    )


def _select_configured_option(page, control, answer: ConfiguredAnswer, *, verify_as: str | None = None) -> bool:
    """Click an actual option; a search input's text is not a selected value."""
    expected = verify_as or answer.value
    if _committed(control, expected):
        return True
    control.click(timeout=800)
    for _ in range(20):
        options = _custom_choice_options(control)
        texts = [(option, (option.text_content() or "").strip()) for option in options]
        exact = [(option, text) for option, text in texts
                 if _normalize_lookup_text(text) == _normalize_lookup_text(answer.value)]
        matches = exact or [(option, text) for option, text in texts if _matches(text, answer.value)]
        if len(matches) == 1:
            option, selected_text = matches[0]
            option.click(timeout=800)
            for _ in range(8):
                if _committed(control, verify_as or selected_text):
                    return True
                page.wait_for_timeout(50)
            break
        if _ == 0:
            # Inspect unfiltered options first: configured 'I agree' may be rendered as Yes/Ja.
            control.fill(answer.value, timeout=800)
        page.wait_for_timeout(50)
    # Close this menu, not the form. Never press Enter on a closed dropdown.
    control.press("Escape", timeout=500)
    return False


def _select_fallback_option(page, control, question: str) -> tuple[bool, str]:
    """Choose among this widget's real options, not fabricated search values."""
    state = _selection_state(control)
    current = next(iter(state["selected"]), "") if not state["invalid"] and not state["emptyMirror"] else ""
    control.click(timeout=800)
    control.fill("", timeout=800)
    for attempt in range(30):
        options = _custom_choice_options(control)
        texts = [(option.text_content() or "").strip() for option in options]
        selected = choose_fallback_option(question, texts, current=current)
        if selected:
            text, source = selected
            if text == current and _committed(control, text):
                control.press("Escape", timeout=500)
                return True, source
            options[texts.index(text)].click(timeout=800)
            for _ in range(8):
                if _committed(control, text):
                    return True, source
                page.wait_for_timeout(50)
            control.press("Escape", timeout=500)
            return False, source
        if attempt == 15:
            # Async lookup widgets can require a nonempty search before any options exist.
            control.fill("a", timeout=800)
        page.wait_for_timeout(50)
    control.press("Escape", timeout=500)
    return False, "fallback.no_usable_options"


def _structured_answer_present(question: str, profile: dict, answers: dict) -> bool:
    normalized = _normalize_lookup_text(question)
    return any(
        isinstance(value, (list, dict)) and bool(value) and _normalize_lookup_text(key) == normalized
        for mapping in (profile, answers, answers.get("screening_overrides") or {})
        for key, value in mapping.items()
    )


def prefill_greenhouse_application(page, settings: dict) -> list[dict]:
    """Fill config first, then favorable/negative/random defaults; never submit."""
    url = page.url
    if not isinstance(url, str):
        return []
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (host == "greenhouse.io" or host.endswith(".greenhouse.io")):
        return []
    form = page.locator("form#application-form")
    if form.count() != 1:
        return []
    jobboard_filter = load_jobboard_filter(settings)
    profile, answers = settings.get("profile") or {}, settings.get("answers") or {}
    fields = form.locator("input[id], textarea[id], select[id]").evaluate_all("els => els.map(el => el.id)")
    report = []
    for field_id in dict.fromkeys(fields):
        current = urlsplit(page.url)
        if ((current.scheme, current.hostname, current.path) != (parsed.scheme, parsed.hostname, parsed.path)
                or (jobboard_filter and not jobboard_filter.matches(page.url))):
            log.warning("Stopped configured Greenhouse prefill: application destination changed")
            break
        control = form.locator(f"[id={json.dumps(field_id)}]")
        if not control.count() or not control.is_visible() or not control.is_enabled():
            continue
        if control.get_attribute("aria-hidden") == "true" or control.get_attribute("readonly") is not None:
            continue
        input_type = (control.get_attribute("type") or "text").lower()
        if input_type not in {"text", "search", "tel", "email", "url", "number", "date"}:
            continue
        question = _extract_field_label(form, control)
        if field_id == "country" and control.evaluate("el => !!el.closest('.phone-input')"):
            value = _scalar(profile.get("phone_country"))
            answer = ConfiguredAnswer(value, "profile.phone_country") if value else None
        else:
            answer = configured_answer(question, profile, answers)
        if not answer and _structured_answer_present(question, profile, answers):
            report.append({"field_id": field_id, "question": question, "source": "configured.structured",
                           "status": "needs_agent"})
            continue
        status = "retry_with_agent"
        source = answer.source if answer else "fallback.text"
        try:
            if control.evaluate("el => el.tagName === 'SELECT'"):
                options = control.locator("option").evaluate_all(
                    "els => els.filter(el => !el.disabled).map(el => ({text: el.textContent, value: el.value}))"
                )
                options = [opt for opt in options if opt["value"]]
                if answer:
                    matches = [opt for opt in options if _matches(opt["text"], answer.value)]
                    selected = matches[0] if len(matches) == 1 else None
                else:
                    current = next((opt["text"] for opt in options if opt["value"] == control.input_value()), "")
                    decision = choose_fallback_option(question, [opt["text"] for opt in options], current=current)
                    selected = next((opt for opt in options if decision and opt["text"] == decision[0]), None)
                    source = decision[1] if decision else "fallback.no_usable_options"
                if selected:
                    control.select_option(value=selected["value"], timeout=800)
                    status = "filled" if control.input_value() == selected["value"] else status
            elif control.get_attribute("role") == "combobox":
                if answer:
                    code = _scalar(profile.get("phone_country_code")) if field_id == "country" else None
                    success = _select_configured_option(page, control, answer, verify_as=code)
                else:
                    success, source = _select_fallback_option(page, control, question)
                if success:
                    status = "filled"
            else:
                numeric = input_type == "number" or control.get_attribute("inputmode") in {"numeric", "decimal"}
                value = answer.value if answer else fallback_text(question, "number" if numeric else input_type)
                control.fill(value, timeout=800)
                control.blur(timeout=800)
                if control.input_value() == value and control.get_attribute("aria-invalid") != "true":
                    status = "filled"
        except PlaywrightError:
            # The agent gets the unresolved field report and can use a fresh snapshot.
            pass
        report.append({"field_id": field_id, "question": question, "source": source, "status": status})
        log.info("Greenhouse answer %s: question=%r; source=%s", status, question, source)
    return report
