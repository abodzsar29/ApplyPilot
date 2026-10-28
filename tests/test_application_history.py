import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest
import test_intelligent_apply_integration as easy_tests
import test_linkedin_non_easy as non_easy_tests

from applypilot import application_history as ah
from applypilot import config, database


def job(number=1, company="Example Company", **extra):
    return {"url": f"https://www.linkedin.com/jobs/view/{number}/", "company": company,
            "company_url": "",
            "title": "Software Engineer", "application_url": f"https://employer.test/jobs/{number}", **extra}


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "applypilot.db")
    return ah.ApplicationHistory()


def rows(history, table="application_history"):
    with history.connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM " + table)]


@pytest.mark.parametrize("value", [0, 1, 30, 60, "30"])
def test_configurable_cooldown(value):
    assert ah.cooldown_days({"company_cooldown_days": value}) == int(value)
    assert ah.cooldown_days({}) == 30


@pytest.mark.parametrize("value", [None, True, False, -1, 0.5, "bad", [], {}])
def test_invalid_cooldown_is_not_silently_disabled(value):
    with pytest.raises(ValueError, match="company_cooldown_days"):
        ah.cooldown_days({"company_cooldown_days": value})


def test_success_records_job_details_and_blocks_other_source(history, caplog):
    with history.attempt(job(), "linkedin-apply") as attempt:
        assert attempt.allowed
        attempt.success()
    with (caplog.at_level("INFO", logger=ah.__name__),
          history.attempt(job(2, "EXAMPLE  COMPANY!"), "stepstone") as attempt):
        assert not attempt.allowed
        assert attempt.reason == "company_cooldown"
        assert attempt.last_applied_at
    record, = rows(history)
    assert record["job_key"] == "linkedin:1"
    assert record["company_name"] == "Example Company"
    assert record["title"] == "Software Engineer"
    assert record["source"] == "linkedin-apply"
    assert record["job_url"] == job()["url"]
    assert record["application_url"] == job()["application_url"]
    assert "last successful application=" in caplog.text
    assert rows(history, "company_application_reservations") == []


@pytest.mark.parametrize(("age_days", "allowed"), [(29, False), (30, True), (31, True)])
def test_rolling_30_day_window(history, age_days, allowed):
    history.record(job(), "apply", applied_at=int(time.time()) - age_days * 86400)
    with history.attempt(job(2), "stepstone") as attempt:
        assert attempt.allowed is allowed


def test_failure_and_dry_run_do_not_create_history_or_cooldown(history):
    with history.attempt(job(), "apply") as attempt:
        assert attempt.allowed  # Failure: no call to success().
    with history.attempt(job(2), "apply", dry_run=True) as attempt:
        assert attempt.allowed
        attempt.success()
        assert rows(history, "company_application_reservations") == []
    assert rows(history) == []
    with history.attempt(job(3), "apply") as attempt:
        assert attempt.allowed


def test_zero_disables_cooldown_but_preserves_history(history):
    history.record(job(), "apply")
    disabled = ah.ApplicationHistory({"company_cooldown_days": 0})
    with disabled.attempt(job(2), "apply") as attempt:
        assert attempt.allowed
        attempt.success()
    assert len(rows(history)) == 2


def test_exact_names_not_blacklist_substrings(history):
    history.record(job(company="Alpha"), "apply")
    with history.attempt(job(2, "Alpha Technologies"), "stepstone") as attempt:
        assert attempt.allowed


def test_company_url_links_renamed_company_and_name_only_records(history):
    old = job(company="Example Company", company_url="https://www.linkedin.com/company/123/life/")
    history.record(old, "linkedin-apply")
    renamed = job(2, "New Example Name", company_url="https://linkedin.com/company/123/?tracking=abc")
    with history.attempt(renamed, "linkedin-noneasy") as attempt:
        assert not attempt.allowed
    with history.attempt(job(3, "New Example Name"), "stepstone") as attempt:
        assert not attempt.allowed


def test_distinct_known_company_ids_are_not_merged(history):
    history.record(job(company_url="https://linkedin.com/company/123/"), "apply")
    with history.attempt(job(2, company_url="https://linkedin.com/company/456/"), "apply") as attempt:
        assert attempt.allowed


