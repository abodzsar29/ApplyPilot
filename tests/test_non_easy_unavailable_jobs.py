import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import non_easy as ne
from applypilot.linkedin.job_status import JobAvailabilityMonitor, unavailable_job_reason
from applypilot.qwen_mcp import QwenMCPAgent

FIXTURE = Path(__file__).parent / "fixtures" / "personio_job_not_found.html"


@pytest.fixture
def harness():
    runner = existing_non_easy.NonEasyTests("test_only_english_rejects_german_and_unknown")
    runner.setUp()
    try:
        yield runner
    finally:
        runner.doCleanups()


@pytest.mark.parametrize("filtered", [False, True])
def test_unavailable_job_skips_before_cv_or_agent_and_scanning_continues(harness, filtered):
    settings = harness.settings | {"max_applications": 1, "jobboard_filter": filtered, "company_cooldown_days": 30}
    if filtered:
        settings["_project_root"] = str(harness.root)
        (harness.root / "jobboards.json").write_text(json.dumps({"test": ["employer.test"]}))
    preparer = Mock()
    settings["_intelligent_preparer"] = preparer
    details = {"title": "Engineer", "description": existing_non_easy.ENGLISH, "company": "Employer"}
    with (patch.object(ne, "JobAvailabilityMonitor") as monitor,
          patch("applypilot.linkedin.scrape.read_current_job", return_value=details)):
        # Each missing job is skipped on the first check, not retried or prepared.
        monitor.return_value.reason.return_value = "job_unavailable: This URL does not exist"
        summary, opener, agent = harness.run_session(settings=settings, jobs=2)
    assert summary["skipped"] == summary["search_stats"]["expired"] == 2
    assert summary["applied"] == summary["failed"] == summary["found"] == 0
    assert summary["search_stats"]["company_cooldown"] == 0
    assert summary["jobs"] == []
    assert opener.call_count == 2
    preparer.prepare.assert_not_called()
    agent.assert_not_called()
    assert not ne._history_path().exists()


def test_page_becoming_unavailable_after_cv_preparation_is_skipped(harness):
    preparer = Mock()
    preparer.prepare.return_value.published_pdf = harness.root / "english.pdf"
    details = {"title": "Engineer", "description": existing_non_easy.ENGLISH, "company": "Employer"}
    with (patch.object(ne, "JobAvailabilityMonitor") as monitor,
          patch("applypilot.linkedin.scrape.read_current_job", return_value=details)):
        monitor.return_value.reason.side_effect = [None, "job_unavailable: HTTP 410"]
        summary, _, agent = harness.run_session(settings=harness.settings | {"_intelligent_preparer": preparer})
    assert summary["search_stats"]["expired"] == 1
    preparer.prepare.return_value.finish.assert_called_once_with("skipped")
    agent.assert_not_called()


def test_agent_expired_result_is_a_skip_and_does_not_consume_application_limit(harness):
    summary, _, agent = harness.run_session("expired", jobs=2, settings=harness.settings | {"max_applications": 1})
    assert summary["search_stats"]["expired"] == summary["skipped"] == 2
    assert summary["failed"] == summary["applied"] == summary["found"] == 0
    assert agent.call_count == 2
    assert not ne._history_path().exists()


@pytest.mark.parametrize("provider", ["openrouter", "dashscope", "claude"])
def test_terminal_page_guard_runs_before_any_provider_request(harness, provider):
    agent = Mock()
    with patch.object(ne.subprocess, "Popen") as spawn:
        result, duration = ne._run_external_application(
            provider, "model", agent, harness.job,
            harness.settings | {"_application_state_check": lambda: "RESULT:EXPIRED"}, 9222, False,
        )
    assert result == "expired" and duration == 0
    agent.run_prompt.assert_not_called()
    spawn.assert_not_called()


