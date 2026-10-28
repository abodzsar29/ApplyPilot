import json
import os
from unittest.mock import Mock, patch

import pytest
import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import non_easy as ne


@pytest.fixture
def harness():
    instance = existing_non_easy.NonEasyTests("test_search_uses_semantic_results_route")
    instance.setUp()
    try:
        yield instance
    finally:
        instance.doCleanups()


def result_cards(first_id, count=25):
    cards = []
    for job_id in range(first_id, first_id + count):
        card = Mock()
        card.get_attribute.side_effect = lambda name, job_id=job_id: (
            f"https://www.linkedin.com/jobs/view/{job_id}/" if name == "href" else None
        )
        card.text_content.return_value = "Software Engineer"
        cards.append(card)
    return cards


@pytest.mark.parametrize("value", [1, 4, 20, "20"])
def test_page_limit_is_read_from_config(value):
    assert ne._configured_max_scan_pages({"max_scan_pages": value}) == int(value)
    assert ne._configured_max_scan_pages({}) == 3


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.5, "", "many", {}, []])
def test_invalid_page_limit_fails_before_browser_start(harness, value):
    settings = harness.settings | {"max_scan_pages": value}
    with patch.object(ne, "launch_chrome") as launch:
        with pytest.raises(ValueError, match="max_scan_pages"):
            ne.validate_non_easy_config(settings)
        with pytest.raises(ValueError, match="max_scan_pages"):
            ne.run_non_easy_apply(settings)
        with pytest.raises(ValueError, match="max_scan_pages"):
            ne.search_non_easy_jobs(settings)
    launch.assert_not_called()


@pytest.mark.parametrize("page_limit", [1, 3, 4, 5])
def test_direct_run_honors_page_limit_and_moves_past_first_25(harness, page_limit):
    pages = [result_cards(1000 + 25 * number) for number in range(page_limit)]
    with (patch.object(ne, "_wait_for_result_cards", return_value=pages[0]),
          patch.object(ne, "_next_results_page", side_effect=[(cards, "") for cards in pages[1:]]) as advance):
        summary, opener, runner = harness.run_session(
            settings=harness.settings | {"max_applications": 100, "max_scan_pages": page_limit}, easy=True,
        )
    assert summary["search_stats"]["pages_scanned"] == page_limit
    assert summary["search_stats"]["candidate_urls"] == summary["skipped"] == 25 * page_limit
    assert summary["search_stats"]["stop_reason"] == "max_scan_pages_reached"
    assert summary["applied"] == summary["failed"] == 0
    assert advance.call_count == page_limit - 1
    opener.assert_not_called()
    runner.assert_not_called()


def test_missing_page_limit_still_defaults_to_three_pages(harness):
    pages = [result_cards(1000 + 25 * number) for number in range(3)]
    with (patch.object(ne, "_wait_for_result_cards", return_value=pages[0]),
          patch.object(ne, "_next_results_page", side_effect=[(cards, "") for cards in pages[1:]]) as advance):
        summary, _, _ = harness.run_session(settings=harness.settings | {"max_applications": 100}, easy=True)
    assert summary["search_stats"]["pages_scanned"] == 3
    assert summary["search_stats"]["stop_reason"] == "max_scan_pages_reached"
    assert advance.call_count == 2


def test_direct_run_preserves_existing_candidate_safety_cap(harness):
    with (patch.object(ne, "_wait_for_result_cards", return_value=result_cards(1000)),
          patch.object(ne, "_next_results_page") as advance):
        summary, _, _ = harness.run_session(
            settings=harness.settings | {"max_applications": 1, "max_scan_pages": 20}, easy=True,
        )
    assert summary["search_stats"]["candidate_urls"] == 4
    assert summary["search_stats"]["stop_reason"] == "candidate_limit_reached"
    advance.assert_not_called()


def test_direct_run_preserves_application_limit(harness):
    with patch.object(ne, "_next_results_page") as advance:
        summary, _, runner = harness.run_session(settings=harness.settings | {"max_applications": 1}, jobs=2)
    assert summary["applied"] == 1
    assert summary["search_stats"]["stop_reason"] == "max_applications_reached"
    runner.assert_called_once()
    advance.assert_not_called()