def test_unknown_companies_do_not_all_share_a_key(history):
    history.record(job(company="Unknown company"), "apply")
    with history.attempt(job(2, "Unknown company"), "apply") as attempt:
        assert attempt.allowed


def test_idempotent_records_keep_latest_timestamp(history):
    at = int(time.time())
    history.record(job(), "apply", applied_at=at)
    history.record(job(url=job()["url"] + "?tracking=abc"), "legacy", applied_at=at - 100)
    record, = rows(history)
    assert record["applied_at"] == at
    assert record["source"] == "apply"
    history.record(job(), "stepstone", applied_at=at + 1)
    record, = rows(history)
    assert record["applied_at"] == at + 1
    assert record["source"] == "stepstone"


def test_legacy_jsonl_import_is_idempotent_and_preserves_file(history, tmp_path, caplog):
    path = tmp_path / "linkedin_noneasy_applied.jsonl"
    original = json.dumps({**job(), "linkedin_url": job()["url"], "recorded_at": int(time.time())})
    path.write_text(original + '\ninvalid\n{"company":"Missing date"}\n')
    before = path.read_bytes()
    with caplog.at_level("WARNING", logger=ah.__name__):
        first = ah.ApplicationHistory()
        second = ah.ApplicationHistory()
    assert len(rows(first)) == len(rows(second)) == 1
    assert path.read_bytes() == before
    assert "invalid legacy" in caplog.text
    with first.attempt(job(2), "stepstone") as attempt:
        assert not attempt.allowed


def test_existing_successful_pipeline_jobs_are_imported(history):
    conn = database.init_db(config.DB_PATH)
    now = datetime.now(UTC).isoformat()
    conn.execute("INSERT INTO jobs(url,title,company,apply_status,applied_at) VALUES(?,?,?,?,?)",
                 (job()["url"], "Software Engineer", "Example Company", "applied", now))
    conn.execute("INSERT INTO jobs(url,title,company,apply_status,applied_at) VALUES(?,?,?,?,?)",
                 (job(2)["url"], "Software Engineer", "Not Applied", "failed", now))
    conn.commit()
    try:
        imported = ah.ApplicationHistory()
        assert len(rows(imported)) == 1
        with imported.attempt(job(3), "linkedin-apply") as attempt:
            assert not attempt.allowed
        with imported.attempt(job(4, "Not Applied"), "linkedin-apply") as attempt:
            assert attempt.allowed
    finally:
        database.close_connection(config.DB_PATH)


def test_company_lookup_has_compound_index(history):
    with history.connection() as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(application_history)")}
    assert "application_history_company_time" in indexes


def test_two_workers_cannot_reserve_same_company(history):
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda number: history.attempt(job(number), "parallel"), [1, 2]))
    try:
        assert sum(attempt.allowed for attempt in attempts) == 1
        assert {attempt.reason for attempt in attempts} == {"", "company_in_progress"}
    finally:
        for attempt in attempts:
            attempt.close()
    assert rows(history, "company_application_reservations") == []


def test_expired_reservation_can_be_reclaimed_but_old_owner_cannot_release_new_one(history):
    old = history.attempt(job(), "first")
    with history.connection() as conn:
        conn.execute("UPDATE company_application_reservations SET expires_at=0")
    new = history.attempt(job(2), "second")
    try:
        assert new.allowed
        assert not old.renew()
        old.close()
        assert new.renew()
        assert len(rows(history, "company_application_reservations")) == 1
    finally:
        new.close()


def test_history_errors_fail_closed(history):
    with patch.object(history, "connection", side_effect=sqlite3.OperationalError("unavailable")):
        with pytest.raises(ah.HistoryError):
            history.attempt(job(), "apply")
        with pytest.raises(ah.HistoryError):
            history.record(job(), "apply")


def test_failed_success_record_keeps_short_reservation_for_uncertain_submission(history):
    attempt = history.attempt(job(), "apply")
    with patch.object(history, "record", side_effect=ah.HistoryError("disk full")), pytest.raises(ah.HistoryError):
        attempt.success()
    attempt.close()
    assert rows(history) == []
    assert len(rows(history, "company_application_reservations")) == 1