def test_qwen_runtime_guard_stops_after_navigation_without_another_model_turn_or_retry():
    state = {"expired": False, "calls": 0, "closed": False}

    class FakeAssistant:
        def run(self, messages):
            try:
                state["calls"] += 1
                yield [{"role": "assistant", "content": "Opening application"}]
                state["expired"] = True
                yield [{"role": "function", "content": "404: This URL does not exist"}]
                raise AssertionError("Must stop before another model request")
            finally:
                state["closed"] = True

    agent = QwenMCPAgent.__new__(QwenMCPAgent)
    agent._assistant = FakeAssistant()
    result = agent.run_prompt("Apply", stop_check=lambda: "RESULT:EXPIRED" if state["expired"] else None)
    assert result == "RESULT:EXPIRED"
    assert state["calls"] == 1
    assert state["closed"]


def test_claude_runtime_guard_terminates_only_its_cli_process(harness):
    proc = Mock(stdin=io.StringIO(), stdout=iter([json.dumps({"type": "assistant", "message": {"content": []}})]))
    proc.stdin = Mock()
    check = Mock(side_effect=[None, "RESULT:EXPIRED"])
    with (patch.object(ne.subprocess, "Popen", return_value=proc),
          patch.object(ne, "reset_worker_dir", return_value=harness.root)):
        result, _ = ne._run_external_application_once(
            "claude", "model", None, harness.job, harness.settings | {"_application_state_check": check}, 9222, False,
        )
    assert result == "expired"
    proc.terminate.assert_called_once()
    proc.wait.assert_called_once_with(timeout=5)


def test_runtime_guard_ignores_preexisting_mailbox_and_checks_new_application_tabs():
    application, mailbox, popup = (Mock(url=url) for url in (
        "https://employer.test/apply", "https://mail.test/inbox", "https://employer.test/deleted",
    ))
    for page in (application, mailbox, popup):
        page.is_closed.return_value = False
    monitor = Mock(context=Mock(pages=[mailbox, application, popup]))
    monitor.reason.side_effect = lambda page: "job_unavailable: HTTP 404" if page is popup else None
    check = ne._application_state_check(monitor, application, {application, mailbox})
    assert check() == "RESULT:EXPIRED"
    assert [call.args[0] for call in monitor.reason.call_args_list] == [application, popup]


def test_runtime_guard_ignores_new_mailbox_tabs_too():
    application = Mock(url="https://employer.test/apply")
    mailbox = Mock(url="https://mail.test/not-found")
    application.is_closed.return_value = mailbox.is_closed.return_value = False
    monitor = Mock(context=Mock(pages=[application, mailbox]))
    monitor.reason.side_effect = lambda page: "job_unavailable: HTTP 404" if page is mailbox else None
    check = ne._application_state_check(monitor, application, {application}, mailbox_host="mail.test")
    assert check() is None
    monitor.reason.assert_called_once_with(application)


def test_runtime_guard_checks_application_even_when_chromium_changes_its_url_to_an_error_page():
    application = Mock(url="chrome-error://chromewebdata/")
    application.is_closed.return_value = False
    monitor = Mock(context=Mock(pages=[application]))
    monitor.reason.return_value = "job_unavailable: HTTP 404"
    check = ne._application_state_check(monitor, application, {application})
    assert check() == "RESULT:EXPIRED"
    monitor.reason.assert_called_once_with(application)


@pytest.fixture
def browser_page():
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("Set RUN_BROWSER_TESTS=1")
    browser = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    browser.setUp()
    try:
        yield browser.page
    finally:
        browser.doCleanups()


def test_detects_supplied_personio_soft_404_without_clicking_links(browser_page):
    browser_page.set_content(FIXTURE.read_text())
    original = browser_page.url
    assert unavailable_job_reason(browser_page) == "job_unavailable: This URL does not exist"
    assert browser_page.url == original


@pytest.mark.parametrize("text", [
    "Job not found", "Page not found", "404", "This position has been filled.",
    "This job is no longer available", "This job posting has expired", "Diese Stelle ist nicht mehr verfügbar",
])
def test_detects_visible_missing_or_closed_job_messages(browser_page, text):
    browser_page.set_content(f"<main><h2>{text}</h2><a href='/'>Back to all jobs</a></main>")
    assert unavailable_job_reason(browser_page)


