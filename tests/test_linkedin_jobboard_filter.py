"""File parsing, hostname matching, and non-Easy-Apply jobboard handoff tests."""

import json
import os
from unittest.mock import Mock, patch

import pytest
import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import non_easy as ne
from applypilot.linkedin.jobboards import jobboard_filter_enabled, load_jobboard_filter

ALLOWLIST = {
    "ashby": ["jobs.ashbyhq.com"],
    "greenhouse": ["job-boards.greenhouse.io", "boards.greenhouse.io"],
    "join": ["join.com"],
    "workable": ["*.workable.com"],
    "personio": ["*.jobs.personio.de", "*-jobs.personio.de"],
}
ALLOWED_URL = "https://jobs.ashbyhq.com/company/engineer"
OTHER_URL = "https://employer.test/apply"


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "jobboards.json").write_text(json.dumps(ALLOWLIST))
    return {"jobboard_filter": True, "_project_root": str(tmp_path)}


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", True),
                                          ("false", False), (" TRUE ", True), ("False", False)])
def test_boolean_values(value, expected):
    assert jobboard_filter_enabled({"jobboard_filter": value}) is expected


@pytest.mark.parametrize("value", [None, 0, 1, "true,", "yes", "", [], {}])
def test_invalid_flag_does_not_silently_disable(value):
    with pytest.raises(ValueError, match="jobboard_filter"):
        jobboard_filter_enabled({"jobboard_filter": value})


@pytest.mark.parametrize("flag", [{}, {"jobboard_filter": False}, {"jobboard_filter": "false"}])
def test_disabled_never_reads_allowlist(flag):
    with patch("pathlib.Path.read_text", side_effect=AssertionError("Disabled filter must not read the file")):
        assert load_jobboard_filter(flag) is None


@pytest.mark.parametrize("url", [
    ALLOWED_URL, "https://JOBS.ASHBYHQ.COM./company", "http://jobs.ashbyhq.com:8080/company",
    "https://job-boards.greenhouse.io/company", "https://boards.greenhouse.io/company/123",
    "https://join.com/companies/company/123", "https://acme.workable.com/jobs/",
    "https://acme.jobs.personio.de/job/123", "https://acme-jobs.personio.de/job/123",
])
def test_allowed_hosts_and_patterns(settings, url):
    assert load_jobboard_filter(settings).matches(url)


@pytest.mark.parametrize("url", [
    OTHER_URL, "https://evil.test/?next=https://jobs.ashbyhq.com/company",
    "https://evil.test/jobs.ashbyhq.com/company", "https://jobs.ashbyhq.com.evil.test/company",
    "https://notjobs.ashbyhq.com/company", "https://jobs.ashbyhq.com@evil.test/company",
    "https://evil.test@jobs.ashbyhq.com/company", "https://jobs.ashbyhq.com:invalid/company",
    "https://jobs.ashbyhq.com:99999/company", "https://jobs.ashbyhq.com\\@evil.test/company",
    "https://jobs.ashbyhq.com/white space", "https://jobs.ashbyhq.com%2eevil.test/company",
    "https://workable.com/jobs", "https://one.two.workable.com/jobs", "https://jobs.personio.de/",
    "https://x.acme.jobs.personio.de/job/123", "https://-jobs.personio.de/job/123",
    "https://www.join.com/company", "ftp://jobs.ashbyhq.com/company", "javascript:alert(1)",
    "//jobs.ashbyhq.com/company", "", "https://[broken", None,
])
def test_unlisted_malicious_or_wrong_wildcard_hosts_do_not_match(settings, url):
    assert not load_jobboard_filter(settings).matches(url)


def test_default_path_is_root_not_empty_config_copy(settings, tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "jobboards.json").write_text("")
    monkeypatch.chdir(tmp_path)
    actual = load_jobboard_filter({"jobboard_filter": True})
    assert actual.path == tmp_path / "jobboards.json"
    assert actual.matches(ALLOWED_URL)


@pytest.mark.parametrize("data", ["", "not JSON", "[]", "{}", '{"ashby": []}',
                                  '{"ashby": "jobs.ashbyhq.com"}', '{"ashby": [1]}',
                                  '{"ashby": ["https://jobs.ashbyhq.com/path"]}',
                                  '{"ashby": ["*"]}', '{"ashby": ["**.ashbyhq.com"]}',
                                  '{"ashby": ["jobs.ashbyhq.com:443"]}', '{"ashby": [""]}'])