def test_non_easy_skips_before_resume_generation_and_external_page(history):
    instance = non_easy_tests.NonEasyTests("test_search_uses_semantic_results_route")
    instance.setUp()
    try:
        root_history = ah.ApplicationHistory()
        root_history.record(job(company="Employer"), "linkedin-apply")
        preparer = Mock()
        with patch("applypilot.linkedin.scrape.read_current_job", return_value={
            "title": "Engineer", "company": "Employer", "description": non_easy_tests.ENGLISH,
        }):
            summary, opener, runner = instance.run_session(settings=instance.settings | {
                "company_cooldown_days": 30, "_intelligent_preparer": preparer,
            })
        assert summary["search_stats"]["company_cooldown"] == 1
        preparer.prepare.assert_not_called()
        opener.assert_not_called()
        runner.assert_not_called()
    finally:
        instance.doCleanups()


def test_easy_apply_shares_successes_with_non_easy(history):
    instance = easy_tests.IntelligentRunnerTests("test_easy_uses_each_fresh_cv_before_click_and_overrides_language_mapping")
    instance.setUp()
    try:
        summary, filler, _ = instance.run_easy(descriptions=[easy_tests.DESCRIPTION], config_overrides={"company_cooldown_days": 30})
        assert summary["applied"] == 1
        filler.assert_called_once()
        shared = ah.ApplicationHistory()
        with shared.attempt(job(999, "Employer"), "linkedin-noneasy") as attempt:
            assert not attempt.allowed
        assert rows(shared)[0]["source"] == "linkedin-apply"
    finally:
        instance.doCleanups()


def test_original_worker_uses_shared_history_without_calling_agent(history):
    from applypilot.apply import launcher

    history.record(job(), "stepstone")
    with patch.object(launcher, "_run_job_once") as agent:
        result, _ = launcher.run_job(job(2), 9222)
    assert result == "skipped:company_cooldown"
    agent.assert_not_called()


@pytest.mark.parametrize(("result", "dry_run", "records"), [("applied", False, 1), ("failed:error", False, 0), ("applied", True, 0)])
def test_original_worker_records_only_non_dry_success(history, result, dry_run, records):
    from applypilot.apply import launcher

    with patch.object(launcher, "_run_job_once", return_value=(result, 1)):
        returned, _ = launcher.run_job(job(), 9222, dry_run=dry_run)
    assert len(rows(history)) == records
    if dry_run:
        assert returned == "ready"


def test_stepstone_checks_history_before_opening_application(history):
    from applypilot.stepstone import apply as stepstone

    history.record(job(), "linkedin-noneasy")
    context = Mock()
    with (patch.object(stepstone, "_goto_stepstone_job_page", return_value=True),
          patch.object(stepstone, "_dismiss_cookie_banner"),
          patch.object(stepstone, "_job_title_matches", return_value=True),
          patch.object(stepstone, "page_job_metadata", return_value=job(2)),
          patch.object(stepstone, "_click_application_start_button") as start):
        assert stepstone._apply_to_stepstone_job(context, job(2)["url"], {}, False) == "SKIPPED"
    start.assert_not_called()


@pytest.mark.parametrize(("result", "dry_run", "records"), [("APPLIED", False, 1), ("FAILED", False, 0), ("APPLIED", True, 0)])
def test_stepstone_records_only_success(history, result, dry_run, records):
    from applypilot.stepstone import apply as stepstone

    application = Mock(url="https://employer.test/apply")
    with (patch.object(stepstone, "_goto_stepstone_job_page", return_value=True),
          patch.object(stepstone, "_dismiss_cookie_banner"),
          patch.object(stepstone, "_job_title_matches", return_value=True),
          patch.object(stepstone, "page_job_metadata", return_value=job()),
          patch.object(stepstone, "_click_application_start_button", return_value=application),
          patch.object(stepstone, "_replace_main_cv_if_present"), patch.object(stepstone, "_fill_stepstone_fields"),
          patch.object(stepstone, "_submit_stepstone_application", return_value=result)):
        stepstone._apply_to_stepstone_job(Mock(), job()["url"], {"resume_path": "resume.pdf"}, dry_run)
    assert len(rows(history)) == records
    assert rows(history, "company_application_reservations") == []


