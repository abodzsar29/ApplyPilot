"""Runner order and handoff tests with mocked browsers and preparation."""

import os
import tempfile
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

    def run_easy(self, *, descriptions=None, preparation_error=None, result="APPLIED",
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
        self.stack.enter_context(patch.object(easy, "_find_linkedin_job_cards", return_value=cards))
        self.stack.enter_context(patch.object(easy, "_extract_job_url_from_card", side_effect=lambda card: card.job_url))
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
        self.stack.enter_context(patch.object(easy, "_click_linkedin_job_card", side_effect=select_card))
        self.stack.enter_context(patch.object(easy, "_current_linkedin_job_id", side_effect=lambda page: active["job_id"]))
        self.control_finder = self.stack.enter_context(patch.object(
            easy, "_find_easy_apply_control", side_effect=lambda *args, **kwargs: active["control"],
        ))
        details = [{"job_id": str(100 + index), "title": "Engineer", "company": "Employer", "description": description}
                   for index, description in enumerate(descriptions)]
        self.stack.enter_context(patch("applypilot.linkedin.scrape.read_current_job", side_effect=details))
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
                    "only_english": True, "intelligent_apply": True}
        summary = easy.search_and_apply(settings, max_applications=len(descriptions), dry_run=result == "READY")
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

    def test_non_easy_preparation_failure_does_not_open_external_form_or_record_history(self):
        harness = self.non_easy_harness()
        self.session.prepare.side_effect = ia.PreparationError("llm_response", "invalid")
        summary, opener, runner = harness.run_session(settings=harness.settings | {"intelligent_apply": True})
        self.assertEqual(summary["failed"], 1)
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