def test_invalid_or_empty_allowlist_fails_closed(settings, tmp_path, data):
    (tmp_path / "jobboards.json").write_text(data)
    with pytest.raises(ValueError, match="jobboards.json"):
        load_jobboard_filter(settings)


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="jobboards.json"):
        load_jobboard_filter({"jobboard_filter": True, "_project_root": str(tmp_path)})


def test_unreadable_file_fails_closed(settings):
    with (patch("pathlib.Path.read_text", side_effect=PermissionError("denied")),
          pytest.raises(ValueError, match="denied")):
        load_jobboard_filter(settings)


def test_patterns_are_case_insensitive_and_deduplicated(settings, tmp_path):
    (tmp_path / "jobboards.json").write_text(json.dumps({"ashby": [" JOBS.ASHBYHQ.COM. ", "jobs.ashbyhq.com"]}))
    assert load_jobboard_filter(settings).patterns == ("jobs.ashbyhq.com",)


@pytest.fixture
def harness():
    instance = existing_non_easy.NonEasyTests("test_only_english_rejects_german_and_unknown")
    instance.setUp()
    instance.settings.update(jobboard_filter=True, _project_root=str(instance.root))
    (instance.root / "jobboards.json").write_text(json.dumps(ALLOWLIST))
    try:
        with patch("applypilot.linkedin.scrape.read_current_job", return_value={
            "title": "Engineer", "company": "Employer", "description": existing_non_easy.ENGLISH,
        }):
            yield instance
    finally:
        instance.doCleanups()


@pytest.mark.parametrize("dry_run", [False, True])
def test_allowed_destination_reaches_runner_and_retains_history_policy(harness, dry_run):
    summary, _, runner = harness.run_session(
        application_urls=[ALLOWED_URL], result="ready" if dry_run else "applied", dry_run=dry_run,
    )
    assert summary["ready" if dry_run else "applied"] == 1
    assert summary["search_stats"]["jobboard_filtered"] == 0
    assert runner.call_args.kwargs["job"]["application_url"] == ALLOWED_URL
    assert runner.call_args.kwargs["config_dict"]["_jobboard_filter"].matches(ALLOWED_URL)
    assert ne._history_path().exists() is not dry_run


@pytest.mark.parametrize("same_tab", [False, True])
def test_actual_landed_url_is_checked_instead_of_initial_url(harness, same_tab):
    summary, opener, runner = harness.run_session(
        application_urls=[ALLOWED_URL], landed_urls=[OTHER_URL], same_tab=same_tab,
    )
    assert summary["skipped"] == 1
    assert summary["search_stats"]["jobboard_filtered"] == 1
    assert summary["failed"] == summary["found"] == summary["applied"] == 0
    assert summary["jobs"] == []
    opener.assert_called_once()
    runner.assert_not_called()
    assert not ne._history_path().exists()
    if not same_tab:
        harness.application.close.assert_called()


def test_filtered_job_does_not_consume_application_limit(harness):
    settings = harness.settings | {"max_applications": 1}
    summary, opener, runner = harness.run_session(
        jobs=2, settings=settings, application_urls=[OTHER_URL, ALLOWED_URL],
    )
    assert summary["found"] == summary["applied"] == 1
    assert summary["skipped"] == summary["search_stats"]["jobboard_filtered"] == 1
    assert opener.call_count == 2
    runner.assert_called_once()
    assert len(ne._load_applied_job_keys()) == 1


def test_false_string_allows_unlisted_site_without_allowlist(harness):
    settings = harness.settings | {"jobboard_filter": "false", "_project_root": str(harness.root / "missing")}
    summary, _, runner = harness.run_session(settings=settings, application_urls=[OTHER_URL])
    assert summary["applied"] == 1
    assert summary["search_stats"]["jobboard_filtered"] == 0
    runner.assert_called_once()


def test_missing_enabled_file_stops_before_browser_launch(harness):
    settings = harness.settings | {"_project_root": str(harness.root / "missing")}
    with patch.object(ne, "launch_chrome") as launch, pytest.raises(ValueError, match="jobboards.json"):
        ne.run_non_easy_apply(settings)
    launch.assert_not_called()


def test_unlisted_destination_skips_intelligent_preparation(harness):
    preparer = Mock()
    settings = harness.settings | {"_intelligent_preparer": preparer}
    summary, _, runner = harness.run_session(settings=settings, application_urls=[OTHER_URL])
    assert summary["search_stats"]["jobboard_filtered"] == 1
    preparer.prepare.assert_not_called()
    runner.assert_not_called()


