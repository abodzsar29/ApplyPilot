"""Non-Easy Apply must repair the rendered location before inspecting jobs."""

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import non_easy as ne
from applypilot.linkedin import unified_apply as easy

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_noneasy_location.html"


@pytest.fixture
def harness():
    runner = existing_non_easy.NonEasyTests("test_search_uses_semantic_results_route")
    runner.setUp()
    try:
        yield runner
    finally:
        runner.doCleanups()


def test_direct_runner_uses_config_location_before_reading_cards_or_applying(harness):
    expected = "Berlin, Germany"
    with patch.object(ne, "_wait_for_result_cards", return_value=[]) as cards:
        def ready(page):
            assert harness.location_guard.call_count == 1
            assert harness.location_guard.call_args.args == (page, expected)
            page.goto.assert_called_once()
            return []
        cards.side_effect = ready
        summary, opener, agent = harness.run_session(settings=harness.settings | {"location": expected})
    assert summary["failed"] == 0
    assert summary["search_stats"]["stop_reason"] == "no_results"
    opener.assert_not_called()
    agent.assert_not_called()


def test_direct_runner_stops_before_scanning_preparing_or_applying_if_location_cannot_be_corrected(harness):
    harness.location_guard.return_value = False
    preparer = Mock()
    with patch.object(ne, "_wait_for_result_cards") as cards:
        summary, opener, agent = harness.run_session(settings=harness.settings | {"_intelligent_preparer": preparer})
    assert summary["failed"] == 1
    assert summary["found"] == summary["applied"] == 0
    assert summary["stopped_reason"] == summary["search_stats"]["stop_reason"] == "location_mismatch"
    cards.assert_not_called()
    preparer.prepare.assert_not_called()
    opener.assert_not_called()
    agent.assert_not_called()
    assert not ne._history_path().exists()


@pytest.mark.parametrize("corrected", [True, False])
def test_discovery_uses_the_same_location_guard_and_closes_the_browser(harness, corrected):
    harness.location_guard.return_value = corrected
    search, detail = Mock(), Mock()
    browser = Mock(pages=[search])
    browser.new_page.return_value = detail
    with (patch.object(ne.config, "ensure_dirs"),
          patch.object(ne.config, "CHROME_WORKER_DIR", harness.root / "chrome"),
          patch.object(ne, "sync_playwright") as playwright,
          patch.object(ne, "_wait_for_result_cards", return_value=[]) as cards,
          patch.object(ne, "_extract_external_apply_url") as external):
        playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = browser
        if corrected:
            jobs, stats = ne.search_non_easy_jobs(harness.settings | {"location": "Berlin, Germany"})
            assert jobs == [] and stats["stop_reason"] == "no_results"
            cards.assert_called_once_with(search)
        else:
            with pytest.raises(RuntimeError, match="configured LinkedIn search location"):
                ne.search_non_easy_jobs(harness.settings | {"location": "Berlin, Germany"})
            cards.assert_not_called()
    harness.location_guard.assert_called_once_with(search, "Berlin, Germany")
    external.assert_not_called()
    browser.close.assert_called_once()


@pytest.fixture
def browser_page():
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("Set RUN_BROWSER_TESTS=1 to launch Chromium")
    runner = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    runner.setUp()
    try:
        runner.page.set_content(FIXTURE.read_text())
        yield runner.page
    finally:
        runner.doCleanups()


def test_supplied_nested_location_control_is_repaired_by_the_shared_helper(browser_page):
    assert ne._ensure_configured_linkedin_search_location is easy._ensure_configured_linkedin_search_location
    assert easy._active_linkedin_search_location(browser_page) == "Vienna, Austria"
    assert ne._ensure_configured_linkedin_search_location(browser_page, "Berlin, Germany")
    assert easy._active_linkedin_search_location(browser_page) == "Berlin, Germany"
    assert browser_page.evaluate("window.locationCommitted") == "Berlin, Germany"
    assert browser_page.locator('[componentkey="semanticSearchBox"]').input_value() == "Software"
    assert not browser_page.get_by_label("Filter by Easy Apply", exact=True).is_checked()


def test_matching_location_is_preserved_without_opening_the_popover(browser_page):
    assert ne._ensure_configured_linkedin_search_location(browser_page, "Vienna, Austria")
    assert browser_page.evaluate("window.locationOpens || 0") == 0


def test_direct_runner_corrects_the_real_fixture_before_scanning(browser_page, harness):
    observed = []
    def check_location(_page, target):
        return easy._ensure_configured_linkedin_search_location(browser_page, target)
    def cards(_page):
        observed.append(easy._active_linkedin_search_location(browser_page))
        return []
    harness.location_guard.side_effect = check_location
    with patch.object(ne, "_wait_for_result_cards", side_effect=cards):
        summary, opener, agent = harness.run_session(settings=harness.settings | {"location": "Berlin, Germany"})
    assert observed == ["Berlin, Germany"]
    assert summary["failed"] == 0
    opener.assert_not_called()
    agent.assert_not_called()
