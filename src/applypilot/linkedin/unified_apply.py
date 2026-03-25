"""Unified LinkedIn search and apply in a single browser session."""

import logging
from pathlib import Path
from playwright.sync_api import sync_playwright
from applypilot import config

log = logging.getLogger(__name__)


def search_and_apply(config_dict: dict, max_applications: int = 1, title_keyword: str = "") -> dict:
    """Search LinkedIn jobs and apply to them in a single browser session.

    Args:
        config_dict: Config with job_title, location, profile, answers, resume_path
        max_applications: Max number of jobs to apply to
        title_keyword: Filter jobs by this keyword (empty = apply to all)

    Returns:
        Dict with applied, skipped, failed counts
    """
    job_title = config_dict.get("job_title", "")
    location = config_dict.get("location", "")
    profile = config_dict.get("profile", {})
    answers = config_dict.get("answers", {})

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
                headless=False,
            )
            page = browser.new_page()

            # Navigate to LinkedIn jobs search
            search_url = f"https://www.linkedin.com/jobs/search/?keywords={job_title}&location={location}&f_AL=true"
            log.info(f"Navigating to {search_url}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

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
                        result = _fill_and_submit_form(page, profile, answers)

                        if result == "APPLIED":
                            applied += 1
                            log.info(f"Successfully applied to job {idx}")
                        elif result == "SKIPPED":
                            skipped += 1
                            log.info(f"Skipped job {idx}")
                        else:
                            failed += 1
                            log.info(f"Failed to apply to job {idx}")

                        # Go back to job list
                        page.go_back()
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


def _fill_and_submit_form(page, profile: dict, answers: dict) -> str:
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

            # Find all input fields on this step
            inputs = page.query_selector_all("input, select, textarea")
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

            if not inputs:
                log.info("No more form fields found")
                break

            # Fill fields
            for input_elem in inputs:
                try:
                    # Get field label and info
                    field_id = input_elem.get_attribute("id") or ""
                    field_name = input_elem.get_attribute("name") or ""
                    field_type = input_elem.get_attribute("type") or ""
                    placeholder = input_elem.get_attribute("placeholder") or ""

                    label_text = placeholder
                    if field_id:
                        label = page.query_selector(f"label[for='{field_id}']")
                        if label:
                            label_text = label.text_content() or ""

                    # Also check for parent label
                    if not label_text:
                        parent_label = input_elem.query_selector("..")
                        if parent_label:
                            label_elem = parent_label.query_selector("label")
                            if label_elem:
                                label_text = label_elem.text_content() or ""

                    label_lower = label_text.lower()
                    log.debug(f"Field: {label_text[:40]}, type={field_type}, name={field_name}")

                    # Match field to config value
                    value = None

                    # Be specific about phone number (don't match "city")
                    if any(x in label_lower for x in ["phone", "mobile", "contact number"]) and "city" not in label_lower:
                        value = profile.get("phone_number")
                    elif any(x in label_lower for x in ["country code", "country", "dial code"]) and "phone" in label_lower:
                        # Try to get country code from profile
                        phone_country = profile.get("phone_country_code", "+44")
                        # Handle common formats - might be +44, 44, or country name
                        value = phone_country
                    elif any(x in label_lower for x in ["first name", "given name"]):
                        value = profile.get("first_name")
                    elif any(x in label_lower for x in ["last name", "family name", "surname"]):
                        value = profile.get("last_name")
                    elif any(x in label_lower for x in ["email", "e-mail"]):
                        value = profile.get("email")
                    elif any(x in label_lower for x in ["city"]) and "phone" not in label_lower:
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
                        # Try to find matching language experience
                        for lang, years in answers.get("years_experience", {}).items():
                            if lang.lower() in label_lower:
                                value = years
                                break

                    # Fill field
                    if value:
                        # Get element tag name to handle select elements
                        try:
                            tag_name = input_elem.evaluate("el => el.tagName").lower()
                        except:
                            # Fallback if evaluation fails
                            tag_name = "unknown"

                        if field_type in ["checkbox", "radio"]:
                            if str(value).lower() in ["yes", "true", "selected"]:
                                input_elem.check()
                        elif tag_name == "select":
                            # For select dropdowns, try multiple methods
                            log.info(f"Attempting to fill dropdown '{label_text}' with value '{value}'")
                            success = False

                            # Method 1: Try standard select_option first
                            try:
                                input_elem.select_option(str(value))
                                log.info(f"Successfully selected '{label_text}' = '{value}'")
                                success = True
                            except Exception as e:
                                log.debug(f"select_option failed: {e}")

                            # Method 2: Try clicking dropdown and selecting option
                            if not success:
                                try:
                                    input_elem.click()
                                    page.wait_for_timeout(500)
                                    # Look for option that matches the value
                                    option_selector = f"option:has-text('{value}')"
                                    opt = page.query_selector(option_selector)
                                    if not opt:
                                        # Try without the + sign for country codes
                                        alt_value = str(value).replace("+", "")
                                        option_selector = f"option:has-text('{alt_value}')"
                                        opt = page.query_selector(option_selector)

                                    if opt:
                                        opt.click()
                                        page.wait_for_timeout(300)
                                        log.info(f"Successfully clicked option in '{label_text}'")
                                        success = True
                                except Exception as e:
                                    log.debug(f"Click method failed: {e}")

                            if not success:
                                log.warning(f"Could not fill dropdown '{label_text}' with '{value}' - skipping")
                                # Skip this field and continue
                                continue
                        else:
                            # For text inputs and textareas
                            input_elem.clear()
                            input_elem.fill(str(value))
                            log.debug(f"Filled: {label_text[:30]} = {str(value)[:20]}")

                except Exception as e:
                    log.debug(f"Could not fill field: {e}")
                    continue

            # Look for Next button to go to next step
            next_btn = page.query_selector("button:has-text('Next'), button:has-text('Continue'), button[aria-label*='Next']")
            if next_btn and next_btn.is_enabled():
                log.info(f"Clicking Next button on step {step}")
                next_btn.click()
                page.wait_for_timeout(2000)
                continue

            # Look for Resume upload field
            resume_input = page.query_selector("input[type='file']")
            if resume_input:
                resume_path = answers.get("resume_path") or profile.get("resume_path") or "data/resume.pdf"
                log.info(f"Uploading resume: {resume_path}")
                try:
                    resume_input.set_input_files(resume_path)
                    page.wait_for_timeout(2000)
                except Exception as e:
                    log.warning(f"Could not upload resume: {e}")

            # Look for final Submit/Apply button
            submit_btn = page.query_selector(
                "button:has-text('Submit'), button:has-text('Apply'), button:has-text('Send'), button[type='submit'], button:has-text('Finish')"
            )
            if submit_btn and submit_btn.is_enabled():
                log.info(f"Clicking Submit button on step {step}")
                submit_btn.click()
                page.wait_for_timeout(2000)

                # Check for success message
                page.wait_for_timeout(1000)
                content = page.content().lower()
                if any(x in content for x in ["application sent", "thank you", "confirmed", "successfully"]):
                    log.info("Application submitted successfully!")
                    return "APPLIED"

                return "APPLIED"  # Assume success

            # If no Next and no Submit, we're done
            log.info(f"No more buttons found on step {step}")
            break

        return "APPLIED"

    except Exception as e:
        log.error(f"Error filling form: {e}")
        import traceback
        traceback.print_exc()
        return "FAILED"
