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


def job_details(company):
    return {"company": company, "title": "Engineer", "description": existing_non_easy.ENGLISH}


@pytest.mark.parametrize(("company", "blacklist"), [
    ("Crossing Hurdles", ["Crossing Hurdles"]),
    ("  cROSSING   hURDLES GmbH ", ["Crossing Hurdles"]),
    ("NOMOS GLASHUTTE", ["NOMOS Glashütte"]),
    ("Digital-Waffle GmbH", ["digital waffle"]),
    ("Avaloq", ["Avaloq Group AG"]),
])
def test_blacklisted_company_skips_before_external_handoff(harness, company, blacklist, caplog):
    with (patch("applypilot.linkedin.scrape.parse_job", return_value=job_details(company)),
          caplog.at_level("INFO", logger=ne.__name__)):
        summary, opener, runner = harness.run_session(
            "ready", settings=harness.settings | {"company_blacklist": blacklist}, dry_run=True,
        )
    assert summary["skipped"] == summary["search_stats"]["company_blacklisted"] == 1
    assert summary["found"] == summary["ready"] == summary["applied"] == summary["failed"] == 0
    assert summary["jobs"] == []
    assert "Skipping blacklisted company" in caplog.text
    opener.assert_not_called()
    runner.assert_not_called()
    assert not ne._history_path().exists()


def test_card_company_can_skip_before_parsing(harness):
    settings = harness.settings | {"company_blacklist": ["Crossing Hurdles"]}
    with (patch.object(ne, "_non_easy_company_name", return_value="Crossing Hurdles GmbH"),
          patch("applypilot.linkedin.scrape.parse_job") as parser):
        summary, opener, runner = harness.run_session(settings=settings)
    assert summary["search_stats"]["company_blacklisted"] == 1
    parser.assert_not_called()
    opener.assert_not_called()
    runner.assert_not_called()


@pytest.mark.parametrize("jobboard_filter", [False, True])
def test_blacklisted_company_skips_intelligent_preparation(harness, jobboard_filter):
    preparer = Mock()
    board_filter = Mock(patterns=("jobs.example.test",), path=harness.root / "jobboards.json")
    settings = harness.settings | {
        "company_blacklist": ["Employer"], "_intelligent_preparer": preparer,
    }
    with (patch("applypilot.linkedin.scrape.read_current_job", return_value=job_details("Employer")),
          patch.object(ne, "load_jobboard_filter", return_value=board_filter if jobboard_filter else None),
          patch.object(ne, "prefill_greenhouse_application") as prefill):
        summary, opener, runner = harness.run_session(settings=settings)
    assert summary["search_stats"]["company_blacklisted"] == 1
    assert summary["failed"] == summary["applied"] == 0
    preparer.prepare.assert_not_called()
    prefill.assert_not_called()
    opener.assert_not_called()
    runner.assert_not_called()
    assert not ne._history_path().exists()


def test_blacklisted_job_does_not_consume_application_limit(harness):
    settings = harness.settings | {"company_blacklist": ["Crossing Hurdles"], "max_applications": 1}
    with patch("applypilot.linkedin.scrape.parse_job", side_effect=[
        job_details("Crossing Hurdles"), job_details("Unlisted Employer"),
    ]):
        summary, opener, runner = harness.run_session(settings=settings, jobs=2)
    assert summary["skipped"] == summary["search_stats"]["company_blacklisted"] == 1
    assert summary["applied"] == summary["found"] == 1
    assert summary["search_stats"]["candidate_urls"] == 2
    assert summary["jobs"][0]["company"] == "Unlisted Employer"
    opener.assert_called_once()
    runner.assert_called_once()
    assert len(ne._load_applied_job_keys()) == 1


@pytest.mark.parametrize("value", [None, [], ["", "  ", "--"], ["Completely Different"]])
def test_empty_or_nonmatching_blacklist_keeps_job_eligible(harness, value):
    with patch("applypilot.linkedin.scrape.parse_job", return_value=job_details("Employer")):
        summary, opener, runner = harness.run_session(
            "ready", settings=harness.settings | {"company_blacklist": value}, dry_run=True,
        )
    assert summary["ready"] == 1
    assert summary["skipped"] == summary["search_stats"]["company_blacklisted"] == 0
    opener.assert_called_once()
    runner.assert_called_once()


def test_blacklist_does_not_match_title_or_description(harness):
    details = job_details("Allowed Employer") | {
        "title": "Crossing Hurdles Engineer",
        "description": existing_non_easy.ENGLISH + " Worked with Crossing Hurdles.",
    }
    with patch("applypilot.linkedin.scrape.parse_job", return_value=details):
        summary, _, runner = harness.run_session(
            "ready", settings=harness.settings | {"company_blacklist": ["Crossing Hurdles"]}, dry_run=True,
        )
    assert summary["ready"] == 1
    assert summary["search_stats"]["company_blacklisted"] == 0
    runner.assert_called_once()