@pytest.mark.parametrize("html", [
    "<main><h1>Software Engineer</h1><p>Implement 404 page not found error handling.</p><form><input></form></main>",
    "<main><h1>Software Engineer</h1><script>const error = 'This URL does not exist';</script></main>",
    "<main><h1>Software Engineer</h1><div hidden><h2>This URL does not exist</h2></div></main>",
    "<main><h1>Software Engineer</h1><h2>We build job not found recovery flows</h2></main>",
    "<main><h2>No jobs found</h2></main>",
    "<main><h2>Login required</h2></main>",
    "<main><h2>Application submitted successfully</h2></main>",
])
def test_does_not_treat_valid_content_hidden_errors_or_other_states_as_expired(browser_page, html):
    browser_page.set_content(html)
    assert unavailable_job_reason(browser_page) is None


@pytest.mark.parametrize("status", [404, 410])
@pytest.mark.parametrize("body", ["", "<main>Unavailable</main>"])
def test_tracks_main_document_http_errors(browser_page, status, body):
    monitor = JobAvailabilityMonitor(browser_page.context)
    try:
        browser_page.route("https://employer.test/**", lambda route: route.fulfill(
            status=status, body=body, content_type="text/html",
        ))
        try:
            browser_page.goto("https://employer.test/missing")
        except ne.PlaywrightError as exc:
            assert "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(exc)
        assert monitor.reason(browser_page) == f"job_unavailable: HTTP {status}"
        # The real runner closes unavailable tabs, including Chromium's internal
        # error pages, instead of trying to navigate them back to a live job.
    finally:
        monitor.close()


@pytest.mark.parametrize("status", [404, 410])
def test_clears_http_error_after_new_document_navigation(browser_page, status):
    monitor = JobAvailabilityMonitor(browser_page.context)
    try:
        browser_page.route("https://employer.test/**", lambda route: route.fulfill(
            status=status, body="<main>Unavailable</main>", content_type="text/html",
        ))
        browser_page.goto("https://employer.test/missing")
        assert monitor.reason(browser_page) == f"job_unavailable: HTTP {status}"
        browser_page.unroute("https://employer.test/**")
        browser_page.goto("https://employer.test/live")
        assert monitor.reason(browser_page) is None
    finally:
        monitor.close()


@pytest.mark.parametrize("status", [200, 404])
def test_runtime_guard_stops_qwen_after_real_browser_navigation(browser_page, status):
    browser_page.goto("https://employer.test/apply")
    monitor = JobAvailabilityMonitor(browser_page.context)
    calls = []

    class NavigatingAssistant:
        def run(self, messages):
            calls.append(messages)
            yield [{"role": "assistant", "content": "Opening application form"}]
            browser_page.goto("https://employer.test/missing")
            yield [{"role": "function", "content": "Missing job page"}]
            raise AssertionError("Must not issue another request after the missing page")

    try:
        browser_page.route("https://employer.test/missing", lambda route: route.fulfill(
            status=status, body=FIXTURE.read_text(), content_type="text/html",
        ))
        agent = QwenMCPAgent.__new__(QwenMCPAgent)
        agent._assistant = NavigatingAssistant()
        check = ne._application_state_check(monitor, browser_page, set(browser_page.context.pages))
        assert agent.run_prompt("Apply", stop_check=check) == "RESULT:EXPIRED"
        assert len(calls) == 1
    finally:
        monitor.close()


def test_ignores_missing_iframe_and_image_responses(browser_page):
    monitor = JobAvailabilityMonitor(browser_page.context)
    try:
        browser_page.route("https://employer.test/missing*", lambda route: route.fulfill(status=404, body=""))
        browser_page.set_content("<main><h1>Engineer</h1><iframe src='https://employer.test/missing-frame'></iframe>"
                                 "<img src='https://employer.test/missing.png'></main>")
        assert monitor.reason(browser_page) is None
    finally:
        monitor.close()
