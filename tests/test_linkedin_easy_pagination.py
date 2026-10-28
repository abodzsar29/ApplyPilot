import json
import os
from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest
import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import unified_apply as easy


def cards(first_id, count=25):
    return [Mock(job_url=f"https://www.linkedin.com/jobs/view/{job_id}/")
            for job_id in range(first_id, first_id + count)]


@pytest.fixture
def run_search():
    def run(*, page_count=3, max_scan_pages=None, delayed_page=None, no_change=False,
            click_ok=True, result="SKIPPED", max_applications=100, repeated=False, empty_initial=False):
        pages = [cards(1000 + index * 25) for index in range(page_count)]
        page = Mock(url="https://www.linkedin.com/jobs/search-results/")
        browser = Mock()
        browser.new_page.return_value = page
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = browser
        position = {"index": 0, "reads": 0}

        def read_cards(_page):
            index = position["index"]
            position["reads"] += 1
            if empty_initial:
                return []
            if delayed_page == index + 1 and position["reads"] < 4:
                return []
            if no_change and index:
                return pages[index - 1]
            if repeated and index == 2:
                return pages[0]
            return pages[index]

        def next_button(_page):
            return Mock() if position["index"] + 1 < page_count else None

        def click(_button):
            if click_ok:
                position["index"] += 1
                position["reads"] = 0
            return click_ok

        settings = {"job_title": "Software", "location": "Berlin, Germany", "profile": {},
                    "answers": {}, "intelligent_apply": False, "company_cooldown_days": 0}
        if max_scan_pages is not None:
            settings["max_scan_pages"] = max_scan_pages
        with ExitStack() as stack:
            manager = stack.enter_context(patch.object(easy, "sync_playwright"))
            manager.return_value.__enter__.return_value = playwright
            stack.enter_context(patch.object(easy, "ApplicationHistory"))
            stack.enter_context(patch.object(easy, "_browser_launch_options", return_value={}))
            stack.enter_context(patch.object(easy, "_log_active_linkedin_search_state"))
            stack.enter_context(patch.object(easy, "_active_linkedin_search_location", return_value="Berlin, Germany"))
            stack.enter_context(patch.object(easy, "_ensure_configured_linkedin_search_location", return_value=True))
            stack.enter_context(patch.object(easy, "_find_easy_apply_limit_modal", return_value=None))
            stack.enter_context(patch.object(easy, "_dismiss_recruiter_upsell_if_present", return_value=False))
            stack.enter_context(patch.object(easy, "_extract_job_url_from_card", side_effect=lambda card: card.job_url))
            reader = stack.enter_context(patch.object(easy, "_find_linkedin_job_cards", side_effect=read_cards))
            finder = stack.enter_context(patch.object(easy, "_find_linkedin_next_page_button", side_effect=next_button))
            clicker = stack.enter_context(patch.object(easy, "_click_action_button", side_effect=click))
            stack.enter_context(patch.object(easy, "_scroll_linkedin_job_card_into_view"))
            selector = stack.enter_context(patch.object(easy, "_click_linkedin_job_card"))
            stack.enter_context(patch.object(easy, "_extract_company_name", return_value="Employer"))
            stack.enter_context(patch.object(easy, "_find_easy_apply_control", return_value=None if result == "SKIPPED" else Mock()))
            stack.enter_context(patch.object(easy, "_extract_job_detail_text", return_value=""))
            stack.enter_context(patch.object(easy, "page_job_metadata", return_value={}))
            stack.enter_context(patch.object(easy, "_open_easy_apply_for_job"))
            stack.enter_context(patch.object(easy, "_wait_for_easy_apply_form_ready"))
            stack.enter_context(patch.object(easy, "_fill_and_submit_form", return_value=result))
            stack.enter_context(patch.object(easy, "_close_easy_apply_modal"))
            summary = easy.search_and_apply(settings, max_applications=max_applications, dry_run=result == "READY")
        return summary, reader, finder, clicker, selector, browser
    return run