def test_blacklist_normalization_deduplicates_and_preserves_config():
    settings = {"company_blacklist": ["  Digital-Waffle ", "digital waffle", "", "NOMOS Glashütte"]}
    original = list(settings["company_blacklist"])
    assert ne._configured_company_blacklist(settings) == ("digital waffle", "nomos glashutte")
    assert settings["company_blacklist"] == original
    assert ne._configured_company_blacklist({}) == ()


@pytest.mark.parametrize("value", ["Employer", True, 1, {}, ["Employer", None], [1]])
def test_invalid_blacklist_stops_before_browser_or_model_calls(harness, value):
    settings = harness.settings | {"company_blacklist": value}
    with (patch.object(ne, "launch_chrome") as launch,
          patch.object(ne, "get_effective_model_and_provider") as model):
        with pytest.raises(ValueError, match="company_blacklist"):
            ne.validate_non_easy_config(settings)
        with pytest.raises(ValueError, match="company_blacklist"):
            ne.run_non_easy_apply(settings)
    launch.assert_not_called()
    model.assert_not_called()


def run_discovery(harness, companies, *, blacklist, parsed_company=None):
    search, detail = Mock(), Mock()
    search.query_selector.return_value = None
    browser = Mock(pages=[search])
    browser.new_page.return_value = detail
    cards = []
    for index in range(len(companies)):
        card = Mock()
        card.get_attribute.return_value = f"https://www.linkedin.com/jobs/view/{100 + index}/"
        card.text_content.return_value = "Engineer"
        cards.append(card)
    with (patch.object(ne.config, "ensure_dirs"),
          patch.object(ne.config, "CHROME_WORKER_DIR", harness.root / "chrome"),
          patch.object(ne, "sync_playwright") as playwright,
          patch.object(ne, "_iter_result_cards", return_value=cards),
          patch.object(ne, "_is_easy_apply", return_value=False),
          patch.object(ne, "_non_easy_company_name", side_effect=companies),
          patch("applypilot.linkedin.scrape.parse_job", return_value=job_details(parsed_company)),
          patch.object(ne, "_extract_external_apply_url", return_value=existing_non_easy.EXTERNAL_URL) as extractor):
        playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = browser
        jobs, stats = ne.search_non_easy_jobs(harness.settings | {
            "company_blacklist": blacklist, "max_applications": 1,
        })
    browser.close.assert_called_once()
    return jobs, stats, extractor


def test_discovery_skips_blacklist_before_external_url_extraction(harness):
    jobs, stats, extractor = run_discovery(
        harness, ["CROSSING HURDLES GmbH", "Unlisted Employer"], blacklist=["Crossing Hurdles"],
    )
    assert stats["company_blacklisted"] == 1
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Unlisted Employer"
    extractor.assert_called_once()


def test_discovery_checks_parsed_sdui_company_if_legacy_selectors_are_missing(harness):
    jobs, stats, extractor = run_discovery(
        harness, [""], blacklist=["PharmaHera"], parsed_company="PharmaHera Service GmbH",
    )
    assert stats["company_blacklisted"] == 1
    assert jobs == []
    extractor.assert_not_called()


def test_cli_reports_company_blacklist_skip_count(harness):
    from typer.testing import CliRunner

    from applypilot.cli import app

    path = harness.root / "config.json"
    path.write_text(json.dumps(harness.settings | {"company_blacklist": ["Crossing Hurdles"]}))
    with (patch("applypilot.cli._bootstrap"), patch("applypilot.cli._prepare_intelligent_settings"),
          patch("applypilot.config.check_non_easy_provider_requirements"),
          patch("applypilot.qwen_mcp.get_effective_model_and_provider", return_value=("model", "openrouter")),
          patch.object(ne, "run_non_easy_apply", return_value={
              "found": 0, "applied": 0, "ready": 0, "skipped": 2, "failed": 0,
              "search_stats": {"company_blacklisted": 2},
          })):
        result = CliRunner().invoke(app, ["linkedin-noneasy", "--config", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Company blacklisted: 2" in result.output


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Set RUN_BROWSER_TESTS=1")
@pytest.mark.parametrize("html", [
    '<main><div aria-label="Company, Crossing Hurdles."></div></main>',
    '<main><a class="topcard__org-name-link">Crossing Hurdles</a></main>',
    '<main><div class="job-details-jobs-unified-top-card__company-name"><a>Crossing Hurdles</a></div></main>',
])
def test_company_extraction_supports_modern_and_legacy_markup(html):
    browser = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    browser.setUp()
    try:
        browser.page.set_content(html)
        assert ne._non_easy_company_name(browser.page) == "Crossing Hurdles"
    finally:
        browser.doCleanups()
