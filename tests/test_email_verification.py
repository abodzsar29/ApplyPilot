"""Local-only regression tests for Proton iframe -> Greenhouse code controls."""

import hashlib
import logging
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import non_easy as ne
from applypilot.linkedin.email_verification import EmailVerificationHandler
from applypilot.linkedin.greenhouse import prefill_greenhouse_application
from applypilot.qwen_mcp import QwenMCPAgent

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://job-boards.greenhouse.io/example-co/jobs/123"
INBOX = "https://mail.proton.me/u/1/inbox"
CODE = "aB3dE6gH"
SETTINGS = {"profile": {"email": "candidate@example.test"}, "mail_inbox_url": INBOX}


def test_expired_page_stops_before_accessing_mail():
    verification = Mock()
    check = ne._application_progress_check(lambda: "RESULT:EXPIRED", verification)
    assert check() == "RESULT:EXPIRED"
    verification.check.assert_not_called()


@pytest.mark.parametrize("provider", ["openrouter", "dashscope", "claude"])
def test_verification_failure_is_terminal_before_a_provider_call(tmp_path, provider):
    agent = Mock()
    settings = SETTINGS | {"_application_state_check": lambda: "RESULT:FAILED:email_2fa_unavailable"}
    with patch.object(ne.subprocess, "Popen") as spawn:
        result, _ = ne._run_external_application(provider, "unused", agent, {}, settings, 9222, False)
    assert result == "failed:email_2fa_unavailable"
    agent.run_prompt.assert_not_called()
    spawn.assert_not_called()


@pytest.fixture
def browser():
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("Set RUN_BROWSER_TESTS=1 for local Chromium fixtures")
    harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    harness.setUp()
    try:
        harness.page.goto(URL)
        harness.page.set_content((FIXTURES / "greenhouse_security_code.html").read_text())
        mailbox = harness.context.new_page()
        mailbox.goto(INBOX + "/conversation")
        mailbox.set_content((FIXTURES / "proton_greenhouse_code.html").read_text())
        mailbox.evaluate("""code => {
            const articles = document.querySelectorAll('article');
            articles[1].querySelector('time').setAttribute('datetime', new Date(Date.now() - 5000).toISOString());
            articles[0].querySelector('iframe').contentDocument.body.innerHTML = '<h1>Old12345</h1>';
            articles[1].querySelector('iframe').contentDocument.body.innerHTML = '<p>Copy and paste this code into your application:</p><h1>' + code + '</h1>';
        }""", CODE)
        yield harness.page, mailbox
    finally:
        harness.doCleanups()


@pytest.mark.parametrize("segmented", [True, False])
@pytest.mark.parametrize("rerender", [True, False])
def test_reads_iframe_and_fills_real_inputs_case_sensitively_without_submitting_or_logging(browser, segmented, rerender, caplog):
    page, mailbox = browser
    page.evaluate("([segmented, rerender]) => { window.segmented = segmented; window.rerender = rerender; window.renderInputs(); }",
                  [segmented, rerender])
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)
    with caplog.at_level(logging.INFO):
        assert handler.check() is None
    assert page.evaluate("window.verificationState.join('')") == CODE
    assert page.evaluate("window.inputEvents.every(Boolean)")
    assert page.locator("#submit").is_enabled()
    assert page.evaluate("window.submitCount") == 0
    assert CODE not in caplog.text
    assert CODE not in repr(handler.__dict__)
    with patch.object(handler, "_read_code", side_effect=AssertionError("Already complete")):
        assert handler.check() is None


@pytest.mark.parametrize("change", [
    "document.querySelector('[data-testid=\"recipients:sender\"]').href = 'mailto:no-reply@greenhouse-mail.io.evil.test'",
    "document.querySelector('article:last-child [data-testid=\"message-header-expanded:To\"] a').href = 'mailto:someone-else@example.test'",
    "document.querySelector('article:last-child iframe').dataset.subject = 'Security code for your application to Another Employer'",
    "document.querySelector('article:last-child time').setAttribute('datetime', '2026-09-12T21:36:00Z')",
    "document.querySelector('article:last-child iframe').contentDocument.body.innerHTML = '<h1>12345678</h1><h1>87654321</h1>'",
    "document.querySelector('article:last-child iframe').contentDocument.body.innerHTML = '<h1>12345</h1>'",
])
def test_rejects_wrong_sender_recipient_subject_stale_or_ambiguous_codes(browser, change):
    page, mailbox = browser
    mailbox.evaluate(change)
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)
    assert handler.check() == "RESULT:FAILED:email_2fa_unavailable"
    assert page.evaluate("window.verificationState.join('')") == ""
    assert page.evaluate("window.submitCount") == 0
    with patch.object(handler, "_read_code", side_effect=AssertionError("Bounded terminal failure")):
        assert handler.check() == "RESULT:FAILED:email_2fa_unavailable"


def test_does_not_reuse_a_previous_code_or_fall_back_to_an_older_message(browser):
    page, mailbox = browser
    mailbox.evaluate("""() => {
        const articles = document.querySelectorAll('article');
        articles[0].setAttribute('data-expanded', 'true');
        articles[0].querySelector('time').setAttribute('datetime', new Date(Date.now() - 10000).toISOString());
    }""")
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)
    handler.attempted.add(hashlib.sha256(CODE.encode()).hexdigest())
    assert handler.check() == "RESULT:FAILED:email_2fa_unavailable"
    assert page.evaluate("window.verificationState.join('')") == ""


