"""Simple LinkedIn Easy Apply automation using Playwright directly."""

import logging
from pathlib import Path
from playwright.sync_api import sync_playwright

from applypilot import config

log = logging.getLogger(__name__)


def apply_to_job(job_url: str, config: dict, profile_dir: str, port: int = 9222) -> str:
    """Apply to a LinkedIn job using the persistent browser context.

    Args:
        job_url: LinkedIn job URL to apply to
        config: Config dict with profile, answers, resume_path
        profile_dir: Path to persistent browser profile directory (for search session)
        port: Chrome DevTools Protocol port (unused, for compatibility)

    Returns:
        Status string: "APPLIED", "SKIPPED", or "FAILED"
    """
    profile = config.get("profile", {})
    answers = config.get("answers", {})

    try:
        with sync_playwright() as p:
            # Use the same persistent context that search uses
            # This ensures we have the LinkedIn session
            linkedin_profile = str(config.CHROME_WORKER_DIR / "linkedin-search")

            browser = p.chromium.launch_persistent_context(
                linkedin_profile,
                headless=False,
            )

            page = browser.new_page()

            try:
                # Ensure URL is absolute
                full_url = job_url if job_url.startswith("http") else f"https://linkedin.com{job_url}"

                # Navigate to job page
                log.info(f"Navigating to {full_url}")
                page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)

                # Check if logged in by looking for profile menu or sign-in button
                is_logged_in = page.query_selector("[data-test-id='topcard-profile-image'], .profile-link, a[href*='/feed/']")
                if not is_logged_in:
                    # Check if there's a sign-in button (indicating we're logged out)
                    sign_in_btn = page.query_selector("button:has-text('Sign in'), a:has-text('Sign in')")
                    if sign_in_btn:
                        log.info("Not logged in - sign-in button found")
                        return "SKIPPED:not_logged_in"
                    log.warning("Could not determine login status")

                # Look for Easy Apply button with multiple selectors
                easy_apply_selectors = [
                    "button:has-text('Easy Apply')",
                    "button[aria-label*='Easy Apply']",
                    ".jobs-apply-button",
                    "[data-test-id*='apply']",
                ]

                easy_apply_btn = None
                for selector in easy_apply_selectors:
                    try:
                        easy_apply_btn = page.query_selector(selector)
                        if easy_apply_btn:
                            log.info(f"Found Easy Apply button with selector: {selector}")
                            break
                    except:
                        continue

                if not easy_apply_btn:
                    # Debug: log what buttons are on the page
                    buttons = page.query_selector_all("button")
                    button_texts = [btn.text_content()[:50] for btn in buttons[:10]]
                    log.info(f"No Easy Apply button found. Page buttons: {button_texts}")
                    return "SKIPPED:no_easy_apply"

                log.info("Found Easy Apply button, clicking...")
                easy_apply_btn.click()
                page.wait_for_timeout(2000)

                # Wait for modal to appear
                modal = page.query_selector("dialog, .artdeco-modal__content, [role='dialog']")
                if not modal:
                    log.warning("Could not find form modal")
                    return "FAILED:form_not_found"

                # Fill form fields
                form_filled = _fill_form(page, config, profile, answers)
                if not form_filled:
                    log.info("Could not fill form or skipped due to required fields")
                    return "SKIPPED:could_not_fill_form"

                # Submit form
                submit_btn = page.query_selector("button:has-text('Submit'), button:has-text('Continue')")
                if submit_btn:
                    log.info("Clicking submit button")
                    submit_btn.click()
                    page.wait_for_timeout(3000)

                    # Check for success message
                    success = page.query_selector("text='Application sent', text='Thank you'")
                    if success or "confirmation" in page.url.lower():
                        log.info("Application submitted successfully")
                        return "APPLIED"
                    else:
                        log.info("Form submitted but no confirmation detected")
                        return "APPLIED"
                else:
                    log.warning("Could not find submit button")
                    return "FAILED:submit_button_not_found"

            finally:
                page.close()
                browser.close()

    except Exception as e:
        log.error(f"Error applying to job: {e}")
        import traceback
        traceback.print_exc()
        return f"FAILED:{str(e)[:50]}"


def _fill_form(page, config: dict, profile: dict, answers: dict) -> bool:
    """Fill out the LinkedIn Easy Apply form.

    Returns:
        True if form was filled successfully, False otherwise
    """
    try:
        # Get all input fields
        inputs = page.query_selector_all("input, select, textarea")

        for input_elem in inputs:
            try:
                label = _get_field_label(page, input_elem)
                if not label:
                    continue

                label_lower = label.lower()
                value = None

                # Map form labels to config values
                if any(x in label_lower for x in ["first name", "given name"]):
                    value = profile.get("first_name")
                elif any(x in label_lower for x in ["last name", "family name", "surname"]):
                    value = profile.get("last_name")
                elif any(x in label_lower for x in ["email", "e-mail"]):
                    value = profile.get("email")
                elif any(x in label_lower for x in ["phone", "mobile", "contact"]):
                    value = profile.get("phone_number")
                elif any(x in label_lower for x in ["city", "location"]):
                    value = profile.get("city")
                elif "visa" in label_lower or "sponsorship" in label_lower:
                    value = answers.get("visa_sponsorship")
                elif "authorized" in label_lower or "work" in label_lower:
                    value = answers.get("authorized_to_work")
                elif "onsite" in label_lower or "remote" in label_lower:
                    value = answers.get("onsite")
                elif "english" in label_lower or "language" in label_lower:
                    value = answers.get("english_level")
                elif "experience" in label_lower or "years" in label_lower:
                    # Try to find matching experience
                    for lang, years in answers.get("years_experience", {}).items():
                        if lang.lower() in label_lower:
                            value = years
                            break

                # Fill the field if we have a value
                if value:
                    _fill_field(input_elem, value)
                    log.debug(f"Filled '{label}' with '{value}'")

            except Exception as e:
                log.debug(f"Could not fill field: {e}")
                continue

        return True

    except Exception as e:
        log.error(f"Error filling form: {e}")
        return False


def _get_field_label(page, element) -> str:
    """Extract label text for a form field."""
    try:
        # Try to find associated label
        field_id = element.get_attribute("id")
        if field_id:
            label = page.query_selector(f"label[for='{field_id}']")
            if label:
                return label.text_content() or ""

        # Try placeholder
        placeholder = element.get_attribute("placeholder")
        if placeholder:
            return placeholder

        # Try aria-label
        aria_label = element.get_attribute("aria-label")
        if aria_label:
            return aria_label

        # Try name attribute
        name = element.get_attribute("name")
        if name:
            return name

        return ""
    except:
        return ""


def _fill_field(element, value: str) -> None:
    """Fill a form field with a value."""
    try:
        tag = element.tag_name
        input_type = element.get_attribute("type") or ""

        if tag == "select":
            # Select dropdown option
            element.select_option(value)
        elif input_type == "checkbox" or input_type == "radio":
            # Check if value matches
            if str(value).lower() in ["yes", "true", "selected"]:
                element.check()
        else:
            # Text input, textarea
            element.clear()
            element.fill(str(value))
    except Exception as e:
        log.debug(f"Could not fill field: {e}")
