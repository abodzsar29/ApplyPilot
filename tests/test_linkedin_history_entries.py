"""Scoped deletion of expanded Easy Apply education/work entries."""

import os
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import unified_apply as easy

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_history_editors.html"


class HistoryEditorScopeTests(unittest.TestCase):
    def test_whole_page_is_never_used_as_a_deletion_scope(self):
        page = Mock()
        self.assertEqual(easy._delete_open_history_entries(page, page), 0)
        page.evaluate_handle.assert_not_called()
        page.query_selector_all.assert_not_called()


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1 for browser fixtures")
class HistoryEditorBrowserTests(unittest.TestCase):
    def setUp(self):
        harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.page = harness.page
        self.page.set_content(FIXTURE.read_text())

    def fill(self):
        with (patch.object(self.page, "wait_for_timeout"),
              patch.object(easy, "_close_easy_apply_modal"),
              patch.object(easy, "_wait_for_easy_apply_form_ready", side_effect=lambda *a, **kw: easy._get_active_modal(self.page))):
            return easy._fill_and_submit_form(self.page, {}, {}, dry_run=True)

    def assert_no_history_filling_or_unrelated_deletion(self):
        self.assertEqual(self.page.evaluate("window.historyInputEvents"), [])
        self.assertEqual(self.page.evaluate("window.historySaveClicks"), 0)
        self.assertEqual(self.page.evaluate("window.closedEntryDeletes"), 0)
        self.assertEqual(self.page.evaluate("window.profileDeletes"), 0)
        self.assertEqual(self.page.locator('#profile-editor input').input_value(), "Leave my profile alone")

    def test_reported_editor_shape_is_detected_without_generated_css_classes(self):
        scope = self.page.query_selector('#application')
        editors = easy._open_history_editors(scope)
        self.assertEqual([kind for kind, _, _ in editors], ["experience", "education"])
        self.assertEqual([editor.get_attribute("id") for _, editor, _ in editors], ["work-entry", "education-entry"])
        self.assertEqual([button.inner_text().strip() for _, _, button in editors], ["Delete experience", "Delete education"])

    def test_open_entries_deleted_before_filling_then_normal_questions_continue(self):
        original = self.page.locator('#application .history-editor:not([hidden])').evaluate_all(
            "entries => entries.map(entry => [...entry.querySelectorAll('input, select, textarea')]"
            ".map(field => ({id: field.id, value: field.value, checked: !!field.checked})))",
        )
        result = self.fill()
        self.assertEqual(result, "SKIPPED")  # Dry run stops at review, without submitting.
        self.assertEqual(self.page.evaluate("window.deletedHistoryEntries"), ["work-entry", "education-entry"])
        self.assertEqual(self.page.evaluate("window.historySnapshots"), original)
        self.assertEqual(self.page.evaluate("window.nextClicks"), 1)
        self.assertEqual(self.page.evaluate("window.ordinaryAnswer"), "a")
        self.assertEqual(self.page.evaluate("window.submitClicks"), 0)
        self.assertIn("Review your application", self.page.locator('#application').inner_text())
        self.assert_no_history_filling_or_unrelated_deletion()

    def test_confirmation_and_whole_form_rerenders_are_handled(self):
        self.page.evaluate("window.deletionsUseConfirmation = true; window.replaceApplicationOnDelete = true")
        self.assertEqual(self.fill(), "SKIPPED")
        self.assertEqual(self.page.evaluate("window.deletedHistoryEntries"), ["work-entry", "education-entry"])
        self.assertEqual(self.page.locator('[role="alertdialog"]').count(), 0)
        self.assertEqual(self.page.evaluate("window.nextClicks"), 1)
        self.assert_no_history_filling_or_unrelated_deletion()

    def test_required_field_repair_deletes_editors_and_fills_only_unrelated_question(self):
        repaired = easy._repair_required_fields(self.page.query_selector('#application'), {}, {}, page=self.page)
        self.assertEqual(repaired, 3)  # Two deletions plus the ordinary required question.
        self.assertEqual(self.page.locator('#ordinary-question').input_value(), "a")
        self.assertEqual(self.page.evaluate("window.deletedHistoryEntries"), ["work-entry", "education-entry"])
        self.assert_no_history_filling_or_unrelated_deletion()

    def test_editor_that_opens_after_next_is_deleted_during_validation_retry(self):
        self.page.evaluate("""() => {
            const template = document.querySelector('#work-entry').outerHTML;
            document.querySelector('#work-entry').remove();
            document.querySelector('#education-entry').remove();
            document.querySelector('#next').disabled = false;
            window.replaceApplicationOnDelete = true;
            document.addEventListener('click', event => {
                if (event.target.closest('#next') && window.nextClicks === 0) {
                    event.stopImmediatePropagation();
                    window.nextClicks++;
                    document.querySelector('#next').insertAdjacentHTML('beforebegin', template);
                    document.querySelector('#next').disabled = true;
                }
            }, true);
        }""")
        connected_clicks = []
        original_click = easy._click_action_button

        def click(button):
            connected_clicks.append(button.evaluate("el => el.isConnected"))
            return original_click(button)

        with patch.object(easy, "_click_action_button", side_effect=click):
            self.assertEqual(self.fill(), "SKIPPED")
        self.assertEqual(connected_clicks, [True, True])
        self.assertEqual(self.page.evaluate("window.deletedHistoryEntries"), ["work-entry"])
        self.assertEqual(self.page.evaluate("window.nextClicks"), 2)
        self.assertEqual(self.page.evaluate("window.submitClicks"), 0)
        self.assert_no_history_filling_or_unrelated_deletion()

    def test_unrelated_existing_confirmation_is_never_clicked(self):
        self.page.evaluate("""() => {
            window.ignoreDelete = true;
            window.unrelatedConfirmationClicks = 0;
            const dialog = document.createElement('div');
            dialog.setAttribute('role', 'alertdialog');
            dialog.innerHTML = '<h2>Delete experience?</h2><button>Delete</button>';
            dialog.querySelector('button').onclick = () => window.unrelatedConfirmationClicks++;
            document.body.append(dialog);
        }""")
        self.assertEqual(self.fill(), "FAILED")
        self.assertEqual(self.page.evaluate("window.unrelatedConfirmationClicks"), 0)
        self.assert_no_history_filling_or_unrelated_deletion()

    def test_missing_or_disabled_delete_never_borrows_closed_entry_button(self):
        for mode in ("missing", "disabled"):
            with self.subTest(mode=mode):
                self.page.set_content(FIXTURE.read_text())
                self.page.evaluate("""mode => {
                    const button = document.querySelector('#work-entry [data-history-delete]');
                    if (mode === 'missing') button.remove(); else button.disabled = true;
                }""", mode)
                self.assertEqual(self.fill(), "FAILED")
                self.assertEqual(self.page.evaluate("window.deletedHistoryEntries"), [])
                self.assertEqual(self.page.evaluate("window.nextClicks"), 0)
                self.assertEqual(self.page.locator('#ordinary-question').input_value(), "")
                self.assert_no_history_filling_or_unrelated_deletion()

    def test_noop_delete_fails_quickly_without_filling_saving_or_advancing(self):
        self.page.evaluate("window.ignoreDelete = true")
        started = time.monotonic()
        self.assertEqual(self.fill(), "FAILED")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(self.page.evaluate("window.historySnapshots")), 1)
        self.assertEqual(self.page.evaluate("window.deletedHistoryEntries"), [])
        self.assertEqual(self.page.evaluate("window.nextClicks"), 0)
        self.assert_no_history_filling_or_unrelated_deletion()

    def test_closed_entries_and_hidden_templates_are_not_deleted(self):
        self.page.evaluate("document.querySelector('#work-entry').remove(); document.querySelector('#education-entry').remove()")
        self.assertEqual(easy._delete_open_history_entries(self.page, self.page.query_selector('#application')), 0)
        self.assertEqual(self.page.locator('#closed-entry').count(), 1)
        self.assertEqual(self.page.locator('#hidden-editor').count(), 1)
        self.assert_no_history_filling_or_unrelated_deletion()

    def test_add_education_and_german_editor_labels_are_supported(self):
        for editor_label, delete_label, save_label in (
            ("Add education", "Delete education", "Save"),
            ("Ausbildung bearbeiten", "Ausbildung löschen", "Speichern"),
        ):
            with self.subTest(editor_label=editor_label):
                self.page.set_content(FIXTURE.read_text())
                self.page.evaluate("""labels => {
                    document.querySelector('#work-entry').remove();
                    const entry = document.querySelector('#education-entry');
                    entry.querySelector('p').textContent = labels[0];
                    entry.querySelector('[data-history-delete]').textContent = labels[1];
                    entry.querySelector('button').textContent = labels[2];
                }""", [editor_label, delete_label, save_label])
                self.assertEqual(easy._delete_open_history_entries(self.page, self.page.query_selector('#application')), 1)
                self.assertEqual(self.page.evaluate("window.deletedHistoryEntries"), ["education-entry"])
                self.assert_no_history_filling_or_unrelated_deletion()


if __name__ == "__main__":
    unittest.main()