def test_delayed_third_page_does_not_stop_after_50_cards(run_search):
    summary, _, _, clicker, selector, _ = run_search(delayed_page=3)
    assert summary["skipped"] == 75
    assert summary["failed"] == summary["applied"] == 0
    assert summary["search_stats"]["pages_scanned"] == 3
    assert summary["search_stats"]["stop_reason"] == "no_next_page"
    assert clicker.call_count == 2
    assert selector.call_count == 75


@pytest.mark.parametrize("limit", [1, 2, 3, "3"])
def test_easy_apply_honors_max_scan_pages_from_config(run_search, limit):
    summary, _, _, clicker, selector, _ = run_search(page_count=5, max_scan_pages=limit)
    assert summary["search_stats"]["pages_scanned"] == int(limit)
    assert summary["search_stats"]["candidate_urls"] == summary["skipped"] == 25 * int(limit)
    assert summary["search_stats"]["stop_reason"] == "max_scan_pages_reached"
    assert clicker.call_count == int(limit) - 1
    assert selector.call_count == 25 * int(limit)


@pytest.mark.parametrize("limit", [None, 2000])
def test_easy_apply_scans_all_13_pages_when_jobs_are_skipped(run_search, limit):
    summary, _, _, clicker, selector, browser = run_search(page_count=13, max_scan_pages=limit)
    assert summary["search_stats"]["pages_scanned"] == 13
    assert summary["skipped"] == summary["search_stats"]["candidate_urls"] == 325
    assert summary["search_stats"]["stop_reason"] == "no_next_page"
    assert summary["applied"] == summary["failed"] == 0
    assert clicker.call_count == 12
    assert selector.call_count == 325
    browser.close.assert_called_once()


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.5, "", "bad", [], {}])
def test_invalid_max_scan_pages_fails_before_browser_or_history_initialization(value):
    with (patch.object(easy, "sync_playwright") as browser, patch.object(easy, "ApplicationHistory") as history,
          pytest.raises(ValueError, match="max_scan_pages")):
        easy.search_and_apply({"intelligent_apply": False, "max_scan_pages": value})
    browser.assert_not_called()
    history.assert_not_called()


@pytest.mark.parametrize("result", ["APPLIED", "READY"])
def test_successful_application_limit_still_stops_before_next_page(run_search, result):
    summary, _, _, clicker, selector, _ = run_search(result=result, max_applications=1)
    assert summary["search_stats"]["stop_reason"] == "max_applications_reached"
    assert summary["search_stats"]["candidate_urls"] == 1
    assert summary["applied" if result == "APPLIED" else "ready"] == 1
    selector.assert_called_once()
    clicker.assert_not_called()


def test_pagination_timeout_is_not_reported_as_results_exhausted(run_search, caplog):
    summary, reader, _, clicker, selector, _ = run_search(no_change=True)
    assert summary["search_stats"]["stop_reason"] == "pagination_no_new_results"
    assert summary["search_stats"]["pages_scanned"] == 1
    assert summary["skipped"] == selector.call_count == 25
    assert reader.call_count == 41  # Initial page, then 40 bounded polls of stale results.
    clicker.assert_called_once()
    assert "pagination timed out" in caplog.text
    assert "does not confirm" in caplog.text


def test_initial_empty_results_are_retried_and_reported_separately(run_search):
    summary, reader, _, clicker, selector, _ = run_search(empty_initial=True)
    assert summary["search_stats"]["stop_reason"] == "initial_results_timeout"
    assert summary["search_stats"]["pages_scanned"] == 0
    assert reader.call_count == 40
    clicker.assert_not_called()
    selector.assert_not_called()


def test_click_failure_has_distinct_stop_reason(run_search):
    summary, _, _, clicker, selector, _ = run_search(click_ok=False)
    assert summary["search_stats"]["stop_reason"] == "pagination_click_failed"
    assert summary["skipped"] == selector.call_count == 25
    clicker.assert_called_once()


def test_previous_page_reappearing_does_not_loop_or_process_jobs_again(run_search):
    summary, _, _, clicker, selector, _ = run_search(repeated=True)
    assert summary["search_stats"]["stop_reason"] == "repeated_results_page"
    assert summary["search_stats"]["pages_scanned"] == 2
    assert summary["skipped"] == selector.call_count == 50
    assert clicker.call_count == 2