def test_direct_run_stops_on_empty_results_without_application_calls(harness):
    with (patch.object(ne, "_wait_for_result_cards", return_value=[]),
          patch.object(ne, "_next_results_page") as advance):
        summary, opener, runner = harness.run_session()
    assert summary["search_stats"]["pages_scanned"] == 0
    assert summary["search_stats"]["stop_reason"] == "no_results"
    opener.assert_not_called()
    runner.assert_not_called()
    advance.assert_not_called()


def test_direct_run_stops_on_repeated_page_without_reprocessing_jobs(harness):
    first, second = result_cards(1000), result_cards(2000)
    with (patch.object(ne, "_wait_for_result_cards", return_value=first),
          patch.object(ne, "_next_results_page", side_effect=[(second, ""), (first, "")])):
        summary, _, _ = harness.run_session(
            settings=harness.settings | {"max_applications": 100, "max_scan_pages": 20}, easy=True,
        )
    assert summary["search_stats"]["pages_scanned"] == 2
    assert summary["search_stats"]["candidate_urls"] == summary["skipped"] == 50
    assert summary["search_stats"]["stop_reason"] == "repeated_results_page"


@pytest.mark.parametrize("reason", ["no_next_page", "pagination_click_failed", "pagination_no_new_results"])
def test_direct_run_reports_pagination_stop_reason(harness, reason, caplog):
    with (patch.object(ne, "_next_results_page", return_value=([], reason)),
          caplog.at_level("INFO", logger=ne.__name__)):
        summary, _, _ = harness.run_session(easy=True)
    assert summary["search_stats"]["stop_reason"] == reason
    assert reason in caplog.text


def test_wait_for_results_handles_empty_stale_and_transient_navigation():
    first, second = result_cards(1000), result_cards(2000)
    with (patch.object(ne, "_iter_result_cards", side_effect=[
        [], first, ne.PlaywrightError("Navigation replaced the document"), second,
    ]), patch.object(ne, "_safe_wait_for_timeout", return_value=True) as wait):
        assert ne._wait_for_result_cards(Mock(), ne._result_card_urls(first)) is second
    assert wait.call_count == 3


def test_wait_for_results_is_bounded_when_page_does_not_change():
    cards = result_cards(1000)
    with (patch.object(ne, "_iter_result_cards", return_value=cards) as read,
          patch.object(ne, "_safe_wait_for_timeout", return_value=True) as wait):
        assert ne._wait_for_result_cards(Mock(), ne._result_card_urls(cards)) == []
    assert read.call_count == 40
    assert wait.call_count == 39


def test_wait_for_results_stops_if_search_page_closes():
    with (patch.object(ne, "_iter_result_cards", return_value=[]) as read,
          patch.object(ne, "_safe_wait_for_timeout", return_value=False)):
        assert ne._wait_for_result_cards(Mock()) == []
    read.assert_called_once()


@pytest.mark.parametrize(("found", "clicked", "updated", "reason"), [
    (False, False, False, "no_next_page"),
    (True, False, False, "pagination_click_failed"),
    (True, True, False, "pagination_no_new_results"),
    (True, True, True, ""),
])
def test_next_page_uses_shared_click_helpers_and_checks_progress(found, clicked, updated, reason):
    page, button = Mock(), Mock()
    cards = result_cards(2000) if updated else []
    previous = {"https://www.linkedin.com/jobs/view/1000/"}
    with (patch.object(ne, "_find_linkedin_next_page_button", return_value=button if found else None) as find,
          patch.object(ne, "_click_action_button", return_value=clicked) as click,
          patch.object(ne, "_wait_for_result_cards", return_value=cards) as wait):
        assert ne._next_results_page(page, 1, previous) == (cards, reason)
    find.assert_called_once_with(page)
    if found:
        click.assert_called_once_with(button)
    else:
        click.assert_not_called()
    if found and clicked:
        wait.assert_called_once_with(page, previous)
    else:
        wait.assert_not_called()