def test_allowed_destination_is_rechecked_after_resume_preparation(harness):
    preparer = Mock()
    generated = Mock(published_pdf=harness.root / "english.pdf", text="Fresh CV", pdf_sha256="hash")

    def prepare(job):
        harness.application.url = OTHER_URL
        return generated

    preparer.prepare.side_effect = prepare
    summary, _, runner = harness.run_session(
        settings=harness.settings | {"_intelligent_preparer": preparer}, application_urls=[ALLOWED_URL],
    )
    assert summary["skipped"] == summary["search_stats"]["jobboard_filtered"] == 1
    generated.finish.assert_called_once_with("skipped")
    generated.verify_published.assert_called_once()
    runner.assert_not_called()
    assert not ne._history_path().exists()


def test_agent_reported_unlisted_redirect_counts_as_skip_and_keeps_searching(harness):
    summary, _, runner = harness.run_session(
        result="failed:jobboard_filtered", jobs=2, settings=harness.settings | {"max_applications": 1},
        application_urls=[ALLOWED_URL, ALLOWED_URL],
    )
    assert summary["skipped"] == summary["search_stats"]["jobboard_filtered"] == 2
    assert summary["found"] == summary["failed"] == summary["applied"] == 0
    assert runner.call_count == 2
    assert not ne._history_path().exists()


@pytest.mark.parametrize("provider", ["openrouter", "claude"])
def test_direct_agent_runner_cannot_bypass_filter(harness, provider):
    agent = Mock()
    with patch.object(ne.subprocess, "Popen") as process:
        result = ne._run_external_application(provider, "model", agent, harness.job, harness.settings, 9222, False)
    assert result == ("failed:jobboard_filtered", 0)
    process.assert_not_called()
    agent.run_prompt.assert_not_called()


def test_browser_prompt_includes_redirect_rule_and_mailbox_exception(harness):
    prompt = ne._build_prompt(harness.job | {"application_url": ALLOWED_URL}, harness.settings)
    assert "== JOB-BOARD FILTER ==" in prompt
    assert "*.jobs.personio.de" in prompt
    assert "RESULT:FAILED:jobboard_filtered" in prompt
    assert "again before entering personal information, uploading, or submitting" in prompt
    assert "mailbox may still be used only for the EMAIL VERIFICATION process" in prompt
    assert "== JOB-BOARD FILTER ==" not in ne._build_prompt(
        harness.job, harness.settings | {"jobboard_filter": False},
    )


def test_cli_displays_enabled_filter_and_rejects_bad_file_before_provider(harness):
    from typer.testing import CliRunner

    from applypilot.cli import app

    path = harness.root / "config.json"
    path.write_text(json.dumps(harness.settings))
    result_summary = {"found": 0, "applied": 0, "ready": 0, "skipped": 1, "failed": 0,
                      "search_stats": {"jobboard_filtered": 1}}
    with (patch("applypilot.cli._bootstrap"),
          patch("applypilot.cli._prepare_intelligent_settings"),
          patch("applypilot.config.check_non_easy_provider_requirements") as requirements,
          patch("applypilot.qwen_mcp.get_effective_model_and_provider", return_value=("model", "openrouter")),
          patch.object(ne, "run_non_easy_apply", return_value=result_summary) as runner):
        result = CliRunner().invoke(app, ["linkedin-noneasy", "--config", str(path), "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Jobboard filter: enabled" in result.output
        assert "Jobboard filtered:   1" in result.output
        assert "jobboards.json" in result.output
        runner.reset_mock()
        requirements.reset_mock()
        (harness.root / "jobboards.json").write_text("invalid")
        result = CliRunner().invoke(app, ["linkedin-noneasy", "--config", str(path)])
        assert result.exit_code == 1
        assert "jobboards.json" in result.output
        runner.assert_not_called()
        requirements.assert_not_called()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Set RUN_BROWSER_TESTS=1 for local Chromium")
@pytest.mark.parametrize("target", ["_blank", "_self"])
@pytest.mark.parametrize("landed_url,expected", [(ALLOWED_URL, True), (OTHER_URL, False)])
def test_open_external_page_checks_redirected_popup_and_same_tab(settings, target, landed_url, expected):
    harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    harness.setUp()
    try:
        def redirect(route):
            route.fulfill(status=302, headers={"location": landed_url})

        harness.context.route("https://redirect.test/*", redirect)
        harness.page.set_content(
            f'<a aria-label="Apply on company website" href="https://redirect.test/job" target="{target}">Apply</a>',
        )
        page, url = ne._open_external_application_page(harness.page)
        assert page is not None
        assert page.url == url == landed_url
        assert load_jobboard_filter(settings).matches(page.url) is expected
    finally:
        harness.doCleanups()