def test_wait_ignores_empty_stale_and_temporarily_detached_cards():
    old, new = cards(1000), cards(2000)
    page = Mock()
    with (patch.object(easy, "_find_linkedin_job_cards", side_effect=[
        [], old, easy.PlaywrightError("Execution context was destroyed"), new,
    ]), patch.object(easy, "_extract_job_url_from_card", side_effect=lambda card: card.job_url)):
        assert easy._wait_for_linkedin_job_cards(page, easy._linkedin_result_card_keys(old)) is new
    assert page.wait_for_timeout.call_count == 3


def test_wait_stops_if_the_browser_closes():
    page = Mock()
    page.wait_for_timeout.side_effect = easy.PlaywrightError("Target page has been closed")
    with patch.object(easy, "_find_linkedin_job_cards", return_value=[]) as read:
        assert easy._wait_for_linkedin_job_cards(page) == []
    read.assert_called_once()


def test_same_job_with_different_tracking_parameters_is_not_a_new_result():
    variants = [Mock(job_url=f"https://www.linkedin.com/jobs/view/123/?trackingId={number}") for number in (1, 2)]
    with patch.object(easy, "_extract_job_url_from_card", side_effect=lambda card: card.job_url):
        assert easy._linkedin_result_card_keys(variants) == {"123"}


def test_cli_shows_scan_limit_and_explicit_stop_reason(tmp_path):
    from typer.testing import CliRunner

    from applypilot.cli import app

    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"fixture")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"job_title": "Software", "location": "Berlin", "profile": {}, "answers": {},
                                "resume_path": str(resume), "max_scan_pages": 2000, "max_applications": 100}))
    with (patch("applypilot.cli._bootstrap"), patch("applypilot.config.check_tier"),
          patch("applypilot.cli._prepare_intelligent_settings", return_value=False),
          patch.object(easy, "search_and_apply", return_value={
              "applied": 0, "ready": 0, "failed": 2, "skipped": 48,
              "search_stats": {"pages_scanned": 2, "candidate_urls": 50, "stop_reason": "pagination_no_new_results"},
          }) as runner):
        result = CliRunner().invoke(app, ["linkedin-apply", "--config", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Max scan pages: 2000" in result.output
    assert "Pages scanned: 2" in result.output
    assert "Jobs inspected: 50" in result.output
    assert "Scan stop reason: pagination_no_new_results" in result.output
    assert runner.call_args.kwargs["config_dict"]["max_scan_pages"] == 2000


@pytest.fixture
def local_browser():
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("Set RUN_BROWSER_TESTS=1 to launch Chromium")
    harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    harness.setUp()
    try:
        yield harness.page
    finally:
        harness.doCleanups()


@pytest.mark.parametrize("control", [
    '<button data-testid="pagination-controls-next-button-visible" id="next">Next</button>',
    '<button aria-label="View next page" id="next">Next</button>',
    '<a aria-label="Next page" id="next" href="#">Next</a>',
])
def test_browser_waits_longer_than_old_two_seconds_for_next_page(local_browser, control):
    local_browser.set_content('''
        <div id="results"><div componentkey="job-card-component-ref-1000">First page</div></div>
    ''' + control + '''
        <script>
            document.getElementById('next').onclick = event => {
                event.preventDefault();
                document.getElementById('results').innerHTML = '';
                setTimeout(() => {
                    document.getElementById('results').innerHTML =
                        '<div componentkey="job-card-component-ref-2000">Second page</div>';
                    document.getElementById('next').setAttribute('aria-disabled', 'true');
                }, 2400);
            };
        </script>
    ''')
    original_keys = easy._linkedin_result_card_keys(easy._wait_for_linkedin_job_cards(local_browser))
    fresh, reason = easy._next_linkedin_results_page(local_browser, 1, original_keys)
    assert reason == ""
    assert easy._linkedin_result_card_keys(fresh) == {"2000"}
    assert easy._next_linkedin_results_page(local_browser, 2, {"2000"}) == ([], "no_next_page")