def test_generic_urls_deduplicate_tracking_but_keep_job_identity():
    assert ah.job_key({"url": "https://employer.test/job?id=42&utm_source=linkedin"}) == "https://employer.test/job?id=42"
    assert ah.job_key({"url": "https://employer.test/job?id=43"}) != "https://employer.test/job?id=42"


def test_database_forward_migration_preserves_existing_jobs(history):
    with history.connection() as conn:
        conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO jobs VALUES ('https://job.test/1', 'Original title')")
    conn = database.init_db(config.DB_PATH)
    try:
        row = dict(conn.execute("SELECT * FROM jobs").fetchone())
        assert row["title"] == "Original title"
        assert row["company"] is None and row["company_url"] is None
    finally:
        database.close_connection(config.DB_PATH)


@pytest.mark.parametrize("function", ["mark_job", "mark_result"])
def test_manual_and_pipeline_success_updates_shared_history(history, function):
    from applypilot.apply import launcher

    database.init_db(config.DB_PATH)
    conn = database.get_connection(config.DB_PATH)
    conn.execute("INSERT INTO jobs(url,title,company) VALUES(?,?,?)", (job()["url"], "Engineer", "Example Company"))
    conn.commit()
    try:
        with patch.object(launcher, "get_connection", return_value=conn):
            getattr(launcher, function)(job()["url"], "applied")
        assert len(rows(history)) == 1
        with history.attempt(job(2), "stepstone") as attempt:
            assert not attempt.allowed
    finally:
        database.close_connection(config.DB_PATH)


def test_worker_does_not_loop_on_a_company_cooldown_skip(history):
    from applypilot.apply import launcher

    first, second = job(), job(2, "Another Company")
    launcher._stop_event.clear()
    with (patch.object(launcher, "acquire_job", side_effect=[first, second, None]) as acquire,
          patch.object(launcher, "launch_chrome"), patch.object(launcher, "cleanup_worker"),
          patch.object(launcher, "release_lock"), patch.object(launcher, "mark_result") as mark,
          patch.object(launcher, "update_state"), patch.object(launcher, "add_event"),
          patch.object(launcher, "run_job", side_effect=[("skipped:company_cooldown", 0), ("applied", 1)])):
        assert launcher.worker_loop(limit=1) == (1, 0)
        assert first["url"] in acquire.call_args_list[1].kwargs["excluded_urls"]
        mark.assert_called_once_with(second["url"], "applied", duration_ms=1)


@pytest.mark.parametrize("dry_run", [True, False])
def test_simple_apply_never_records_dry_or_unconfirmed_submission(history, dry_run):
    from applypilot.apply import simple_apply

    page = Mock(url=job()["url"])
    page.query_selector.side_effect = lambda selector: None if selector.startswith("text=") else Mock()
    button = Mock()
    button.is_visible.return_value = True
    button.get_attribute.side_effect = lambda attr: "/jobs/view/1/apply/" if attr == "href" else "Easy Apply"
    button.text_content.return_value = "Easy Apply"
    page.query_selector_all.return_value = [button]
    browser = Mock()
    browser.new_page.return_value = page
    with (patch.object(simple_apply, "sync_playwright") as playwright,
          patch.object(simple_apply, "page_job_metadata", return_value=job()),
          patch.object(simple_apply, "_fill_form", return_value=True)):
        playwright.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = browser
        result = simple_apply.apply_to_job(job()["url"], {}, "profile", dry_run=dry_run)
    assert result == ("READY" if dry_run else "FAILED:submission_unconfirmed")
    assert rows(history) == []


def test_reservation_renewal_extends_lease(history):
    with history.attempt(job(), "apply") as attempt:
        previous = rows(history, "company_application_reservations")[0]["expires_at"]
        with patch.object(ah.time, "time", return_value=int(time.time()) + 120):
            assert attempt.renew()
        assert rows(history, "company_application_reservations")[0]["expires_at"] > previous


