"""Field-scoped numeric/text retries for LinkedIn's invalid-input feedback."""

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import unified_apply as easy

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_invalid_type_inputs.html"


class CandidateInput:
    def __init__(self, numeric=False):
        self.numeric = numeric

    def evaluate(self, script, value):
        return not self.numeric or bool(re.fullmatch(r"-?\d+(?:\.\d+)?", value))


class TypeFallbackTests(unittest.TestCase):
    def test_defaults_are_number_then_text(self):
        self.assertEqual(easy._type_fallback_candidates(CandidateInput(), "Unknown question", {}, {}),
                         [("1", "number"), ("a", "text")])

    def test_applicable_configured_answer_has_priority_without_duplicate_values(self):
        self.assertEqual(easy._type_fallback_candidates(CandidateInput(), "Exact count", {}, {"Exact count": "7"}),
                         [("7", "configured"), ("1", "number"), ("a", "text")])
        self.assertEqual(easy._type_fallback_candidates(CandidateInput(), "Exact count", {}, {"Exact count": "1"}),
                         [("1", "configured"), ("a", "text")])

    def test_incompatible_native_numeric_value_is_not_tried(self):
        self.assertEqual(easy._type_fallback_candidates(CandidateInput(numeric=True), "Unknown question", {}, {}),
                         [("1", "number")])

    def test_attempt_sets_are_per_field(self):
        first, second = easy._TypeFallbackState(), easy._TypeFallbackState()
        first.tried.add("a")
        self.assertEqual(second.tried, set())


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1 for browser fixtures")
class InvalidInputBrowserTests(unittest.TestCase):
    def setUp(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.page = harness.page
        self.page.set_content(FIXTURE.read_text())

    def fill(self, answers=None, dry_run=True):
        with (patch.object(self.page, "wait_for_timeout"),
              patch.object(easy, "_close_easy_apply_modal"),
              patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(self.page))):
            return easy._fill_and_submit_form(self.page, {}, answers or {}, dry_run=dry_run)

    def test_reported_dutch_fields_rotate_a_to_one_and_reach_review(self):
        self.assertEqual(self.fill(), "SKIPPED")
        self.assertEqual(self.page.evaluate("window.reviewValues"), [{"azure": "1", "ai": "1", "ordinary": "a"}])
        self.assertEqual(self.page.evaluate("window.submitClicks"), 0)
        numeric_events = self.page.evaluate("window.fillEvents.filter(event => event.field !== 'ordinary')")
        self.assertEqual(numeric_events, [{"field": "azure", "value": "1"}, {"field": "ai", "value": "1"}])
        self.assertEqual(self.page.evaluate("window.radioChanges"), 0)

    def test_invalid_input_appearing_after_review_is_repaired_and_live_button_retried(self):
        self.page.evaluate("""() => {
            for (const error of document.querySelectorAll('[data-error]')) error.hidden = true;
            for (const input of document.querySelectorAll('[data-field]')) input.value = '';
            window.replaceOnReview = true;
        }""")
        self.assertEqual(self.fill(), "SKIPPED")
        attempts = self.page.evaluate("window.reviewValues")
        self.assertEqual(attempts, [
            {"azure": "a", "ai": "a", "ordinary": "a"},
            {"azure": "1", "ai": "1", "ordinary": "a"},
        ])
        self.assertEqual(self.page.evaluate("window.submitClicks"), 0)

    def test_corrected_values_survive_whole_form_rerenders_and_repeated_step(self):
        self.page.evaluate("window.replaceOnInput = true; window.replaceOnReview = true; window.repeatAcceptedStepOnce = true")
        self.assertEqual(self.fill(), "SKIPPED")
        self.assertEqual(self.page.evaluate("window.reviewValues"), [
            {"azure": "1", "ai": "1", "ordinary": "a"},
            {"azure": "1", "ai": "1", "ordinary": "a"},
        ])
        numeric_events = self.page.evaluate("window.fillEvents.filter(event => event.field !== 'ordinary')")
        self.assertEqual(numeric_events, [{"field": "azure", "value": "1"}, {"field": "ai", "value": "1"}])

    def test_rejected_number_can_rotate_to_text_without_restoring_bad_configured_number(self):
        self.page.evaluate("""() => {
            window.expected = {azure: 'text', ai: 'text'};
            for (const input of document.querySelectorAll('[data-field="azure"], [data-field="ai"]')) input.value = '1';
            for (const error of document.querySelectorAll('[data-error]')) error.textContent = 'Invalid type';
            window.repeatAcceptedStepOnce = true;
        }""")
        answers = {"Hoeveel jaar werkervaring hebt u met Microsoft Azure?": "1",
                   "Hoeveel jaar werkervaring hebt u met Kunstmatige intelligentie?": "1"}
        self.assertEqual(self.fill(answers), "SKIPPED")
        self.assertEqual(self.page.evaluate("window.reviewValues"), [
            {"azure": "a", "ai": "a", "ordinary": "a"},
            {"azure": "a", "ai": "a", "ordinary": "a"},
        ])

    def test_exhausted_fallbacks_stop_instead_of_alternating_forever(self):
        self.page.evaluate("window.alwaysReject = true; window.replaceOnReview = true")
        with self.assertLogs(easy.log, level="INFO") as logs:
            self.assertEqual(self.fill(), "FAILED")
        self.assertTrue(any("fallback exhausted" in message for message in logs.output))
        self.assertLessEqual(self.page.evaluate("window.reviewClicks"), 2)
        self.assertEqual(self.page.evaluate("window.submitClicks"), 0)
        numeric_events = self.page.evaluate("window.fillEvents.filter(event => event.field !== 'ordinary')")
        self.assertEqual(numeric_events, [{"field": "azure", "value": "1"}, {"field": "ai", "value": "1"}])

    def test_only_fields_with_visible_linked_errors_are_repaired(self):
        self.page.set_content('''<div role="dialog"><h2>Questions</h2>
            <label>Rejected text<input id="bad" aria-describedby="bad-info" value="a"></label>
            <div id="bad-info"><p>INVALID INPUT</p></div>
            <label>Valid text<input id="good" value="keep-this-answer"></label>
            <label>Hidden feedback<input id="hidden-error" aria-describedby="hidden-info" value="a"></label>
            <div id="hidden-info"><p hidden>Invalid type</p></div>
            <label>Required only<input id="required" aria-describedby="required-info"></label>
            <div id="required-info"><p>This field is required</p></div>
            <input id="token" type="hidden" value="keep-token" aria-describedby="bad-info">
            <input id="disabled" disabled value="a" aria-describedby="bad-info">
            <input id="readonly" readonly value="a" aria-describedby="bad-info">
            <label><input id="radio" type="radio" checked aria-describedby="bad-info">Ja</label>
            <input id="upload" type="file" aria-describedby="bad-info"></div>''')
        scope = self.page.query_selector('[role="dialog"]')
        self.assertEqual([field.get_attribute("id") for field in easy._invalid_type_inputs(scope)], ["bad"])
        self.assertEqual(easy._rotate_invalid_type_fields(self.page, scope, {}, {}, {}), 1)
        self.assertEqual(self.page.locator('#bad').input_value(), "1")
        self.assertEqual(self.page.locator('#good').input_value(), "keep-this-answer")
        self.assertEqual(self.page.locator('#hidden-error').input_value(), "a")
        self.assertEqual(self.page.locator('#token').input_value(), "keep-token")
        self.assertTrue(self.page.locator('#radio').is_checked())

    def test_required_repair_preserves_a_successful_text_type_fallback(self):
        self.page.set_content('''<div role="dialog"><label>Special answer*
            <input required id="answer" aria-describedby="answer-info" value="1"></label>
            <div id="answer-info">Invalid type</div><p>This field is required</p></div>''')
        scope = self.page.query_selector('[role="dialog"]')
        states = {}
        answers = {"Special answer": "1"}
        self.assertEqual(easy._rotate_invalid_type_fields(self.page, scope, states, {}, answers), 1)
        self.page.locator('#answer-info').evaluate("el => el.textContent = ''")
        self.assertEqual(easy._repair_required_fields(scope, {}, answers, page=self.page, type_fallbacks=states), 0)
        self.assertEqual(self.page.locator('#answer').input_value(), "a")

    def test_native_numeric_constraints_skip_incompatible_text(self):
        self.page.set_content('''<div role="dialog"><label>Count
            <input id="count" type="number" min="0" max="5" value="10"></label></div>''')
        scope = self.page.query_selector('[role="dialog"]')
        self.assertEqual(easy._type_fallback_candidates(self.page.query_selector('#count'), "Count", {}, {}), [("1", "number")])
        self.assertEqual(easy._rotate_invalid_type_fields(self.page, scope, {}, {}, {}), 1)
        self.assertEqual(self.page.locator('#count').input_value(), "1")

    def test_final_submit_type_validation_is_repaired_on_the_same_step(self):
        self.page.set_content('''<div role="dialog"><label>Unknown count*
            <input required id="count" aria-describedby="count-info"></label>
            <div id="count-info" hidden>Invalid input</div>
            <button id="submit">Submit application</button></div>''')
        self.page.evaluate("""() => {
            window.finalAttempts = [];
            document.querySelector('#submit').onclick = () => {
                const value = document.querySelector('#count').value;
                window.finalAttempts.push(value);
                if (value !== '1') { document.querySelector('#count-info').hidden = false; return; }
                document.querySelector('[role="dialog"]').remove();
                document.body.insertAdjacentHTML('beforeend', '<p>Application submitted successfully</p>');
            };
        }""")
        self.assertEqual(self.fill(dry_run=False), "APPLIED")
        self.assertEqual(self.page.evaluate("window.finalAttempts"), ["a", "1"])


if __name__ == "__main__":
    unittest.main()
