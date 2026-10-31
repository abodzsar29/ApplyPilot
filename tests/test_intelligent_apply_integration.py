"""Runner order and handoff tests with mocked browsers and preparation."""

import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import intelligent_apply as ia
from applypilot.linkedin import non_easy as ne
from applypilot.linkedin import unified_apply as easy
from applypilot.linkedin.scrape import parse_job

DESCRIPTION = "We are looking for a candidate with Python experience and communication skills for our team."


def prepared(number):
    result = Mock(finalized=False, text=f"Fresh CV text {number}", pdf_sha256=f"sha-{number}")
    result.published_pdf = Path(f"/fixture/data/generated-{number}.pdf")
    return result


class IntelligentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.session = Mock()
        manager = self.stack.enter_context(patch.object(ia, "IntelligentApplySession"))
        manager.return_value.__enter__.return_value = self.session
        self.events = []
        self.history_root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(easy.config, "DB_PATH", self.history_root / "applypilot.db"))
        self.stack.enter_context(patch.object(easy.config, "APP_DIR", self.history_root))

    def run_easy(self, *, descriptions=None, card_titles=None, detail_titles=None, config_overrides=None,
                 preparation_error=None, result="APPLIED",
                 replace_control_during_preparation=False, selected_job_after_preparation=None):
        descriptions = descriptions or [DESCRIPTION, DESCRIPTION]
        page = Mock(url="https://www.linkedin.com/jobs/search-results/")
        browser = Mock()
        browser.new_page.return_value = page
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = browser
        manager = self.stack.enter_context(patch.object(easy, "sync_playwright"))
        manager.return_value.__enter__.return_value = playwright
        cards = [Mock() for _ in descriptions]
        for index, card in enumerate(cards):
            card.job_url = f"https://www.linkedin.com/jobs/view/{100 + index}/"
            card.job_title = card_titles[index] if card_titles is not None else "Engineer"
        self.stack.enter_context(patch.object(easy, "_find_linkedin_job_cards", return_value=cards))
        self.stack.enter_context(patch.object(easy, "_extract_job_url_from_card", side_effect=lambda card: card.job_url))
        self.stack.enter_context(patch.object(easy, "_extract_job_title_from_card", side_effect=lambda card: card.job_title))
        for name in ("_scroll_linkedin_job_card_into_view", "_restore_job_detail_header",
                     "_log_active_linkedin_search_state", "_wait_for_easy_apply_form_ready", "_close_easy_apply_modal"):
            self.stack.enter_context(patch.object(easy, name))
        self.stack.enter_context(patch.object(easy, "_ensure_configured_linkedin_search_location", return_value=True))
        self.stack.enter_context(patch.object(easy, "_active_linkedin_search_location", return_value="Berlin"))
        self.stack.enter_context(patch.object(easy, "_dismiss_recruiter_upsell_if_present", return_value=False))
        self.stack.enter_context(patch.object(easy, "_find_easy_apply_limit_modal", return_value=None))
        self.stack.enter_context(patch.object(easy, "_extract_company_name", return_value="Employer"))
        self.stack.enter_context(patch.object(easy, "_find_linkedin_next_page_button", return_value=None))
        active = {"job_id": None, "control": None}
        self.original_control = Mock()
        self.current_control = Mock()
        self.current_control.click.side_effect = lambda **kwargs: self.events.append("click")
        if replace_control_during_preparation:
            self.original_control.click.side_effect = easy.PlaywrightError("Element is not attached to the DOM")
        else:
            self.original_control = self.current_control
        def select_card(card):
            active.update(job_id=easy._linkedin_job_id(card.job_url), control=self.original_control)
        self.card_selector = self.stack.enter_context(patch.object(easy, "_click_linkedin_job_card", side_effect=select_card))
        self.stack.enter_context(patch.object(easy, "_current_linkedin_job_id", side_effect=lambda page: active["job_id"]))
        self.control_finder = self.stack.enter_context(patch.object(
            easy, "_find_easy_apply_control", side_effect=lambda *args, **kwargs: active["control"],
        ))
        details = {str(100 + index): {
            "job_id": str(100 + index), "title": detail_titles[index] if detail_titles is not None else "Engineer",
            "company": "Employer", "description": description,
        } for index, description in enumerate(descriptions)}
        self.stack.enter_context(patch("applypilot.linkedin.scrape.read_current_job",
                                      side_effect=lambda page, url: details[easy._linkedin_job_id(url)]))
        generated = [prepared(index) for index in range(len(descriptions))]
        def prepare(job):
            self.events.append("prepare")
            if preparation_error:
                raise preparation_error
            active["control"] = self.current_control
            if selected_job_after_preparation:
                active["job_id"] = selected_job_after_preparation
            return generated[int(job["job_id"]) - 100]
        self.session.prepare.side_effect = prepare
        def fill(*args, **kwargs):
            self.events.append("fill")
            return result
        filler = self.stack.enter_context(patch.object(easy, "_fill_and_submit_form", side_effect=fill))
        settings = {"job_title": "Engineer", "location": "Berlin", "resume_path": "/old.pdf",
                    "resume_paths": {"english": "/wrong-language-cv.pdf"}, "profile": {}, "answers": {},
                    "only_english": True, "intelligent_apply": True, "company_cooldown_days": 0} | (config_overrides or {})
        summary = easy.search_and_apply(settings, max_applications=len(descriptions), dry_run=result == "READY",
                                        title_keyword=settings.get("title_keyword", ""))
        return summary, filler, generated

    def test_easy_uses_each_fresh_cv_before_click_and_overrides_language_mapping(self):
        summary, filler, generated = self.run_easy()
        self.assertEqual(summary["applied"], 2)
        self.assertEqual(self.events, ["prepare", "click", "fill", "prepare", "click", "fill"])
        for index, call in enumerate(filler.call_args_list):
            self.assertEqual(call.args[3], str(generated[index].published_pdf))
            self.assertTrue(call.kwargs["require_fresh_resume"])
            generated[index].verify_published.assert_called_once()
            generated[index].finish.assert_called_once_with("applied")

    def test_easy_language_filter_runs_before_preparation(self):
        summary, filler, _ = self.run_easy(descriptions=["Wir suchen eine Person mit Erfahrung und Deutschkenntnissen."])
        self.assertEqual(summary["skipped"], 1)
        self.session.prepare.assert_not_called()
        filler.assert_not_called()
        self.assertEqual(self.events, [])

    def test_easy_ignored_title_skips_before_job_open_or_resume_preparation(self):
        summary, filler, generated = self.run_easy(
            card_titles=["AI SCIENTIST", "Software Engineer"], result="READY",
            config_overrides={"to_ignore": "sCiEnTiSt"},
        )
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["ready"], 1)
        self.assertEqual(summary["failed"], 0)
        self.card_selector.assert_called_once()
        self.assertEqual(self.card_selector.call_args.args[0].job_title, "Software Engineer")
        self.session.prepare.assert_called_once()
        self.assertEqual(self.session.prepare.call_args.args[0]["job_id"], "101")
        filler.assert_called_once()
        generated[0].verify_published.assert_not_called()
        self.assertEqual(self.events, ["prepare", "click", "fill"])

    def test_easy_ignored_detail_title_skips_before_resume_preparation(self):
        summary, filler, _ = self.run_easy(
            descriptions=[DESCRIPTION], card_titles=["Climate Simulation ..."],
            detail_titles=["Climate Simulation & AI Scientist, Built Environment"],
            config_overrides={"to_ignore": "Scientist"},
        )
        self.assertEqual(summary["skipped"], 1)
        self.session.prepare.assert_not_called()
        filler.assert_not_called()
        self.assertEqual(self.events, [])

    def test_easy_exclusion_works_without_intelligent_apply_and_overrides_positive_keyword(self):
        summary, filler, _ = self.run_easy(
            descriptions=[DESCRIPTION], card_titles=["Senior AI Engineer"],
            config_overrides={"intelligent_apply": False, "title_keyword": "AI", "to_ignore": "senior"},
        )
        self.assertEqual(summary["skipped"], 1)
        self.card_selector.assert_not_called()
        filler.assert_not_called()

    def test_easy_description_text_does_not_trigger_title_exclusion(self):
        summary, filler, _ = self.run_easy(
            descriptions=[DESCRIPTION + " Work with a scientist."], card_titles=["Software Engineer"],
            result="READY", config_overrides={"to_ignore": "scientist"},
        )
        self.assertEqual(summary["ready"], 1)
        self.assertEqual(summary["skipped"], 0)
        filler.assert_called_once()

    def test_easy_blank_title_exclusion_leaves_job_eligible(self):
        summary, _, _ = self.run_easy(
            descriptions=[DESCRIPTION], card_titles=["AI Scientist"], result="READY",
            config_overrides={"to_ignore": "  "},
        )
        self.assertEqual(summary["ready"], 1)
        self.assertEqual(summary["skipped"], 0)

    def test_easy_list_exclusions_match_any_entry_before_preparation(self):
        summary, filler, _ = self.run_easy(
            card_titles=["Junior Software Engineer", "TEAM LEAD"],
            detail_titles=["Junior Software Engineer", "TEAM LEAD"], result="READY",
            config_overrides={"to_ignore": ["senior", "lEaD", "scientist"]},
        )
        self.assertEqual(summary["ready"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.card_selector.assert_called_once()
        self.session.prepare.assert_called_once()
        filler.assert_called_once()

    def test_easy_list_exclusions_recheck_full_detail_title(self):
        summary, filler, _ = self.run_easy(
            descriptions=[DESCRIPTION], card_titles=["Climate Simulation ..."],
            detail_titles=["Climate Simulation & AI SCIENTIST"],
            config_overrides={"to_ignore": ["senior", "Scientist"]},
        )
        self.assertEqual(summary["skipped"], 1)
        self.session.prepare.assert_not_called()
        filler.assert_not_called()

    def test_easy_preparation_failure_never_clicks_and_fatal_error_stops(self):
        summary, filler, _ = self.run_easy(preparation_error=ia.PreparationError("compile", "failed", fatal=True))
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["stopped_reason"], "intelligent_compile_failed")
        self.assertEqual(self.events, ["prepare"])
        filler.assert_not_called()

    def test_easy_dry_run_is_logged_as_ready(self):
        summary, filler, generated = self.run_easy(descriptions=[DESCRIPTION], result="READY")
        self.assertEqual(summary["applied"], 0)
        self.assertEqual(summary["ready"], 1)
        self.assertTrue(filler.call_args.kwargs["dry_run"])
        generated[0].finish.assert_called_once_with("ready")

    def test_easy_reacquires_detached_control_without_repeating_resume_preparation(self):
        summary, filler, generated = self.run_easy(descriptions=[DESCRIPTION], result="READY",
                                                  replace_control_during_preparation=True)
        self.assertEqual(summary["ready"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(self.events, ["prepare", "click", "fill"])
        self.session.prepare.assert_called_once()
        self.original_control.click.assert_not_called()
        self.current_control.click.assert_called_once()
        self.assertEqual(self.control_finder.call_count, 2)
        filler.assert_called_once()
        generated[0].finish.assert_called_once_with("ready")

    def test_easy_never_hands_a_prepared_resume_to_another_selected_job(self):
        summary, filler, generated = self.run_easy(descriptions=[DESCRIPTION], selected_job_after_preparation="999")
        self.assertEqual(summary["failed"], 1)
        self.current_control.click.assert_not_called()
        filler.assert_not_called()
        self.assertEqual(self.events, ["prepare"])
        generated[0].finish.assert_called_once_with("failed")

    def non_easy_harness(self):
        harness = existing_non_easy.NonEasyTests("test_only_english_rejects_german_and_unknown")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        fixture = parse_job(existing_non_easy.FIXTURE.read_text(), existing_non_easy.JOB_URL)
        self.stack.enter_context(patch("applypilot.linkedin.scrape.read_current_job", return_value=fixture))
        return harness

    def test_non_easy_passes_matching_pdf_text_and_records_success_after_handoff(self):
        harness = self.non_easy_harness()
        a, b = prepared(1), prepared(2)
        self.session.prepare.side_effect = [a, b]
        summary, opener, runner = harness.run_session(settings=harness.settings | {"intelligent_apply": True}, jobs=2)
        self.assertEqual(summary["applied"], 2)
        self.assertEqual(opener.call_count, 2)
        for result, call in zip((a, b), runner.call_args_list):
            settings = call.kwargs["config_dict"]
            self.assertEqual(settings["resume_path"], str(result.published_pdf))
            self.assertEqual(settings["_intelligent_resume_text"], result.text)
            result.verify_published.assert_called_once()
            result.finish.assert_called_once_with("applied")
        self.assertEqual(len(ne._load_applied_job_keys()), 2)

    def test_non_easy_preparation_failure_does_not_run_agent_or_record_history(self):
        harness = self.non_easy_harness()
        self.session.prepare.side_effect = ia.PreparationError("llm_response", "invalid")
        summary, opener, runner = harness.run_session(settings=harness.settings | {"intelligent_apply": True})
        self.assertEqual(summary["failed"], 1)
        opener.assert_called_once()  # Inspect the destination before paying to prepare a CV.
        runner.assert_not_called()
        self.assertFalse(ne._history_path().exists())

    def test_non_easy_ignored_title_skips_intelligent_preparation_and_external_handoff(self):
        harness = self.non_easy_harness()
        summary, opener, runner = harness.run_session(
            settings=harness.settings | {"intelligent_apply": True, "to_ignore": "eNgInEeR"},
        )
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["search_stats"]["title_ignored"], 1)
        self.session.prepare.assert_not_called()
        opener.assert_not_called()
        runner.assert_not_called()
        self.assertFalse(ne._history_path().exists())

    def test_non_easy_dry_run_matching_text_ignores_stale_sibling(self):
        harness = self.non_easy_harness()
        result = prepared(1)
        result.published_pdf = harness.root / "english.pdf"
        self.session.prepare.return_value = result
        summary, _, runner = harness.run_session(settings=harness.settings | {"intelligent_apply": True},
                                                 result="ready", dry_run=True)
        self.assertEqual(summary["applied"], 0)
        self.assertEqual(summary["ready"], 1)
        settings = runner.call_args.kwargs["config_dict"]
        prompt = ne._build_prompt(harness.job, settings, dry_run=True)
        self.assertIn("Fresh CV text 1", prompt)
        self.assertNotIn("English CV contents", prompt)
        self.assertIn("do not select a previously saved", prompt)
        self.assertFalse(ne._history_path().exists())

    def test_setup_bypasses_intelligent_settings_and_session_creation(self):
        calls = []
        @ia.intelligent_session
        def runner(config_dict, setup=False):
            calls.append(config_dict)
        runner({"intelligent_apply": "invalid but ignored in setup"}, setup=True)
        self.assertEqual(len(calls), 1)


class EasyApplyOpeningTests(unittest.TestCase):
    def test_transient_detachment_reacquires_control_with_bounded_click_retries(self):
        page = Mock()
        stale, current = Mock(), Mock()
        stale.click.side_effect = easy.PlaywrightError("Element is not attached to the DOM")
        record = Mock()
        with (patch.object(easy, "_current_linkedin_job_id", return_value="123"),
              patch.object(easy, "_restore_job_detail_header"),
              patch.object(easy, "_get_active_modal", return_value=None),
              patch.object(easy, "_find_easy_apply_control", side_effect=[stale, current]) as finder):
            easy._open_easy_apply_for_job(page, "https://www.linkedin.com/jobs/view/123/", record=record)
        self.assertEqual(finder.call_count, 2)
        stale.click.assert_called_once()
        current.click.assert_called_once()
        self.assertLessEqual(current.click.call_args.kwargs["timeout"], 2000)
        self.assertNotIn("force", current.click.call_args.kwargs)
        self.assertIn("RETRY", [call.args[1] for call in record.call_args_list])

    def test_persistent_detachment_fails_after_three_click_attempts(self):
        page, control = Mock(), Mock()
        control.click.side_effect = easy.PlaywrightError("Element is not attached to the DOM")
        with (patch.object(easy, "_current_linkedin_job_id", return_value="123"),
              patch.object(easy, "_restore_job_detail_header"),
              patch.object(easy, "_get_active_modal", return_value=None),
              patch.object(easy, "_find_easy_apply_control", return_value=control),
              self.assertRaisesRegex(RuntimeError, "Could not open Easy Apply")):
            easy._open_easy_apply_for_job(page, "https://www.linkedin.com/jobs/view/123/")
        self.assertEqual(control.click.call_count, 3)

    def test_control_can_mount_late_without_repeating_other_steps(self):
        page, control = Mock(), Mock()
        with (patch.object(easy, "_current_linkedin_job_id", return_value="123"),
              patch.object(easy, "_restore_job_detail_header"),
              patch.object(easy, "_find_easy_apply_control", side_effect=[None, None, control])):
            easy._open_easy_apply_for_job(page, "https://www.linkedin.com/jobs/view/123/")
        control.click.assert_called_once()
        self.assertEqual(page.wait_for_timeout.call_count, 2)

    def test_timeout_preserves_failure_when_no_control_appears(self):
        page = Mock()
        with (patch.object(easy, "_current_linkedin_job_id", return_value="123"),
              patch.object(easy, "_restore_job_detail_header"),
              patch.object(easy, "_find_easy_apply_control", return_value=None),
              self.assertRaisesRegex(RuntimeError, "Could not open Easy Apply")):
            easy._open_easy_apply_for_job(page, "https://www.linkedin.com/jobs/view/123/", timeout_ms=1)

    def test_existing_dialog_after_transient_click_error_is_not_clicked_twice(self):
        page, control = Mock(), Mock()
        control.click.side_effect = easy.PlaywrightError("Element is not attached to the DOM")
        with (patch.object(easy, "_current_linkedin_job_id", return_value="123"),
              patch.object(easy, "_restore_job_detail_header"),
              patch.object(easy, "_get_active_modal", return_value=Mock()),
              patch.object(easy, "_find_easy_apply_control", return_value=control) as finder):
            easy._open_easy_apply_for_job(page, "https://www.linkedin.com/jobs/view/123/")
        finder.assert_called_once()
        control.click.assert_called_once()

    def test_selection_change_during_header_restore_prevents_wrong_job_click(self):
        page, control = Mock(), Mock()
        with (patch.object(easy, "_current_linkedin_job_id", side_effect=["123", "999", "999"]),
              patch.object(easy, "_restore_job_detail_header"),
              patch.object(easy, "_find_easy_apply_control", return_value=control),
              self.assertRaisesRegex(RuntimeError, "Selected job changed from 123 to 999")):
            easy._open_easy_apply_for_job(page, "https://www.linkedin.com/jobs/view/123/")
        control.click.assert_not_called()


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1 for browser fixtures")
class FreshResumeBrowserTests(unittest.TestCase):
    def test_all_attachment_buttons_and_native_fields_get_current_cv_once(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        fixture = Path(__file__).parent / "fixtures" / "linkedin_attachment_uploads.html"
        page.set_content(fixture.read_text())
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            contents = "%PDF-1.7\nFresh CV reused for every document"
            pdf.write_text(contents)
            with (patch.object(page, "wait_for_timeout"),
                  patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
                result = easy._fill_and_submit_form(page, {}, {}, str(pdf), dry_run=True, require_fresh_resume=True)
        self.assertEqual(result, "READY")
        self.assertEqual(page.evaluate("window.uploads"), {"resume": 1, "evidence": 1, "transcript": 1, "certificate": 1})
        for name in ("resume", "evidence", "transcript", "certificate"):
            self.assertEqual(page.evaluate("name => window.contents[name]", name), contents)
        self.assertEqual(page.evaluate("window.attachmentNextClicks"), 2)
        self.assertEqual(page.evaluate("window.submitClicks"), 0)

    def test_unfamiliar_native_uploads_replace_old_files_with_current_cv(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<div role="dialog"><h2>Documents</h2><form>
            <label>Transcript<input id="transcript" type="file" accept=".pdf" required></label>
            <label>Novel evidence type<input id="evidence" type="file" required></label>
            <label>Resume<input id="resume" type="file" accept=".pdf" required></label>
            <input id="disabled" type="file" disabled>
            <button type="button">Submit application</button></form></div>''')
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            contents = "%PDF-1.7\nThe CV for this job"
            pdf.write_text(contents)
            old = Path(directory) / "old-transcript.pdf"
            old.write_text("%PDF-1.7\nOld file, not this job's CV")
            page.query_selector("#transcript").set_input_files(str(old))
            with (patch.object(page, "wait_for_timeout"),
                  patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
                result = easy._fill_and_submit_form(page, {}, {}, str(pdf), dry_run=True, require_fresh_resume=True)
            self.assertEqual(result, "READY")
            for selector in ("#transcript", "#evidence", "#resume"):
                self.assertEqual(page.locator(selector).evaluate("el => el.files[0].name"), pdf.name)
                self.assertEqual(page.locator(selector).evaluate("el => el.files[0].text()"), contents)
            self.assertEqual(page.locator("#disabled").evaluate("el => el.files.length"), 0)

    def test_transcript_upload_does_not_satisfy_fresh_resume_requirement(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            pdf.write_text("%PDF-1.7\nCurrent CV")
            for use_button in (False, True):
                with self.subTest(use_button=use_button):
                    controls = ('<button id="upload">Upload Transcript</button>' if use_button else
                                '<label>Transcript<input id="transcript" type="file" accept=".pdf" required></label>')
                    page.set_content('<div role="dialog"><p>Saved resume: AndrewBodzsarCV.pdf</p>' + controls +
                                     '<button>Submit application</button></div>' +
                                     ('<input id="picker" type="file" accept=".pdf" hidden>' if use_button else ''))
                    if use_button:
                        page.evaluate("document.querySelector('#upload').onclick = () => document.querySelector('#picker').click()")
                    with (patch.object(page, "wait_for_timeout"),
                          patch.object(easy, "_click_action_button") as click,
                          patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
                        result = easy._fill_and_submit_form(page, {}, {}, str(pdf), dry_run=True, require_fresh_resume=True)
                    self.assertEqual(result, "FAILED")
                    self.assertEqual(page.locator('input[type="file"]').evaluate("el => el.files[0].name"), pdf.name)
                    click.assert_not_called()

    def test_unfamiliar_upload_button_is_actionable_with_disabled_next(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<div role="dialog"><p>Transcript*</p>
            <button type="button">Upload Transcript</button><button disabled>Next</button></div>''')
        self.assertTrue(easy._scope_has_easy_apply_controls(page.query_selector('[role="dialog"]')))

    def test_each_button_can_reuse_a_shared_file_picker_inside_or_outside_the_modal(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            pdf.write_text("%PDF-1.7\nCurrent CV for all shared-picker slots")
            for local_picker in (False, True):
                with self.subTest(local_picker=local_picker):
                    picker = '<input id="shared-picker" type="file" accept=".pdf" hidden>'
                    page.set_content('''<div role="dialog"><h2>Documents</h2>
                        <label>Resume<input id="resume" type="file" accept=".pdf" hidden></label>
                        <section><p>Transcript*</p><button type="button" id="transcript"
                            aria-controls="shared-picker">Upload Transcript</button></section>
                        <section><p>Certificate*</p><button type="button" id="certificate"
                            aria-controls="shared-picker">Upload Certificate</button></section>'''
                                     + (picker if local_picker else '') + '</div>' + ('' if local_picker else picker))
                    page.evaluate('''() => {
                        window.uploads = {};
                        document.querySelector('#resume').onchange = () => window.uploads.resume = 1;
                        for (const name of ['transcript', 'certificate']) {
                            document.getElementById(name).onclick = () => {
                                window.targetSlot = name;
                                document.querySelector('#shared-picker').click();
                            };
                        }
                        document.querySelector('#shared-picker').onchange = () => {
                            const slot = window.targetSlot || 'incorrect-direct-upload';
                            window.uploads[slot] = (window.uploads[slot] || 0) + 1;
                        };
                    }''')
                    completed = set()
                    with patch.object(page, "wait_for_timeout"):
                        scope = page.query_selector('[role="dialog"]')
                        self.assertEqual(easy._upload_application_attachments(page, scope, str(pdf), "", completed), (True, True))
                        self.assertEqual(easy._upload_application_attachments(page, scope, str(pdf), "", completed), (True, False))
                    self.assertEqual(page.evaluate("window.uploads"), {"resume": 1, "transcript": 1, "certificate": 1})

    def test_reused_unlabelled_file_input_on_new_question_still_receives_cv(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        completed = set()
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            pdf.write_text("%PDF-1.7\nCurrent CV")
            for question in ("Transcript*", "Certificate*"):
                page.set_content(f'''<div role="dialog"><h2>Additional documents</h2>
                    <p>{question}</p><input id="_r_cv_" type="file" accept=".pdf" hidden></div>''')
                with patch.object(page, "wait_for_timeout"):
                    success, _ = easy._upload_application_attachments(
                        page, page.query_selector('[role="dialog"]'), str(pdf), "", completed,
                    )
                self.assertTrue(success)
                self.assertEqual(page.locator('input[type="file"]').evaluate("el => el.files[0].name"), pdf.name)

    def test_cover_letter_button_is_actionable_when_next_is_disabled_and_no_file_input_exists(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<div role="dialog"><div>
            <fieldset aria-describedby="error-message-_r_hm_" role="radiogroup">
                <div id="easyApplyUploadedCoverLetterRef" componentkey="easyApplyUploadedCoverLetterRef"></div>
            </fieldset>
            <button type="button"><span><span>Upload cover letter</span></span></button>
            <p>A cover letter is required</p></div>
            <button disabled>Next</button></div>''')
        modal = page.query_selector('[role="dialog"]')
        self.assertTrue(easy._scope_has_easy_apply_controls(modal))
        self.assertIsNotNone(easy._wait_for_easy_apply_form_ready(page, timeout_ms=500))

    def test_cover_letter_button_uploads_the_same_fresh_cv_on_a_later_step_once(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        fixture = Path(__file__).parent / "fixtures" / "linkedin_cover_letter_upload.html"
        page.set_content(fixture.read_text())
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            contents = "%PDF-1.7\nFresh job-specific CV fixture"
            pdf.write_text(contents)
            with (patch.object(page, "wait_for_timeout"),
                  patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
                result = easy._fill_and_submit_form(page, {}, {}, str(pdf), dry_run=True, require_fresh_resume=True)
        self.assertEqual(result, "READY")
        self.assertEqual(page.evaluate("window.resumeUploads"), 1)
        self.assertEqual(page.evaluate("window.coverUploads"), 1)
        self.assertEqual(page.evaluate("window.coverNextClicks"), 2)
        self.assertEqual(page.evaluate("window.submitClicks"), 0)
        self.assertEqual(page.evaluate("window.resumeContents"), contents)
        self.assertEqual(page.evaluate("window.coverContents"), contents)
        self.assertIn("Review your application", page.inner_text('[role="dialog"]'))

    def test_cover_letter_and_resume_file_inputs_are_distinct_and_both_get_current_cv(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<div role="dialog"><form>
            <label>Cover letter<input id="cover" name="cover_letter" type="file" accept=".pdf" required></label>
            <label>Resume<input id="resume" name="resume" type="file" accept="application/pdf" required></label>
            <fieldset><legend>Optional photo</legend><input id="photo" type="file" accept="image/*"></fieldset>
            <button type="button">Submit application</button>
            </form></div>''')
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            contents = "%PDF-1.7\nCurrent CV; not an older saved cover letter"
            pdf.write_text(contents)
            old = Path(directory) / "old-cover.pdf"
            old.write_text("%PDF-1.7\nOld cover letter")
            page.query_selector("#cover").set_input_files(str(old))
            with (patch.object(page, "wait_for_timeout"),
                  patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
                result = easy._fill_and_submit_form(page, {}, {}, str(pdf), dry_run=True, require_fresh_resume=True)
            self.assertEqual(result, "READY")
            for selector in ("#cover", "#resume"):
                self.assertEqual(page.locator(selector).evaluate("el => el.files[0].name"), pdf.name)
                self.assertEqual(page.locator(selector).evaluate("el => el.files[0].text()"), contents)
            self.assertEqual(page.locator("#photo").evaluate("el => el.files.length"), 0)

    def test_cover_letter_input_with_generated_cv_id_uses_its_own_section_context(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<div role="dialog"><div>
            <fieldset><div id="easyApplyUploadedCoverLetterRef"></div></fieldset>
            <input id="_r_cv_" type="file" accept=".pdf" hidden>
            <button>Upload cover letter</button><p>A cover letter is required</p>
            </div></div>''')
        scope = page.query_selector('[role="dialog"]')
        cover = page.query_selector('[id="_r_cv_"]')
        self.assertFalse(easy._is_resume_upload_input(scope, cover, "data/AndrewBodzsarCV.pdf"))
        self.assertEqual(easy._find_cover_letter_upload_input(scope).get_attribute("id"), "_r_cv_")

    def test_cover_letter_upload_does_not_satisfy_the_fresh_resume_requirement(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<div role="dialog"><p>Saved resume: AndrewBodzsarCV.pdf</p>
            <label>Cover letter<input id="cover" type="file" accept=".pdf" required></label>
            <button>Submit application</button></div>''')
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            pdf.write_text("%PDF-1.7\nCurrent CV fixture")
            with (patch.object(page, "wait_for_timeout"),
                  patch.object(easy, "_click_action_button") as click,
                  patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
                result = easy._fill_and_submit_form(page, {}, {}, str(pdf), dry_run=True, require_fresh_resume=True)
            self.assertEqual(result, "FAILED")
            self.assertEqual(page.locator("#cover").evaluate("el => el.files[0].name"), pdf.name)
            click.assert_not_called()

    def test_cover_letter_upload_failure_does_not_advance_or_submit(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            pdf.write_text("%PDF-1.7\nCurrent CV fixture")
            for cv_path, accept in (("", ".pdf"), (str(pdf), ".docx")):
                with self.subTest(cv_path=bool(cv_path), accept=accept):
                    page.set_content(f'''<div role="dialog"><label>Cover letter
                        <input id="cover" type="file" accept="{accept}" required></label>
                        <button>Submit application</button></div>''')
                    with (patch.object(page, "wait_for_timeout"),
                          patch.object(easy, "_click_action_button") as click,
                          patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
                        result = easy._fill_and_submit_form(page, {}, {}, cv_path, dry_run=True)
                    self.assertEqual(result, "FAILED")
                    self.assertEqual(page.locator("#cover").evaluate("el => el.files.length"), 0)
                    click.assert_not_called()

    def test_reported_sdui_checkbox_option_text_is_not_the_empty_label(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        fixture = Path(__file__).parent / "fixtures" / "linkedin_sdui_checkbox_questions.html"
        page.set_content(fixture.read_text())
        self.assertEqual(easy._choice_option_text(page.query_selector('[id="_r_bd_"]'), "checkbox"), "LinkedIn")
        self.assertEqual(easy._choice_option_text(page.query_selector('[id="_r_bs_"]'), "checkbox"), "In Germany, but not Berlin")
        self.assertEqual(easy._choice_option_text(page.query_selector('[id="_r_c5_"]'), "checkbox"), "I choose not to identify")

    def test_reported_sdui_checkbox_form_selects_one_per_question_and_advances(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        fixture = Path(__file__).parent / "fixtures" / "linkedin_sdui_checkbox_questions.html"
        for replace in (False, True):
            with self.subTest(replace=replace):
                page.set_content(fixture.read_text())
                page.evaluate("replace => { window.replaceFieldsetsOnChange = replace; }", replace)
                started = time.monotonic()
                with (patch.object(page, "wait_for_timeout"),
                      patch.object(easy.random_module, "choice", side_effect=lambda options: options[-1]) as chooser,
                      patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
                    result = easy._fill_and_submit_form(page, {"city": "Berlin, Germany"},
                                                        {"What are your salary expectations?": "1"}, dry_run=True)
                self.assertLess(time.monotonic() - started, 10)
                self.assertEqual(result, "SKIPPED")  # Dry run reaches review, never submits.
                self.assertIn("Review your application", page.inner_text('[role="dialog"]'))
                self.assertEqual(page.evaluate("window.nextClicks"), 1)
                self.assertEqual(page.evaluate("window.choiceChanges"), 4)
                self.assertEqual(chooser.call_count, 4)
                self.assertEqual(page.evaluate("window.snapshot"), [
                    {"question": "How did you hear about Distribusion?*", "selected": ["Other"]},
                    {"question": "What is your preferred working setup?*", "selected": ["In Germany, but not Berlin"]},
                    {"question": "Gender identity*", "selected": ["Other"]},
                    {"question": "Age range*", "selected": ["Other"]},
                ])
                self.assertEqual(page.evaluate("window.textSnapshot"), {
                    "_r_bm_": "a", "_r_bn_": "Berlin, Germany", "_r_bt_": "1", "_r_bu_": "1", "_r_bv_": "a",
                })

    def test_reported_sdui_checkbox_validation_repair_preserves_random_choices(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        fixture = Path(__file__).parent / "fixtures" / "linkedin_sdui_checkbox_questions.html"
        page.set_content(fixture.read_text())
        page.evaluate("window.replaceFieldsetsOnChange = true")
        scope = page.query_selector('[role="dialog"]')
        with patch.object(easy.random_module, "choice", side_effect=lambda options: options[-1]) as chooser:
            for _ in range(2):
                easy._repair_required_fields(scope, {}, {}, page=page)
            self.assertEqual(chooser.call_count, 4)
        self.assertEqual(page.evaluate("window.choiceChanges"), 4)
        self.assertEqual(page.evaluate("[...document.querySelectorAll('fieldset')].map(group => group.querySelectorAll('input:checked').length)"),
                         [1, 1, 1, 1])
        self.assertTrue(page.locator("#terms").is_checked())
        self.assertEqual(page.locator("#hidden-token").input_value(), "preserve-this")

    def test_sdui_checkbox_choice_rule_prefers_yes_then_ja_and_configured_answer(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        for labels, configured, expected in ((["No", "Ja", "Yes"], {}, "Yes"),
                                              (["Nein", "Ja", "Other"], {}, "Ja"),
                                              (["No", "Ja", "Yes"], {"Unseen question": "No"}, "No")):
            with self.subTest(labels=labels, configured=configured):
                page.set_content('''<div role="dialog"><div><p>Unseen question*</p><fieldset><div>
                    <div><div><input id="no" type="checkbox"><label for="no"></label></div><p></p></div>
                    <div><div><input id="ja" type="checkbox"><label for="ja"></label></div><p></p></div>
                    <div><div><input id="yes" type="checkbox"><label for="yes"></label></div><p></p></div>
                    </div></fieldset></div></div>''')
                page.evaluate("labels => document.querySelectorAll('fieldset p').forEach((p, index) => { p.textContent = labels[index]; })", labels)
                scope = page.query_selector('[role="dialog"]')
                with patch.object(easy.random_module, "choice", side_effect=AssertionError("No random choice is needed")):
                    key, options = easy._checkbox_question_options(page.query_selector("#no"))
                    self.assertEqual(len(options), 3)
                    self.assertTrue(key.startswith("checkbox:"))
                    self.assertTrue(easy._answer_checkbox_question(scope, options, "Unseen question*", {}, configured))
                selected = page.query_selector("input:checked")
                self.assertEqual(easy._choice_option_text(selected, "checkbox"), expected)
                self.assertEqual(page.locator("input:checked").count(), 1)

    def test_checkbox_group_excludes_hidden_disabled_options_and_preserves_independent_consents(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<div role="dialog">
            <div><p>Unseen question*</p><fieldset>
                <label><input id="disabled" type="checkbox" disabled>Yes</label>
                <label style="display:none"><input id="hidden" type="checkbox">Ja</label>
                <label><input id="first" type="checkbox">First</label>
                <label><input id="second" type="checkbox">Second</label>
            </fieldset></div>
            <fieldset><legend>Independent consents</legend>
                <label><input id="privacy" type="checkbox" required>I agree to the privacy policy*</label>
                <label><input id="terms" type="checkbox" required>I agree to the terms*</label>
            </fieldset>
            <label><input id="follow-company-checkbox" type="checkbox">Follow this company</label>
        </div>''')
        scope = page.query_selector('[role="dialog"]')
        with patch.object(easy.random_module, "choice", side_effect=lambda options: options[-1]) as chooser:
            easy._repair_required_fields(scope, {}, {}, page=page)
        self.assertTrue(page.locator("#second").is_checked())
        self.assertEqual(chooser.call_count, 1)
        for selector in ("#disabled", "#hidden", "#first", "#follow-company-checkbox"):
            self.assertFalse(page.locator(selector).is_checked())
        self.assertTrue(page.locator("#privacy").is_checked())
        self.assertTrue(page.locator("#terms").is_checked())

    def test_unseen_multichoice_questions_use_random_enabled_options_and_advance(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_default_timeout(30000)
        fixture = Path(__file__).parent / "fixtures" / "linkedin_multichoice_questions.html"
        for mode in ("custom", "radio", "select"):
            with self.subTest(mode=mode):
                page.set_content(fixture.read_text())
                page.evaluate("mode => window.renderForm(mode)", mode)
                started = time.monotonic()
                with (patch.object(page, "wait_for_timeout"),
                      patch.object(easy.random_module, "choice", side_effect=lambda options: options[-1]) as chooser,
                      patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
                    result = easy._fill_and_submit_form(page, {"city": "Berlin, Germany"}, {}, dry_run=True)
                self.assertLess(time.monotonic() - started, 10)
                self.assertEqual(result, "SKIPPED")  # Stops at review without submitting.
                self.assertIn("Review your application", page.inner_text('[role="dialog"]'))
                self.assertEqual(page.evaluate("window.nextClicks"), 1)
                self.assertEqual(chooser.call_count, 4, "Choose once per unknown choice question")
                self.assertEqual(page.evaluate("window.snapshot"), {
                    "source": "Other", "setup": "In Germany, but not Berlin", "gender": "Other", "age": "Other",
                    "other": "a", "based": "Berlin, Germany", "base": "1", "compensation": "1", "notice": "a",
                })

    def test_custom_dropdown_prefers_yes_then_ja_and_keeps_random_selection_on_retry(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        fixture = Path(__file__).parent / "fixtures" / "linkedin_multichoice_questions.html"
        for options, expected, random_calls in ((["No", "Ja", "Yes"], "Yes", 0),
                                                (["Nein", "Ja"], "Ja", 0),
                                                (["LinkedIn", "Other"], "Other", 1)):
            with self.subTest(options=options):
                page.set_content(fixture.read_text())
                page.evaluate("options => { questions[0].options = options; window.renderForm(); }", options)
                scope = page.query_selector('[role="dialog"]')
                with patch.object(easy.random_module, "choice", side_effect=lambda choices: choices[-1]) as chooser:
                    for _ in range(2):
                        self.assertTrue(easy._answer_custom_choice(page, scope, page.query_selector("#source"),
                                                                 "A new question", {}, {}))
                self.assertEqual(page.evaluate("window.choiceState.source"), expected)
                self.assertEqual(chooser.call_count, random_calls)

    def test_exact_screening_questions_choose_yes_a_one_and_advance(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_default_timeout(30000)
        page.set_content('''<div role="dialog"><h2>Weitere Fragen</h2>
            <fieldset><legend>Have you spent at least 2 years working in a product company
                (SaaS, platform or software product) rather than primarily in an agency or client project environment?*</legend>
                <label><input id="product-no" type="radio" value="opaque-n" style="display:none">No</label>
                <label><input id="product-yes" type="radio" value="opaque-y" style="display:none">Yes</label>
            </fieldset>
            <fieldset id="relocate"><legend>The position requires working from the office two days a week.
                Do you currently live in Hamburg or Berlin, or are you willing to relocate?*</legend>
                <div role="radio" aria-checked="true" onclick="pick(this)">
                    <input id="relocate-no" name="relocate's [group]" type="radio" value="uuid-no" style="display:none">No</div>
                <div role="radio" aria-checked="false" onclick="pick(this)">
                    <input id="relocate-yes" name="relocate's [group]" type="radio" value="uuid-yes" checked style="display:none">Yes</div>
            </fieldset>
            <label for="features">How many frontend features have you owned end-to-end (from requirements and
                implementation to production and post-release improvements) within the last 12 months?*</label>
            <input id="features" type="text" maxlength="20" required>
            <label>Approximately how many pull requests (PRs) have you reviewed for other engineers
                during the last 12 months?*<input id="prs" type="number" required></label>
            <p id="resident-question">Are you a German resident?*</p>
            <div role="radiogroup" aria-labelledby="resident-question" aria-required="true">
                <button type="button" id="resident-no" role="radio" aria-checked="false" onclick="pick(this)">No</button>
                <button type="button" id="resident-yes" role="radio" aria-checked="false" onclick="pick(this)">Yes</button>
            </div>
            <label>What package managers have you used?<textarea id="packages" required></textarea></label>
            <input type="hidden" id="token" value="keep-this">
            <button id="next" type="button">Next</button>
            </div><script>
                window.picks = 0;
                function pick(option) {
                    window.picks++;
                    for (const peer of option.parentElement.querySelectorAll('[role="radio"]')) {
                        peer.setAttribute('aria-checked', String(peer === option));
                    }
                }
                document.querySelector('#next').onclick = () => {
                    window.answers = {
                        product: document.querySelector('#product-yes').checked,
                        relocate: document.querySelector('#relocate-yes').parentElement.getAttribute('aria-checked'),
                        resident: document.querySelector('#resident-yes').getAttribute('aria-checked'),
                        features: document.querySelector('#features').value,
                        prs: document.querySelector('#prs').value,
                        packages: document.querySelector('#packages').value,
                        token: document.querySelector('#token').value,
                    };
                    if (window.answers.product && window.answers.relocate === 'true' && window.answers.resident === 'true' &&
                        window.answers.features === '1' && window.answers.prs === '1' && window.answers.packages === 'a') {
                        document.querySelector('[role="dialog"]').innerHTML =
                            '<h2>Review your application</h2><button>Submit application</button>';
                    }
                };
            </script>''')
        started = time.monotonic()
        with (patch.object(page, "wait_for_timeout"),
              patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(page))):
            result = easy._fill_and_submit_form(page, {}, {"German": "B2"}, dry_run=True)
        self.assertLess(time.monotonic() - started, 10, "Screening questions must not trigger long locator retries")
        self.assertEqual(result, "SKIPPED")  # Normal dry-run stop at final submit, after advancing.
        self.assertIn("Review your application", page.inner_text('[role="dialog"]'))
        self.assertEqual(page.evaluate("window.answers"), {
            "product": True, "relocate": "true", "resident": "true", "features": "1", "prs": "1",
            "packages": "a", "token": "keep-this",
        })
        self.assertEqual(page.evaluate("window.picks"), 2)

    def test_required_field_repair_supports_unnamed_radios_and_numeric_text(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<form>
            <fieldset><legend>Unknown question*</legend>
                <label><input type="radio" id="no" required>No</label>
                <label><input type="radio" id="yes" required>Yes</label>
            </fieldset>
            <label>Count<input id="count" type="text" inputmode="numeric" required></label>
            <label>Unknown text<textarea id="text" required></textarea></label>
            <input type="hidden" id="token" required value="keep">
            <p>This field is required</p></form>''')
        self.assertEqual(easy._repair_required_fields(page.query_selector("form"), {}, {}), 3)
        self.assertTrue(page.locator("#yes").is_checked())
        self.assertFalse(page.locator("#no").is_checked())
        self.assertEqual(page.locator("#count").input_value(), "1")
        self.assertEqual(page.locator("#text").input_value(), "a")
        self.assertEqual(page.locator("#token").input_value(), "keep")

    def test_group_isolation_configured_no_and_saved_resume_selection(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<form>
            <div><p>Question one</p>
                <label><input id="first-no" type="radio" name="company's [question]" value="uuid-no">No</label>
                <label><input id="first-yes" type="radio" name="company's [question]" value="uuid-yes">Yes</label>
            </div>
            <div><p>Question two</p>
                <label><input id="second-no" type="radio" value="uuid-no">No</label>
                <label><input id="second-yes" type="radio" value="uuid-yes">Yes</label>
            </div>
            <fieldset><legend>Resume</legend>
                <label><input id="old" type="radio" name="resume">Old.pdf</label>
                <label><input id="fresh" type="radio" name="resume" checked>AndrewBodzsarCV.pdf</label>
            </fieldset></form>''')
        scope = page.query_selector("form")
        first_key, first_options = easy._radio_question_options(page.query_selector("#first-yes"))
        second_key, second_options = easy._radio_question_options(page.query_selector("#second-yes"))
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(len(first_options), 2)
        self.assertEqual(len(second_options), 2)
        self.assertTrue(easy._answer_radio_question(scope, first_options, "Question one", {}, {"Question one": "No"}))
        self.assertTrue(easy._answer_radio_question(scope, second_options, "Question two", {}, {}))
        self.assertTrue(page.locator("#first-no").is_checked())
        self.assertTrue(page.locator("#second-yes").is_checked())
        _, resume_options = easy._radio_question_options(page.query_selector("#old"))
        easy._answer_radio_question(scope, resume_options, "Resume", {}, {})
        self.assertTrue(page.locator("#fresh").is_checked())
        self.assertFalse(page.locator("#old").is_checked())

    def test_unresponsive_custom_radio_fails_quickly_without_claiming_a_selection(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_default_timeout(30000)
        page.set_content('<div role="dialog"><div id="yes" role="radio" aria-checked="false">Yes</div></div>')
        scope = page.query_selector('[role="dialog"]')
        started = time.monotonic()
        self.assertFalse(easy._click_choice_input(scope, page.query_selector("#yes"), "radio"))
        self.assertLess(time.monotonic() - started, 3)

    def test_simple_unknown_text_fallback_keeps_resume_upload_and_skips_hidden_fields(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            pdf.write_bytes(b"%PDF-1.7\nlocal upload fixture")
            page.set_content('''<div role="dialog">
                <label>What package managers have you used?<input id="packages" required></label>
                <label>What Physical AI / Robotics projects have you done?<textarea id="projects" required></textarea></label>
                <label>Your Github handle and a project that you are proud of?<input id="github" required></label>
                <input id="hidden" type="hidden" value="preserve-this">
                <label>Resume<input type="file" name="resume" accept=".pdf"></label>
                <button>Submit application</button></div>''')
            modal = page.query_selector('[role="dialog"]')
            with (patch.object(page, "wait_for_timeout"),
                  patch.object(easy, "_wait_for_easy_apply_form_ready", return_value=modal),
                  patch.object(easy, "_find_easy_apply_next_button", return_value=None),
                  patch.object(easy, "_find_easy_apply_action_button", return_value=modal.query_selector("button")),
                  patch.object(easy, "_click_action_button") as click):
                result = easy._fill_and_submit_form(page, {}, {}, str(pdf), dry_run=True, require_fresh_resume=True)
            self.assertEqual(result, "READY")
            for selector in ("#packages", "#projects", "#github"):
                self.assertEqual(page.locator(selector).input_value(), "a")
            self.assertEqual(page.locator('#hidden').input_value(), "preserve-this")
            self.assertEqual(page.locator('input[type=file]').evaluate("el => el.files[0].name"), pdf.name)
            click.assert_not_called()

    def test_title_extraction_from_sdui_detail_and_self_anchored_result(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('''<div id="job-card" data-testid="lazy-column">
            <p><a href="https://www.linkedin.com/company/infrared-city/life/">infrared.city</a></p>
            <p><a id="job-title" href="https://www.linkedin.com/jobs/view/4476491628/?trackingId=fixture">
                Climate Simulation &amp; AI Scientist, Built Environment</a></p>
            <h2>About the job</h2><p>We work on climate simulation pipelines.</p>
        </div>''')
        expected = "Climate Simulation & AI Scientist, Built Environment"
        self.assertEqual(easy._extract_job_title_from_card(page.query_selector("#job-card")), expected)
        self.assertEqual(easy._extract_job_title_from_card(page.query_selector("#job-title")), expected)

    def test_live_apply_locator_resolves_a_replaced_anchor_or_button(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        job_id = "4467577090"
        for tag, hidden_copy in (("a", False), ("a", True), ("button", False), ("button", True)):
            with self.subTest(tag=tag, hidden_copy=hidden_copy):
                href = f'href="/jobs/view/{job_id}/apply/"' if tag == "a" else ""
                hidden = (f'<{tag} {href} style="display:none" aria-label="Easy Apply to this job">Easy Apply</{tag}>'
                          if hidden_copy else "")
                page.set_content(f'<main data-sdui-screen="jobs.SemanticJobDetails">{hidden}'
                                 f'<{tag} id="apply" {href} aria-label="Easy Apply to this job">Easy Apply</{tag}>'
                                 f'<div id="JobDetails_AboutTheJob_{job_id}">Job description</div></main>')
                old_handle = page.query_selector("#apply")
                live_locator = easy._find_easy_apply_control(page, job_url=existing_non_easy.JOB_URL)
                self.assertIsNotNone(live_locator)
                page.evaluate("""() => {
                    window.clicks = 0;
                    const old = document.querySelector('#apply');
                    const fresh = old.cloneNode(true);
                    fresh.onclick = event => { event.preventDefault(); window.clicks++; };
                    old.replaceWith(fresh);
                }""")
                self.assertFalse(old_handle.evaluate("el => el.isConnected"))
                live_locator.click(timeout=2000)
                self.assertEqual(page.evaluate("window.clicks"), 1)
                easy._open_easy_apply_for_job(page, existing_non_easy.JOB_URL)
                self.assertEqual(page.evaluate("window.clicks"), 2)

    def test_virtualized_header_is_restored_without_navigating_or_rebuilding_resume(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('<div id="pane" data-sdui-screen="jobs.SemanticJobDetails" '
                         'style="height:200px;overflow-y:auto"><div id="header"></div>'
                         '<div style="height:1000px"></div><div id="JobDetails_AboutTheJob_4467577090">'
                         'Job description</div></div>')
        page.evaluate("""() => {
            window.clicks = 0;
            const pane = document.querySelector('#pane');
            pane.scrollTop = pane.scrollHeight;
            pane.onscroll = () => {
                if (pane.scrollTop !== 0 || document.querySelector('#apply')) return;
                const button = document.createElement('button');
                button.id = 'apply';
                button.textContent = 'Easy Apply';
                button.setAttribute('aria-label', 'Easy Apply to this job');
                button.onclick = () => window.clicks++;
                document.querySelector('#header').append(button);
            };
        }""")
        self.assertEqual(page.locator("#apply").count(), 0)
        before = page.url
        easy._open_easy_apply_for_job(page, existing_non_easy.JOB_URL)
        self.assertEqual(page.evaluate("window.clicks"), 1)
        self.assertEqual(page.url, before)

    def test_changed_job_or_wrong_job_link_is_never_clicked(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        page.set_content('<main data-sdui-screen="jobs.SemanticJobDetails">'
                         '<a aria-label="Easy Apply to this job" href="/jobs/view/999/apply/">Easy Apply</a>'
                         '<div id="JobDetails_AboutTheJob_999">Another job</div></main>')
        self.assertIsNone(easy._find_easy_apply_control(page, job_url=existing_non_easy.JOB_URL))
        with self.assertRaisesRegex(RuntimeError, "Selected job changed"):
            easy._open_easy_apply_for_job(page, existing_non_easy.JOB_URL)

    def test_submit_requires_a_new_upload_even_with_an_identically_named_saved_cv(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        page = harness.page
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            pdf = Path(directory) / "AndrewBodzsarCV.pdf"
            pdf.write_bytes(b"%PDF-1.7\nlocal upload fixture")
            stack.enter_context(patch.object(page, "wait_for_timeout"))
            stack.enter_context(patch.object(easy, "_find_easy_apply_next_button", return_value=None))
            for has_upload, expected in ((False, "FAILED"), (True, "READY")):
                with self.subTest(has_upload=has_upload):
                    page.set_content('<div role="dialog"><h2>Apply to Employer</h2>'
                                     '<p>Saved resume: AndrewBodzsarCV.pdf</p>'
                                     + ('<label>Resume<input type="file" name="resume" accept=".pdf"></label>' if has_upload else '')
                                     + '<button>Submit application</button></div>')
                    modal = page.query_selector('[role="dialog"]')
                    submit = modal.query_selector("button")
                    with (patch.object(easy, "_wait_for_easy_apply_form_ready", return_value=modal),
                          patch.object(easy, "_find_easy_apply_action_button", return_value=submit),
                          patch.object(easy, "_click_action_button") as click):
                        result = easy._fill_and_submit_form(page, {}, {}, str(pdf), dry_run=True,
                                                            require_fresh_resume=True)
                    self.assertEqual(result, expected)
                    click.assert_not_called()
                    if has_upload:
                        self.assertEqual(page.locator('input[type="file"]').evaluate("el => el.files[0].name"), pdf.name)


if __name__ == "__main__":
    unittest.main()