def test_opens_matching_inbox_subject_then_reads_fresh_message(browser):
    page, mailbox = browser
    mailbox.evaluate("""() => {
        const articles = document.querySelectorAll('article');
        for (const article of articles) article.removeAttribute('data-testid');
        const heading = document.createElement('h2');
        heading.textContent = 'Security code for your application to Example Co';
        heading.onclick = () => articles.forEach((article, index) => article.dataset.testid = 'message-view-' + index);
        document.body.prepend(heading);
    }""")
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)
    assert handler.check() is None
    assert page.evaluate("window.verificationState.join('')") == CODE


def test_expands_the_latest_message_without_touching_the_older_message(browser):
    page, mailbox = browser
    mailbox.evaluate("""() => {
        const current = document.querySelector('article:last-child');
        current.dataset.expanded = 'false';
        const header = current.querySelector('[data-testid="message-header-expanded:1"]');
        header.dataset.testid = 'message-header-collapsed:1';
        header.onclick = () => current.dataset.expanded = 'true';
    }""")
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)
    assert handler.check() is None
    assert page.evaluate("window.verificationState.join('')") == CODE
    assert mailbox.locator("article").first.get_attribute("data-expanded") == "false"


def test_readable_proton_local_datetime_is_accepted(browser):
    page, mailbox = browser
    mailbox.evaluate("""() => {
        const date = new Date(Date.now() - 5000);
        const day = date.toLocaleDateString('en-GB', {weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'});
        const hour = date.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
        document.querySelector('article:last-child time').setAttribute('datetime', day + ' at ' + hour);
    }""")
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)
    assert handler.check() is None
    assert page.evaluate("window.verificationState.join('')") == CODE


def test_waits_for_the_iframe_body_to_finish_loading(browser):
    page, mailbox = browser
    mailbox.evaluate("""code => {
        const frame = document.querySelector('article:last-child iframe');
        frame.contentDocument.body.innerHTML = '';
        setTimeout(() => frame.contentDocument.body.innerHTML = '<h1>' + code + '</h1>', 100);
    }""", CODE)
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)
    assert handler.check() is None
    assert page.evaluate("window.verificationState.join('')") == CODE


def test_preserves_protons_active_account_when_returning_to_the_inbox(browser):
    page, mailbox = browser
    settings = SETTINGS | {"mail_inbox_url": "https://mail.proton.me/u/0/inbox"}
    handler = EmailVerificationHandler(page, mailbox, settings, "Example Co", poll_delay=0)
    handler._open_message()
    assert mailbox.url == INBOX


def test_does_not_read_mail_without_a_visible_application_challenge(browser):
    page, mailbox = browser
    page.locator("#verification").evaluate("el => el.hidden = true")
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)
    with patch.object(handler, "_read_code") as read:
        assert handler.check() is None
        read.assert_not_called()


@pytest.mark.parametrize("destination", ["https://other.test/confirm", "https://job-boards.greenhouse.io/other/jobs/456"])
def test_does_not_fill_a_code_if_the_application_redirects_while_reading_mail(browser, destination):
    page, mailbox = browser
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)
    def redirected(_length):
        page.goto(destination)
        page.set_content((FIXTURES / "greenhouse_security_code.html").read_text())
        return CODE
    with patch.object(handler, "_read_code", side_effect=redirected):
        assert handler.check() == "RESULT:FAILED:email_verification_input_failed"
    assert page.evaluate("window.verificationState.join('')") == ""


def test_ordinary_answer_prefill_does_not_fill_security_code_boxes_with_placeholders(browser):
    page, _ = browser
    prefill_greenhouse_application(page, SETTINGS)
    assert page.evaluate("window.verificationState.join('')") == ""
    assert page.evaluate("window.inputEvents") == []


@pytest.mark.parametrize("reason", ["no_mailbox", "wrong_challenge_recipient", "login_required", "host_not_allowed"])
def test_mail_access_requires_current_challenge_configured_session_recipient_and_allowed_host(browser, reason):
    page, mailbox = browser
    settings = SETTINGS
    if reason == "no_mailbox":
        mailbox = None
    elif reason == "wrong_challenge_recipient":
        page.locator("#verification").evaluate("el => el.setAttribute('aria-label', el.getAttribute('aria-label').replace('candidate@example.test', 'other@example.test'))")
    elif reason == "login_required":
        mailbox.goto("https://account.proton.me/login")
    else:
        settings = SETTINGS | {"_jobboard_filter": Mock(matches=Mock(return_value=False))}
        with patch("applypilot.linkedin.email_verification.load_jobboard_filter", return_value=settings["_jobboard_filter"]):
            handler = EmailVerificationHandler(page, mailbox, settings, "Example Co", poll_delay=0)
            with patch.object(handler, "_read_code") as read:
                assert handler.check() is None
                read.assert_not_called()
        return
    handler = EmailVerificationHandler(page, mailbox, settings, "Example Co", poll_delay=0)
    with patch.object(handler, "_read_code") as read:
        assert handler.check() == "RESULT:FAILED:email_2fa_unavailable"
        read.assert_not_called()


def test_runtime_hook_fills_code_before_another_agent_turn(browser):
    page, mailbox = browser
    page.locator("#verification").evaluate("el => el.hidden = true")
    handler = EmailVerificationHandler(page, mailbox, SETTINGS, "Example Co", poll_delay=0)

    class Assistant:
        def run(self, messages):
            yield [{"role": "assistant", "content": "Submitting the prepared form"}]
            page.locator("#verification").evaluate("el => el.hidden = false")
            yield [{"role": "function", "content": "Verification code required"}]
            assert page.evaluate("window.verificationState.join('')") == CODE
            yield [{"role": "assistant", "content": "RESULT:READY"}]

    agent = QwenMCPAgent.__new__(QwenMCPAgent)
    agent._assistant = Assistant()
    check = ne._application_progress_check(lambda: None, handler)
    assert "RESULT:READY" in agent.run_prompt("Apply", stop_check=check)
    assert page.evaluate("window.submitCount") == 0