def test_discovery_keeps_employer_metadata_for_all_database_sources(history):
    # Only exercise storage; the optional network scraper need not be installed.
    with patch.dict(sys.modules, {"jobspy": Mock(scrape_jobs=Mock())}):
        from applypilot.discovery import jobspy
    from applypilot.discovery import smartextract, workday

    conn = database.init_db(config.DB_PATH)
    try:
        database.store_jobs(conn, [job(1)], "Job board", "json_ld")
        smartextract._store_jobs_filtered(conn, [job(2)], "Job board", "json_ld", [], [])
        dataframe = Mock()
        dataframe.iterrows.return_value = [(0, {"job_url": job(3)["url"], "title": "Engineer", "company": "Example Company"})]
        jobspy.store_jobspy_results(conn, dataframe, "LinkedIn")
        workday.store_results(conn, [{"apply_url": job(4)["url"], "title": "Engineer", "employer_name": "Example Company"}], {})
        saved = list(conn.execute("SELECT company FROM jobs"))
        assert len(saved) == 4
        assert all(row[0] == "Example Company" for row in saved)
    finally:
        database.close_connection(config.DB_PATH)


def test_json_ld_discovery_retains_hiring_organization():
    from applypilot.discovery.smartextract import execute_json_ld

    found, = execute_json_ld({"json_ld": [{
        "@type": "JobPosting", "title": "Engineer", "url": job()["url"],
        "hiringOrganization": {"name": "Example Company", "sameAs": "https://example.test"},
    }]}, {"extraction": {"title": "title", "url": "url"}})
    assert found["company"] == "Example Company"
    assert found["company_url"] == "https://example.test"


def test_detail_enrichment_reads_company_without_additional_llm_calls():
    from applypilot.enrichment import detail

    page = Mock()
    page.goto.return_value.status = 200
    with (patch.object(ah, "page_job_metadata", return_value=job()),
          patch.object(detail, "collect_detail_intelligence", return_value={}),
          patch.object(detail, "extract_from_json_ld", return_value={"full_description": "Description", "application_url": "https://example.test/apply"}),
          patch.object(detail, "extract_with_llm") as llm):
        result = detail.scrape_detail_page(page, job()["url"])
    assert result["company"] == "Example Company"
    llm.assert_not_called()


@pytest.mark.parametrize("stage", ["tailor", "cover"])
def test_document_generation_skips_cooldown_before_model_request(history, tmp_path, stage):
    from applypilot.scoring import cover_letter, tailor

    history.record(job(), "apply")
    module = tailor if stage == "tailor" else cover_letter
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [job(2)]
    with (patch.object(module, "load_profile", return_value={}),
          patch.object(module, "RESUME_PATH") as resume,
          patch.object(module, "get_connection", return_value=conn),
          patch.object(module, "get_jobs_by_stage", return_value=[job(2)]),
          patch.object(module, "TAILORED_DIR" if stage == "tailor" else "COVER_LETTER_DIR", tmp_path / "documents"),
          patch.object(module, "tailor_resume" if stage == "tailor" else "generate_cover_letter") as generate):
        resume.read_text.return_value = "Original CV"
        if stage == "tailor":
            module.run_tailoring()
        else:
            module.run_cover_letters()
    generate.assert_not_called()


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Set RUN_BROWSER_TESTS=1")
@pytest.mark.parametrize("html", [
    '<script type="application/ld+json">{"@graph":[{"@type":"JobPosting","title":"Engineer","hiringOrganization":{"name":"Example Company","sameAs":"https://employer.test"}}]}</script>',
    '<h1>Engineer</h1><a class="topcard__org-name-link" href="https://employer.test">Example Company</a>',
    '<h1>Engineer</h1><span data-at="header-company-name">Example Company</span>',
])
def test_browser_reads_employer_metadata_from_supported_markup(html):
    browser = non_easy_tests.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    browser.setUp()
    try:
        browser.page.set_content(html)
        metadata = ah.page_job_metadata(browser.page, job()["url"])
        assert metadata["company"] == "Example Company"
        assert metadata["title"] == "Engineer"
    finally:
        browser.doCleanups()
