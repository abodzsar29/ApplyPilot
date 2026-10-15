import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from applypilot.linkedin import non_easy as ne


FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_job_sdui.html"
JOB_URL = "https://www.linkedin.com/jobs/view/4467577090/"
EXTERNAL_URL = "https://employer.test/apply"
MAIL_URL = "https://mail.test/inbox"
ENGLISH = "We are looking for a candidate with experience and communication skills for our team."
GERMAN = "Wir suchen eine erfahrene Person mit Deutschkenntnissen und Erfahrung fuer unser Team."


class NonEasyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(ne.config, "APP_DIR", self.root))
        self.stack.enter_context(patch.object(ne.config, "LOG_DIR", self.root))
        for name in ("fallback.pdf", "english.pdf", "german.pdf", "photo.png"):
            (self.root / name).write_bytes(b"test file")
        (self.root / "english.txt").write_text("English CV contents")
        (self.root / "german.txt").write_text("German CV contents")
        self.settings = {
            "job_title": "Engineer", "location": "Berlin", "max_applications": 2,
            "profile": {"email": "candidate@example.test"}, "answers": {},
            "resume_path": str(self.root / "fallback.pdf"),
            "resume_paths": {"english": str(self.root / "english.pdf"),
                             "german": str(self.root / "german.pdf")},
            "photo_path": str(self.root / "photo.png"),
            "mail_inbox_url": MAIL_URL,
        }
        self.job = {"title": "Engineer", "company": "Employer", "linkedin_url": JOB_URL,
                    "application_url": EXTERNAL_URL}

    def test_history_round_trip_and_distinct_ids(self):
        ne._record_applied_job(self.job)
        keys = ne._load_applied_job_keys()
        self.assertTrue(ne._was_applied(self.job | {"linkedin_url": JOB_URL + "?tracking=1"}, keys))
        self.assertFalse(ne._was_applied(self.job | {"linkedin_url": JOB_URL.replace("7090", "7091")}, keys))
        row = json.loads(ne._history_path().read_text().strip())
        self.assertEqual(row["application_url"], EXTERNAL_URL)
        self.assertIsInstance(row["recorded_at"], int)

    def test_legacy_history_invalid_lines_and_truncated_tail(self):
        ne._history_path().write_text('broken\n[]\n{"title":12}\n'
                                     '{"title":"  ENGINEER ","company":"Employer"}\n{"partial":')
        with self.assertLogs(ne.log, level="WARNING"):
            self.assertTrue(ne._was_applied(self.job, ne._load_applied_job_keys()))
        ne._record_applied_job(self.job)
        with self.assertLogs(ne.log, level="WARNING"):
            self.assertIn("linkedin:4467577090", ne._load_applied_job_keys())

    def test_old_history_with_urls_uses_ids(self):
        ne._history_path().write_text(json.dumps(self.job))
        self.assertEqual(ne._load_applied_job_keys(), {"linkedin:4467577090"})

    def test_unreadable_history_stops_instead_of_disabling_duplicates(self):
        with patch.object(Path, "open", side_effect=PermissionError("denied")), self.assertLogs(ne.log):
            with self.assertRaises(PermissionError):
                ne._load_applied_job_keys()

    def test_unknown_titles_do_not_collide(self):
        self.assertFalse(ne._was_applied({"title": "Unknown title", "company": "Unknown company"}, {""}))

    def test_per_job_language_selection_does_not_mutate_config(self):
        for description, language in ((ENGLISH, "english"), (GERMAN, "german")):
            settings, detected = ne._settings_for_job(self.settings, description)
            self.assertEqual(detected, language)
            self.assertEqual(settings["resume_path"], self.settings["resume_paths"][language])
        self.assertEqual(self.settings["resume_path"], str(self.root / "fallback.pdf"))
        settings, _ = ne._settings_for_job(self.settings, "")
        self.assertEqual(settings["resume_path"], self.settings["resume_path"])
        settings, _ = ne._settings_for_job(self.settings | {"auto_select_resume_by_job_language": False}, ENGLISH)
        self.assertEqual(settings["resume_path"], self.settings["resume_path"])

    def test_only_english_rejects_german_and_unknown(self):
        for description in (GERMAN, ""):
            settings, _ = ne._settings_for_job(self.settings | {"only_english": True}, description)
            self.assertIsNone(settings)
        settings, _ = ne._settings_for_job(self.settings | {"only_english": True}, ENGLISH)
        self.assertIsNotNone(settings)

    def test_validate_uploads_and_mailbox(self):
        ne.validate_non_easy_config(self.settings)
        for overrides in ({"photo_path": "missing.png"}, {"resume_paths": {"german": "missing.pdf"}},
                          {"resume_path": str(self.root)}, {"mail_inbox_url": "file:///inbox"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                ne.validate_non_easy_config(self.settings | overrides)

    def test_mailbox_defaults_and_override(self):
        for domain in ("proton.me", "protonmail.com", "pm.me"):
            self.assertEqual(ne._mailbox_context({"profile": {"email": "ME@" + domain}})[0],
                             ne.PROTONMAIL_INBOX_URL)
        self.assertIsNone(ne._mailbox_context({"profile": {"email": "me@example.test"}})[0])
        self.assertEqual(ne._mailbox_context(self.settings)[0], MAIL_URL)

    def test_mailbox_reuse_failure_and_tab_cleanup(self):
        mailbox = Mock(url=MAIL_URL)
        mailbox.is_closed.return_value = False
        context = Mock(pages=[mailbox])
        self.assertIs(ne._ensure_mailbox_tab(context, self.settings), mailbox)
        context.new_page.assert_not_called()
        application = Mock()
        context.pages.append(application)
        ne._close_application_tabs(context, (mailbox,))
        application.close.assert_called_once()
        mailbox.close.assert_not_called()
        context.pages = []
        context.new_page.return_value.goto.side_effect = RuntimeError("unavailable")
        with self.assertLogs(ne.log, level="WARNING"):
            self.assertIsNone(ne._ensure_mailbox_tab(context, self.settings))
        context.new_page.return_value.close.assert_called_once()

    def test_prompt_has_selected_cv_photo_and_conditional_mailbox(self):
        settings, _ = ne._settings_for_job(self.settings, GERMAN)
        prompt = ne._build_prompt(self.job, settings, dry_run=True)
        self.assertIn("German CV contents", prompt)
        self.assertNotIn("English CV contents", prompt)
        self.assertIn(str(self.root / "photo.png"), prompt)
        self.assertIn("RESULT:READY", prompt)
        self.assertIn("Check at most three times", prompt)
        self.assertIn("Exclude mailbox tabs", prompt)
        self.assertIn(MAIL_URL, prompt)
        prompt = ne._build_prompt(self.job, settings | {"photo_path": "", "mail_inbox_url": ""})
        self.assertIn("No mailbox is configured", prompt)
        self.assertIn("missing_required_photo", prompt)

    def test_result_requires_final_result_line(self):
        for output, expected in (
            ("I will output RESULT:APPLIED when finished", "failed:no_result_line"),
            ("RESULT:APPLIED\nRESULT:FAILED:upload_rejected", "failed:upload_rejected"),
            ("RESULT:READY", "ready"), ("RESULT:APPLIED (dry run)", "ready"),
            ("**RESULT:APPLIED**", "applied"),
            ("RESULT:FAILED:email_2fa_unavailable", "failed:email_2fa_unavailable"),
            ("insufficient credits", "failed:provider_credit_low"),
        ):
            with self.subTest(output=output):
                self.assertEqual(ne._extract_result(output), expected)

    def test_search_uses_semantic_results_route(self):
        url = ne._search_url({"job_title": "Junior Software", "location": "Berlin, Germany"})
        self.assertEqual(
            url,
            "https://www.linkedin.com/jobs/search-results/"
            "?keywords=Junior%20Software&location=Berlin%2C%20Germany",
        )
        self.assertNotIn("/jobs/search/?", url)

    def test_result_card_url_supports_href_and_semantic_component_key(self):
        linked_card = Mock()
        linked_card.get_attribute.side_effect = lambda name: (
            "/jobs/view/12345/?tracking=1" if name == "href" else None
        )
        self.assertEqual(
            ne._job_url_from_result_card(linked_card),
            "https://www.linkedin.com/jobs/view/12345/?tracking=1",
        )

        semantic_card = Mock()
        semantic_card.get_attribute.side_effect = lambda name: (
            "job-card-component-ref-4475936947" if name == "componentkey" else None
        )
        self.assertEqual(
            ne._job_url_from_result_card(semantic_card),
            "https://www.linkedin.com/jobs/view/4475936947/",
        )

    def test_both_provider_paths_receive_same_context(self):
        settings, _ = ne._settings_for_job(self.settings, ENGLISH)
        agent = Mock()
        agent.run_prompt.return_value = "RESULT:READY"
        self.assertEqual(ne._run_external_application("openrouter", "model", agent, self.job,
                                                     settings, 9222, True)[0], "ready")
        prompt = agent.run_prompt.call_args.args[0]
        proc = Mock(stdin=io.StringIO(), stdout=iter([json.dumps({"type": "result", "result": "RESULT:READY"})]))
        captured = []
        proc.stdin = Mock()
        proc.stdin.write.side_effect = captured.append
        with patch.object(ne, "reset_worker_dir", return_value=self.root), patch.object(ne.subprocess, "Popen", return_value=proc):
            self.assertEqual(ne._run_external_application("claude", "haiku", None, self.job,
                                                         settings, 9222, True)[0], "ready")
        self.assertEqual(captured, [prompt])

    def run_session(self, result="applied", *, settings=None, easy=False, description=None, dry_run=False, jobs=1):
        search, detail = Mock(url="about:blank"), Mock()
        search.is_closed.return_value = False
        mailbox = Mock(url=MAIL_URL)
        mailbox.is_closed.return_value = False
        # A preexisting mailbox can be the first tab and must not become search.
        context = Mock(pages=[mailbox, search])
        detail.is_closed.return_value = False
        detail.query_selector.return_value = None
        detail.content.return_value = FIXTURE.read_text()
        search.query_selector.return_value = None
        def new_page():
            context.pages.append(detail)
            return detail
        context.new_page.side_effect = new_page
        application = Mock()
        def open_application(page):
            context.pages.append(application)
            return application, EXTERNAL_URL
        cards = []
        for index in range(jobs):
            card = Mock()
            card.get_attribute.return_value = JOB_URL.replace("7090", str(7090 + index))
            card.text_content.return_value = "Engineer"
            cards.append(card)
        def run_agent(**kwargs):
            mailbox.close.assert_not_called()
            return result, 1
        playwright = Mock()
        playwright.chromium.connect_over_cdp.return_value = Mock(contexts=[context])
        with ExitStack() as stack:
            for name in ("launch_chrome", "cleanup_worker", "kill_all_chrome", "QwenMCPAgent"):
                stack.enter_context(patch.object(ne, name))
            stack.enter_context(patch.object(ne, "get_effective_model_and_provider", return_value=("model", "openrouter")))
            manager = stack.enter_context(patch.object(ne, "sync_playwright"))
            manager.return_value.__enter__.return_value = playwright
            stack.enter_context(patch.object(ne, "_iter_result_cards", return_value=cards))
            stack.enter_context(patch.object(ne, "_is_easy_apply", return_value=easy))
            opener = stack.enter_context(patch.object(ne, "_open_external_application_page", side_effect=open_application))
            runner = stack.enter_context(patch.object(ne, "_run_external_application", side_effect=run_agent))
            if description is not None:
                stack.enter_context(patch("applypilot.linkedin.scrape.parse_job", return_value={
                    "description": description, "title": "Engineer", "company": "Employer"}))
            summary = ne.run_non_easy_apply(settings or self.settings, dry_run=dry_run)
            if runner.called:
                self.assertTrue(application.close.called)
            self.assertTrue(mailbox.close.called)
            return summary, opener, runner

    def test_session_records_success_then_skips_before_external_click(self):
        summary, _, runner = self.run_session()
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(runner.call_args.kwargs["config_dict"]["resume_path"], str(self.root / "english.pdf"))
        summary, opener, runner = self.run_session()
        self.assertEqual(summary["search_stats"]["already_applied"], 1)
        opener.assert_not_called()
        runner.assert_not_called()

    def test_session_filters_before_external_click(self):
        for description in (GERMAN, ""):
            summary, opener, _ = self.run_session(settings=self.settings | {"only_english": True}, description=description)
            self.assertEqual(summary["search_stats"]["language_skipped"], 1)
            opener.assert_not_called()
        summary, opener, _ = self.run_session(easy=True)
        self.assertEqual(summary["search_stats"]["easy_skipped"], 1)
        opener.assert_not_called()

    def test_two_jobs_keep_mailbox_and_distinguish_same_title_company(self):
        summary, _, runner = self.run_session(jobs=2, description=ENGLISH)
        self.assertEqual(summary["applied"], 2)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(len(ne._load_applied_job_keys()), 2)

    def test_credit_failure_stops_remaining_jobs_and_does_not_record(self):
        with self.assertLogs(ne.log):
            summary, _, runner = self.run_session("failed:provider_credit_low", jobs=2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(runner.call_count, 1)
        self.assertFalse(ne._history_path().exists())

    def test_cli_validation_and_dry_run_summary(self):
        from typer.testing import CliRunner
        from applypilot.cli import app

        path = self.root / "config.json"
        with patch("applypilot.cli._bootstrap"), patch("applypilot.config.check_non_easy_provider_requirements"), \
                patch("applypilot.qwen_mcp.get_effective_model_and_provider", return_value=("haiku", "claude")), \
                patch.object(ne, "run_non_easy_apply", return_value={
                    "found": 1, "applied": 0, "ready": 1, "skipped": 0, "failed": 0}) as runner:
            path.write_text(json.dumps(self.settings | {"photo_path": "missing.png"}))
            result = CliRunner().invoke(app, ["linkedin-noneasy", "--config", str(path), "--dry-run"])
            self.assertEqual(result.exit_code, 1)
            self.assertIn("photo_path", result.output)
            runner.assert_not_called()
            path.write_text(json.dumps(self.settings))
            result = CliRunner().invoke(app, ["linkedin-noneasy", "--config", str(path), "--dry-run"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Ready (dry run):     1", result.output)
            self.assertTrue(runner.call_args.kwargs["dry_run"])

    def test_session_dry_run_and_failure_never_record_success(self):
        for result, dry_run in (("ready", True), ("applied", True), ("failed:missing_required_photo", False)):
            summary, _, _ = self.run_session(result, dry_run=dry_run)
            self.assertEqual(summary["applied"], 0)
            self.assertEqual(summary["ready"], int(dry_run))
            self.assertFalse(ne._history_path().exists())

    def test_history_write_failure_reports_submitted_but_stops(self):
        with patch.object(ne, "_record_applied_job", side_effect=OSError("disk full")), self.assertLogs(ne.log):
            summary, _, _ = self.run_session()
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(summary["stopped_reason"], "history_write_failed")


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1 to launch Chromium")
class NonEasyBrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = ne.sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(
            headless=True,
            executable_path=ne.config.get_chrome_path(),
        )
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(body="<h1>Local fixture</h1>", content_type="text/html"))
        self.page = self.context.new_page()
        self.page.goto(JOB_URL)

    def test_external_handoff_popup_and_same_tab(self):
        for target in ("_blank", "_self"):
            with self.subTest(target=target):
                self.page.goto(JOB_URL)
                self.page.set_content(f'<main><a aria-label="Apply on company website" href="{EXTERNAL_URL}" target="{target}">Apply</a></main>')
                destination, url = ne._open_external_application_page(self.page)
                self.assertEqual(url, EXTERNAL_URL)
                self.assertIsNotNone(destination)
                self.assertEqual(destination is self.page, target == "_self")
                if destination is not self.page:
                    destination.close()

    def test_sdui_easy_apply_exclusion(self):
        self.page.set_content('<div data-sdui-screen="jobs.SemanticJobDetails"><a aria-label="LinkedIn Apply to this job" href="/jobs/view/123/apply/">Apply</a></div>')
        self.assertTrue(ne._is_easy_apply(self.page))
        self.assertIsNone(ne._find_apply_control(self.page))

    def test_mailbox_tab_survives_application_cleanup(self):
        settings = {"mail_inbox_url": MAIL_URL}
        mailbox = ne._ensure_mailbox_tab(self.context, settings)
        self.assertIs(ne._ensure_mailbox_tab(self.context, settings), mailbox)
        external = self.context.new_page()
        external.goto(EXTERNAL_URL)
        ne._close_application_tabs(self.context, (self.page, mailbox))
        self.assertTrue(external.is_closed())
        self.assertFalse(mailbox.is_closed())


if __name__ == "__main__":
    unittest.main()