def test_discovery_uses_same_page_limit_and_shared_paginator(harness):
    pages = [result_cards(1000 + 25 * number) for number in range(4)]
    search, detail = Mock(), Mock()
    browser = Mock(pages=[search])
    browser.new_page.return_value = detail
    with (patch.object(ne.config, "ensure_dirs"),
          patch.object(ne.config, "CHROME_WORKER_DIR", harness.root / "chrome"),
          patch.object(ne, "sync_playwright") as playwright,
          patch.object(ne, "_wait_for_result_cards", return_value=pages[0]),
          patch.object(ne, "_next_results_page", side_effect=[(cards, "") for cards in pages[1:]]) as advance,
          patch.object(ne, "_is_easy_apply", return_value=True),
          patch.object(ne, "_extract_external_apply_url") as external):
        playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = browser
        jobs, stats = ne.search_non_easy_jobs(harness.settings | {"max_scan_pages": 4, "max_applications": 100})
    assert jobs == []
    assert stats["pages_scanned"] == 4
    assert stats["candidate_urls"] == stats["easy_skipped"] == 100
    assert stats["stop_reason"] == "max_scan_pages_reached"
    assert advance.call_count == 3
    external.assert_not_called()
    browser.close.assert_called_once()


def test_cli_shows_configured_page_limit_and_scan_stop_reason(harness):
    from typer.testing import CliRunner

    from applypilot.cli import app

    path = harness.root / "config.json"
    path.write_text(json.dumps(harness.settings | {"max_scan_pages": 20}))
    with (patch("applypilot.cli._bootstrap"), patch("applypilot.cli._prepare_intelligent_settings"),
          patch("applypilot.config.check_non_easy_provider_requirements"),
          patch("applypilot.qwen_mcp.get_effective_model_and_provider", return_value=("model", "openrouter")),
          patch.object(ne, "run_non_easy_apply", return_value={
              "found": 0, "applied": 0, "ready": 0, "skipped": 25, "failed": 0,
              "search_stats": {"pages_scanned": 1, "stop_reason": "no_next_page"},
          }) as runner):
        result = CliRunner().invoke(app, ["linkedin-noneasy", "--config", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Max scan pages: 20" in result.output
    assert "Pages scanned:       1" in result.output
    assert "Scan stop reason:    no_next_page" in result.output
    assert runner.call_args.kwargs["config_dict"]["max_scan_pages"] == 20


@pytest.fixture
def local_browser():
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("Set RUN_BROWSER_TESTS=1")
    instance = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    instance.setUp()
    try:
        yield instance.page
    finally:
        instance.doCleanups()


@pytest.mark.parametrize("control", [
    '<button id="next" data-testid="pagination-controls-next-button-visible"><svg width="20" height="20"></svg></button>',
    '<button id="next" aria-label="Next page"><svg width="20" height="20"></svg></button>',
    '<a id="next" aria-label="Next page" href="#">&#8594;</a>',
])
def test_browser_paginates_with_modern_buttons_and_legacy_links(local_browser, control):
    local_browser.set_content('''
        <div id="results"><a class="base-card__full-link" href="/jobs/view/1000/">Software Engineer</a></div>
        <button data-testid="pagination-controls-next-button-visible" style="display:none">Hidden Next</button>
        <button data-testid="pagination-controls-next-button-visible" aria-disabled="true">Disabled Next</button>
    ''' + control + '''
        <script>
          document.getElementById('next').onclick = event => {
            event.preventDefault();
            document.getElementById('results').innerHTML = '';
            setTimeout(() => {
              document.getElementById('results').innerHTML =
                '<a class="base-card__full-link" href="/jobs/view/2000/">Software Engineer</a>';
              document.getElementById('next').setAttribute('aria-disabled', 'true');
            }, 300);
          };
        </script>
    ''')
    previous = ne._result_card_urls(ne._wait_for_result_cards(local_browser))
    cards, reason = ne._next_results_page(local_browser, 1, previous)
    assert reason == ""
    assert ne._result_card_urls(cards) == {"https://www.linkedin.com/jobs/view/2000/"}
    assert ne._next_results_page(local_browser, 2, ne._result_card_urls(cards)) == ([], "no_next_page")
