"""Unified LinkedIn search and apply in a single browser session."""

import logging
import random
import re
import time
import unicodedata
from playwright.sync_api import sync_playwright
from applypilot import config

log = logging.getLogger(__name__)


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


def _normalize_location_text(value: str) -> str:
    """Normalize location labels for resilient text matching."""
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


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
        Dict with applied, skipped, failed counts
    """
    job_title = config_dict.get("job_title", "")
    location = config_dict.get("location", "")
    profile = config_dict.get("profile", {})
    answers = config_dict.get("answers", {})
    resume_path = config_dict.get("resume_path", "")

    if not job_title or not location:
        log.error("job_title and location are required")
        return {"applied": 0, "skipped": 0, "failed": 0}

    applied = 0
    skipped = 0
    failed = 0

    try:
        with sync_playwright() as p:
            # Launch persistent context with saved LinkedIn session
            profile_dir = config.CHROME_WORKER_DIR / "linkedin-search"
            browser = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=headless,
            )
            page = browser.new_page()

            # Navigate to LinkedIn jobs search
            search_url = f"https://www.linkedin.com/jobs/search/?keywords={job_title}&location={location}&f_AL=true"
            if setup:
                _hold_setup_session_open(page, search_url)
                return {"applied": 0, "skipped": 0, "failed": 0}

            log.info(f"Navigating to {search_url}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            if exact_location:
                _apply_exact_location_filter(page, location)

            # Process jobs on current page
            while applied < max_applications:
                log.info(f"Looking for jobs (applied: {applied}/{max_applications})")

                # Find job cards
                job_cards = page.query_selector_all("div.job-card-container")
                log.info(f"Found {len(job_cards)} job cards on page")

                if not job_cards:
                    log.info("No more job cards found")
                    break

                for idx, card in enumerate(job_cards):
                    if applied >= max_applications:
                        break

                    try:
                        # Get job URL
                        job_link = card.query_selector("a[href*='/jobs/view/']")
                        if not job_link:
                            log.debug(f"Card {idx}: No job link found")
                            continue

                        job_url = job_link.get_attribute("href") or ""
                        if not job_url:
                            continue

                        # Check title if keyword filter is active
                        if title_keyword:
                            title_elem = card.query_selector("h3, .job-title, span")
                            if not title_elem:
                                log.debug(f"Card {idx}: No title element")
                                skipped += 1
                                continue

                            title_text = (title_elem.text_content() or "").lower()
                            if title_keyword.lower() not in title_text:
                                log.debug(f"Card {idx}: Skipping - keyword '{title_keyword}' not in '{title_text}'")
                                skipped += 1
                                continue

                        log.info(f"Card {idx}: Found job {job_url}")

                        # Click on job to open it
                        job_link.click()
                        page.wait_for_timeout(3000)

                        # Look for Easy Apply button in the job detail panel (not the filter)
                        # Try multiple selectors to find the button on the job detail, not in the sidebar
                        easy_apply_btn = None
                        selectors = [
                            ".job-details-top-card button:has-text('Easy Apply')",
                            "[data-test-id='jobs-details'] button:has-text('Easy Apply')",
                            ".jobs-detail__main-content button:has-text('Easy Apply')",
                            ".jobs-apply-button",
                            "button[aria-label='Easy Apply job']",
                            # Last resort: any Easy Apply button that's visible and enabled
                            "button:has-text('Easy Apply')",
                        ]

                        for selector in selectors:
                            try:
                                btns = page.query_selector_all(selector)
                                # Filter to buttons that are actually visible (not in sidebar)
                                for btn in btns:
                                    # Check if button is in the main content area, not sidebar
                                    bounding_box = btn.bounding_box()
                                    if bounding_box and bounding_box['x'] > 300:  # Job detail is typically to the right
                                        easy_apply_btn = btn
                                        log.info(f"Card {idx}: Found Easy Apply button with selector: {selector}")
                                        break
                                if easy_apply_btn:
                                    break
                            except:
                                continue

                        if not easy_apply_btn:
                            log.info(f"Card {idx}: No Easy Apply button found in job detail")
                            skipped += 1
                            continue

                        log.info(f"Card {idx}: Clicking Easy Apply button")
                        easy_apply_btn.click()
                        page.wait_for_timeout(2000)

                        # Try to fill form (simplified version)
                        result = _fill_and_submit_form(
                            page,
                            profile,
                            answers,
                            resume_path,
                            dry_run=dry_run,
                        )

                        if result == "APPLIED":
                            applied += 1
                            log.info(f"Successfully applied to job {idx}")
                        elif result == "SKIPPED":
                            skipped += 1
                            log.info(f"Skipped job {idx}")
                        else:
                            failed += 1
                            log.info(f"Failed to apply to job {idx}")

                        _close_easy_apply_modal(page)
                        page.wait_for_timeout(1000)

                    except Exception as e:
                        log.error(f"Error processing card {idx}: {e}")
                        failed += 1
                        continue

                # Try to go to next page if we haven't reached max
                if applied < max_applications:
                    next_btn = page.query_selector("button[aria-label*='View next page'], button:has-text('Next')")
                    if next_btn and next_btn.is_enabled():
                        log.info("Going to next page")
                        next_btn.click()
                        page.wait_for_timeout(2000)
                    else:
                        log.info("No next page button found")
                        break

            browser.close()

    except Exception as e:
        log.error(f"Error in search_and_apply: {e}")
        import traceback
        traceback.print_exc()

    log.info(f"Final results: applied={applied}, skipped={skipped}, failed={failed}")
    return {"applied": applied, "skipped": skipped, "failed": failed}


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
        if "country" in label.lower() or "dial" in label.lower():
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
            if "country" in label.lower() or "dial" in label.lower():
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


def _get_active_modal(page):
    """Return the visible Easy Apply modal/dialog if one is open."""
    selectors = [
        "div.jobs-easy-apply-modal",
        ".artdeco-modal[role='dialog']",
        "[role='dialog']",
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


def _close_easy_apply_modal(page) -> None:
    """Dismiss the Easy Apply modal if it is still open."""
    modal = _get_active_modal(page)
    if not modal:
        return

    selectors = [
        "button[aria-label*='Dismiss']",
        "button[aria-label*='Close']",
        "button:has-text('Done')",
        "button:has-text('Dismiss')",
        "button:has-text('Close')",
    ]
    for selector in selectors:
        try:
            btn = modal.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(500)
                return
        except Exception:
            continue

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass


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


def _is_review_submit_step(scope) -> bool:
    """Return True when the Easy Apply modal is on the final review step."""
    scope_text = _scope_text(scope)
    if "review your application" not in scope_text:
        return False

    selectors = [
        "button[aria-label='Submit application']",
        "[data-live-test-easy-apply-submit-button]",
        "button:has-text('Submit application')",
    ]
    for selector in selectors:
        try:
            btn = scope.query_selector(selector)
            if btn and btn.is_visible() and btn.is_enabled():
                return True
        except Exception:
            continue
    return False


def _uncheck_follow_company_checkbox(scope) -> bool:
    """Uncheck the follow-company checkbox on the review step."""
    checkboxes = scope.query_selector_all("input[type='checkbox']")
    for checkbox in checkboxes:
        try:
            checkbox_id = checkbox.get_attribute("id") or ""
            label_text = ""
            label = None
            if checkbox_id:
                label = scope.query_selector(f"label[for='{checkbox_id}']")
                if label:
                    label_text = (label.text_content() or "").strip()

            label_lower = " ".join(label_text.lower().split())
            if "follow" not in label_lower:
                continue
            if "stay up to date" not in label_lower and "page" not in label_lower:
                continue

            if checkbox.is_checked():
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
        if tech.lower() in label_lower:
            return str(years)
    if "experience" in label_lower or "years" in label_lower:
        return "1"
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
        elif key_norm in label_norm:
            score = 200 + len(key_norm)
        elif label_norm in key_norm:
            score = 100 + len(label_norm)

        if score > best_score:
            best_score = score
            best_value = str(value)

    return best_value if best_score >= 0 else None


def _infer_question_answer(label_text: str, profile: dict, answers: dict) -> str | None:
    """Infer an answer from config for common LinkedIn screening questions."""
    label_lower = " ".join(label_text.lower().split())

    direct_profile_value = _lookup_direct_mapping(label_text, profile)
    if direct_profile_value is not None:
        return direct_profile_value

    direct_answer_value = _lookup_direct_mapping(label_text, answers)
    if direct_answer_value is not None:
        return direct_answer_value

    if any(x in label_lower for x in ["country code", "dial code", "landesvorwahl"]) or (
        any(x in label_lower for x in ["phone", "telefon", "mobile", "handy"])
        and any(x in label_lower for x in ["country", "code", "dial", "vorwahl"])
    ):
        return str(profile.get("phone_country_code", "+44"))
    if any(x in label_lower for x in ["phone", "mobile", "contact number", "telefonnummer", "handynummer", "mobilnummer"]) and "city" not in label_lower:
        return profile.get("phone_number")
    if any(x in label_lower for x in ["first name", "given name", "vorname"]):
        return profile.get("first_name")
    if any(x in label_lower for x in ["last name", "family name", "surname", "nachname"]):
        return profile.get("last_name")
    if any(x in label_lower for x in ["email", "e-mail"]):
        return profile.get("email")
    if any(x in label_lower for x in ["city", "current location", "current city", "where are you located", "stadt", "wohnort"]) and "phone" not in label_lower:
        return profile.get("city")
    if "visa" in label_lower or "sponsorship" in label_lower:
        return answers.get("visa_sponsorship", "No")
    if any(x in label_lower for x in ["authorized to work", "work authorization", "legally authorized"]):
        return answers.get("authorized_to_work", "Yes")
    if "onsite" in label_lower or "remote" in label_lower:
        return answers.get("onsite")
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
        "are you willing", "can you start", "are you available"
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


def _collect_select_candidates(input_elem) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    try:
        options = input_elem.query_selector_all("option")
    except Exception:
        return candidates

    for opt in options:
        text = (opt.text_content() or "").strip()
        value = (opt.get_attribute("value") or "").strip()
        normalized = text.lower()
        if not text:
            continue
        if "select an option" in normalized or normalized in {"select", "choose", "choose one"}:
            continue
        candidates.append((text, value or text))
    return candidates


def _select_random_dropdown_value(input_elem, label_text: str) -> str | None:
    """Randomly choose a non-placeholder option from a dropdown."""
    candidates = _collect_select_candidates(input_elem)
    if not candidates:
        return None
    chosen_text, chosen_value = random.choice(candidates)
    log.info(f"Randomly selected dropdown '{label_text}' = '{chosen_text}'")
    return chosen_value


def _fill_text_input(input_elem, text_value: str) -> str:
    """Fill a text input, retrying with '1' when the default 'a' is rejected."""
    try:
        input_elem.fill(text_value, timeout=1000)
        return text_value
    except Exception:
        if text_value != "a":
            raise

    input_elem.fill("1", timeout=1000)
    return "1"


def _rewrite_default_a_fields_to_four(scope) -> int:
    """Replace fallback 'a' values with '4' for visible text-like inputs."""
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
            input_elem.fill("4", timeout=1000)
            rewritten += 1
        except Exception:
            continue
    return rewritten


def _retry_numeric_validation_with_four(page, previous_scope_text: str, button, button_name: str) -> bool:
    """Retry the current step when it appears blocked by a 0..99 numeric validation."""
    modal = _get_active_modal(page)
    scope = modal or page
    current_scope_text = _scope_text(scope)
    if current_scope_text != previous_scope_text:
        return False
    if "0" not in current_scope_text or "99" not in current_scope_text:
        return False

    rewritten = _rewrite_default_a_fields_to_four(scope)
    if rewritten <= 0:
        return False

    log.info(
        "Step unchanged after %s and 0/99 validation detected; rewrote %d field(s) from 'a' to '4'",
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
    option_lower = " ".join(option_text.lower().split())
    score = 0

    if inferred_value:
        inferred_lower = str(inferred_value).lower()
        if option_lower == inferred_lower:
            score += 120
        elif inferred_lower in option_lower:
            score += 90

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

    if any(x in label_lower for x in ["country code", "dial code"]) or ("phone" in label_lower and "country" in label_lower):
        if _country_code_option_matches(phone_country, option_text, option_text):
            score += 120

    if any(x in label_lower for x in ["visa", "sponsorship"]):
        if sponsorship == "no":
            if option_lower in {"no", "false"} or "do not require" in option_lower or "no sponsorship" in option_lower:
                score += 120
            if "sponsorship" in option_lower and any(x in option_lower for x in ["yes", "require", "need"]):
                score -= 120
        elif sponsorship == "yes":
            if option_lower in {"yes", "true"} or "require" in option_lower or "need sponsorship" in option_lower:
                score += 120

    if any(x in label_lower for x in ["authorized to work", "work authorization", "legally authorized", "right to work", "legally work"]):
        if authorized == "yes":
            if option_lower in {"yes", "true"}:
                score += 110
            if any(x in option_lower for x in ["eu", "germany", "german", "citizen", "citizenship", "permanent", "authorized", "work permit"]):
                score += 95
            if any(x in option_lower for x in ["no", "false", "need sponsorship", "require sponsorship", "none"]):
                score -= 120
        elif authorized == "no":
            if option_lower in {"no", "false"}:
                score += 110

    if any(x in label_lower for x in ["open to relocating", "based in berlin", "willing"]):
        if any(x in option_lower for x in ["yes", "open to relocating", "berlin", "germany"]):
            score += 80
        if "outside" in option_lower:
            score -= 30

    if "email" in label_lower and profile.get("email") and str(profile.get("email")).lower() in option_lower:
        score += 120

    if "no experience" in option_lower or option_lower == "none":
        score -= 10

    return score


def _best_option_value(label_text: str, candidates: list[tuple[str, str]], inferred_value: str | None, profile: dict, answers: dict) -> str | None:
    if not candidates:
        return None

    best_text, best_value = candidates[0]
    best_score = _score_option(label_text, best_text, inferred_value, profile, answers)
    for text, value in candidates[1:]:
        score = _score_option(label_text, text, inferred_value, profile, answers)
        if score > best_score:
            best_text, best_value, best_score = text, value, score
    return best_value


def _select_radio_group(scope, group_name: str, desired_value: str) -> bool:
    """Select a radio option by group name and desired value/label."""
    if not group_name:
        return False

    desired = desired_value.strip().lower()
    radios = scope.query_selector_all(f"input[type='radio'][name='{group_name}']")

    for radio in radios:
        try:
            option_value = (radio.get_attribute("value") or "").strip().lower()
            radio_id = radio.get_attribute("id") or ""
            label_text = ""
            if radio_id:
                label = scope.query_selector(f"label[for='{radio_id}']")
                if label:
                    label_text = (label.text_content() or "").strip().lower()

            if desired not in {option_value, label_text}:
                continue

            if radio_id:
                label = scope.query_selector(f"label[for='{radio_id}']")
                if label and label.is_visible():
                    label.click(timeout=1000)
                    return True

            radio.check(timeout=1000, force=True)
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
            option_label = (radio.get_attribute("value") or "").strip()
            if radio_id:
                label = scope.query_selector(f"label[for='{radio_id}']")
                if label:
                    option_label = (label.text_content() or "").strip() or option_label
            if option_label:
                candidates.append((option_label, option_label))
        except Exception:
            continue

    best_value = _best_option_value(label_text, candidates, inferred_value, profile, answers)
    if not best_value:
        return False
    return _select_radio_group(scope, group_name, best_value)


def _select_checkbox_option(scope, group_name: str, desired_value: str) -> bool:
    """Select a checkbox option by group name and desired label/value."""
    if not group_name:
        return False

    desired = desired_value.strip().lower()
    checkboxes = scope.query_selector_all(f"input[type='checkbox'][name='{group_name}']")
    for checkbox in checkboxes:
        try:
            option_value = (checkbox.get_attribute("value") or "").strip().lower()
            checkbox_id = checkbox.get_attribute("id") or ""
            label_text = ""
            if checkbox_id:
                label = scope.query_selector(f"label[for='{checkbox_id}']")
                if label:
                    label_text = (label.text_content() or "").strip().lower()

            if desired not in {option_value, label_text}:
                continue

            if checkbox_id:
                label = scope.query_selector(f"label[for='{checkbox_id}']")
                if label and label.is_visible():
                    label.click(timeout=1000)
                    return True

            checkbox.check(timeout=1000, force=True)
            return True
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
            option_label = (checkbox.get_attribute("value") or "").strip()
            if checkbox_id:
                label = scope.query_selector(f"label[for='{checkbox_id}']")
                if label:
                    option_label = (label.text_content() or "").strip() or option_label
            if option_label:
                candidates.append((option_label, option_label))
        except Exception:
            continue

    best_value = _best_option_value(label_text, candidates, inferred_value, profile, answers)
    if not best_value:
        return False
    return _select_checkbox_option(scope, group_name, best_value)


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
    return _best_option_value(label_text, candidates, inferred_value, profile, answers)


def _try_fill_select(input_elem, value: str, label_text: str) -> bool:
    """Fill a select quickly without Playwright's long default waits."""
    current_text = _element_current_text(input_elem).lower()
    value_lower = str(value).lower()
    label_lower = label_text.lower()

    if current_text:
        if any(x in label_lower for x in ["country code", "dial code"]) or (
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
            if any(x in label_lower for x in ["country code", "dial code"]) or (
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


def _fill_and_submit_form(
    page,
    profile: dict,
    answers: dict,
    resume_path: str = "",
    dry_run: bool = False,
) -> str:
    """Fill and submit the multi-step Easy Apply form.

    Returns:
        "APPLIED", "SKIPPED", or "FAILED"
    """
    try:
        max_steps = 10
        step = 0

        while step < max_steps:
            step += 1
            log.info(f"Form step {step}")
            page.wait_for_timeout(1000)

            modal = _get_active_modal(page)
            scope = modal or page

            if not modal:
                content = page.content().lower()
                if any(x in content for x in ["application sent", "thank you", "successfully"]):
                    return "APPLIED"
                log.warning("Easy Apply modal is no longer visible")
                return "FAILED"

            # Find all input fields on this step
            inputs = scope.query_selector_all("input, select, textarea")
            log.info(f"Found {len(inputs)} form fields on step {step}")

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

            # Resume upload can be the only required action on a step.
            resume_input = scope.query_selector("input[type='file']")
            if resume_input and resume_path:
                log.info(f"Uploading resume: {resume_path}")
                try:
                    resume_input.set_input_files(resume_path)
                    page.wait_for_timeout(2000)
                except Exception as e:
                    log.warning(f"Could not upload resume: {e}")

            if _is_review_submit_step(scope):
                if _uncheck_follow_company_checkbox(scope):
                    log.info("Unchecked follow-company checkbox on review step")

            processed_choice_groups = set()

            # Fill fields
            for input_elem in inputs:
                try:
                    field_name = input_elem.get_attribute("name") or ""
                    field_type = input_elem.get_attribute("type") or ""
                    label_text = _extract_field_label(scope, input_elem)

                    label_lower = label_text.lower()
                    log.debug(f"Field: {label_text[:40]}, type={field_type}, name={field_name}")

                    value = _infer_question_answer(label_text, profile, answers)

                    # Fill field
                    # Get element tag name to handle select elements
                    try:
                        tag_name = input_elem.evaluate("el => el.tagName").lower()
                    except:
                        tag_name = "unknown"

                    if field_type == "radio":
                        group_name = field_name or input_elem.get_attribute("name") or input_elem.get_attribute("id") or ""
                        if group_name in processed_choice_groups:
                            continue
                        selected = False
                        if value:
                            selected = _select_radio_group(scope, group_name, str(value))
                        if not selected:
                            selected = _select_best_radio_option(scope, group_name, label_text, value, profile, answers)
                        if selected:
                            processed_choice_groups.add(group_name)
                            log.info(f"Selected radio answer for '{label_text}'")
                        continue
                    if field_type == "checkbox":
                        group_name = field_name or input_elem.get_attribute("name") or input_elem.get_attribute("id") or ""
                        if group_name in processed_choice_groups:
                            continue
                        selected = False
                        if value and str(value).lower() in ["yes", "true", "selected"]:
                            try:
                                input_elem.check(timeout=1000, force=True)
                                selected = True
                            except Exception:
                                selected = False
                        if not selected:
                            selected = _select_best_checkbox_option(scope, group_name, label_text, value, profile, answers)
                        if selected:
                            processed_choice_groups.add(group_name)
                            log.info(f"Selected checkbox answer for '{label_text}'")
                    elif tag_name == "select":
                        current_text = _element_current_text(input_elem).lower()
                        if "email" in label_lower and current_text:
                            log.info(f"Leaving prefilled email dropdown unchanged: '{current_text}'")
                            continue

                        value = _select_random_dropdown_value(input_elem, label_text)
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
                        current_text = _element_current_text(input_elem)
                        if "email" in label_lower and current_text:
                            log.info(f"Leaving prefilled email field unchanged: '{current_text}'")
                            continue

                        text_value = str(value) if value else "a"
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

            # LinkedIn often leaves the follow-company box checked by default.
            try:
                follow_checkbox = scope.query_selector("input[type='checkbox']")
                if follow_checkbox and follow_checkbox.is_checked():
                    label_text = ""
                    try:
                        checkbox_id = follow_checkbox.get_attribute("id") or ""
                        if checkbox_id:
                            label = scope.query_selector(f"label[for='{checkbox_id}']")
                            if label:
                                label_text = (label.text_content() or "").lower()
                    except Exception:
                        pass
                    if "follow" in label_text:
                        follow_checkbox.uncheck()
            except Exception:
                pass

            scope_text_before_action = _scope_text(scope)

            # Look for Next/Continue/Review button to go to next step.
            next_btn = scope.query_selector(
                "button:has-text('Next'), button:has-text('Continue'), "
                "button:has-text('Review'), button[aria-label*='Next']"
            )
            if next_btn and next_btn.is_enabled():
                log.info(f"Clicking Next button on step {step}")
                if not _click_action_button(next_btn):
                    log.warning(f"Could not click Next button on step {step}")
                    return "FAILED"
                page.wait_for_timeout(2000)
                _retry_numeric_validation_with_four(
                    page,
                    scope_text_before_action,
                    next_btn,
                    "Next",
                )
                continue

            # Look for final Submit/Apply button
            submit_btn = (
                scope.query_selector("button[aria-label='Submit application']")
                or scope.query_selector("[data-live-test-easy-apply-submit-button]")
                or scope.query_selector("button:has-text('Submit application')")
                or scope.query_selector("button:has-text('Submit')")
                or scope.query_selector("button:has-text('Apply')")
                or scope.query_selector("button:has-text('Send')")
                or scope.query_selector("button[type='submit']")
                or scope.query_selector("button:has-text('Finish')")
            )
            if submit_btn and submit_btn.is_enabled():
                if dry_run:
                    log.info("Dry run active; stopping before final submit")
                    return "SKIPPED"

                log.info(f"Clicking Submit button on step {step}")
                if not _click_action_button(submit_btn):
                    log.warning(f"Could not click Submit button on step {step}")
                    return "FAILED"
                page.wait_for_timeout(2000)
                _retry_numeric_validation_with_four(
                    page,
                    scope_text_before_action,
                    submit_btn,
                    "Submit",
                )

                # Check for success message
                page.wait_for_timeout(1000)
                content = page.content().lower()
                if any(x in content for x in ["application sent", "thank you", "confirmed", "successfully"]):
                    log.info("Application submitted successfully!")
                    return "APPLIED"

                return "APPLIED"  # Assume success

            # If the modal is still open but we cannot advance, treat it as a failure.
            log.warning(f"Could not find an actionable button on step {step}")
            return "FAILED"

        return "FAILED"

    except Exception as e:
        log.error(f"Error filling form: {e}")
        import traceback
        traceback.print_exc()
        return "FAILED"
