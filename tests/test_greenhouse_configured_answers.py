"""Greenhouse receives all configured facts and commits real dropdown options."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import non_easy as ne
from applypilot.linkedin.greenhouse import configured_answer, prefill_greenhouse_application

URL = "https://job-boards.greenhouse.io/example/jobs/123"
FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_configured_questions.html"
PROFILE = {"first_name": "Test", "last_name": "Applicant", "email": "test@example.test",
           "phone_country": "United Kingdom", "phone_country_code": "+44", "phone_number": "7700900123",
           "city": "Berlin, Germany"}
ANSWERS = {
    "authorized_to_work": True, "visa_sponsorship": False,
    "Are you 18 years of age or older?": True,
    "Job Applicant Data Privacy Notice": "I agree",
    "Are you currently employed by a client, partner, or competitor of Addepar?": False,
    "years_experience": {"Python": 0},
    "What is your current notice period?": "Two weeks",
    "Native configured option": False,
}


@pytest.mark.parametrize("question,value,source", [
    ("First Name*", "Test", "profile.first_name"),
    ("Location (City)*", "Berlin, Germany", "profile.city"),
    ("Phone*", "7700900123", "profile.phone_number"),
    ("Are you authorized to work in the United Kingdom without visa sponsorship?*", "Yes",
     "answers.authorized_to_work + answers.visa_sponsorship"),
    ("Do you require visa sponsorship?", "No", "answers.visa_sponsorship"),
    ("ARE YOU 18 YEARS OF AGE OR OLDER? *", "Yes", "answers.Are you 18 years of age or older?"),
    ("Job Applicant Data Privacy Notice*", "I agree", "answers.Job Applicant Data Privacy Notice"),
    ("How many years of Python experience?", "0", "answers.years_experience"),
    ("What is your current notice period?*", "Two weeks", "answers.What is your current notice period?"),
])
def test_resolves_full_question_keys_and_aliases(question, value, source):
    answer = configured_answer(question, PROFILE, ANSWERS)
    assert answer.value == value
    assert answer.source == source


@pytest.mark.parametrize("permitted,sponsorship,expected", [
    (True, False, "Yes"), (False, False, "No"), (True, True, "No"), (False, True, "No"),
    ("Yes", "No", "Yes"), ("No", "No", "No"),
])
def test_authorization_without_sponsorship_uses_both_configured_facts(permitted, sponsorship, expected):
    answer = configured_answer("Are you authorized to work without visa sponsorship?", {}, {
        "authorized_to_work": permitted, "visa_sponsorship": sponsorship,
    })
    assert answer.value == expected


def test_explicit_question_and_screening_override_take_precedence():
    question = "Are you authorized to work without visa sponsorship?"
    answers = ANSWERS | {question: False}
    assert configured_answer(question, PROFILE, answers).value == "No"
    answers["screening_overrides"] = {"authorized to work without": True}
    assert configured_answer(question, PROFILE, answers).value == "Yes"


@pytest.mark.parametrize("question", ["Are you 18 years of age or older?", "Job Applicant Data Privacy Notice",
                                      "Are you employed by a client, partner, or competitor?"])
def test_does_not_fabricate_missing_facts_or_consent(question):
    assert configured_answer(question, PROFILE, {}) is None


def test_combined_authorization_without_all_facts_is_unresolved():
    assert configured_answer("Are you authorized to work without visa sponsorship?", {}, {
        "authorized_to_work": "Yes",
    }) is None


def test_configured_language_and_experience_are_resolved_before_favorable_defaults():
    answer = configured_answer("What is your proficiency level in German?", {}, {"german_level": "B1"})
    assert answer.value == "B1"
    assert answer.source == "answers.german_level"
    assert configured_answer("Are you permitted to work in Germany?", {}, {"german_level": "B1"}) is None
    assert configured_answer("Do you have Python experience?", {}, {"years_experience": {"Python": 0}}).value == "No"
    assert configured_answer("Do you have Python experience?", {}, {"years_experience": {"Python": 2}}).value == "Yes"
    assert configured_answer("Do you have at least 3 years of Python experience?", {}, {
        "years_experience": {"Python": 2},
    }).value == "No"


def test_prompt_preserves_all_profile_and_answer_data_including_false_zero_and_lists(tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF fixture")
    profile = PROFILE | {"preferred_name": "Preferred", "custom_location_fact": {"country": "Germany"}}
    answers = ANSWERS | {"Languages*": ["English", "German"], "Custom exact question?": "Configured response"}
    settings = {"resume_path": str(resume), "profile": profile, "answers": answers}
    job = {"linkedin_url": existing_non_easy.JOB_URL, "application_url": URL}
    prompt = ne._build_prompt(job, settings)
    profile_text = prompt.split("== APPLICANT PROFILE ==\n")[1].split("== STANDARD ANSWERS ==")[0].strip()
    answers_text = prompt.split("== STANDARD ANSWERS ==\n")[1].split("== SCREENING OVERRIDES ==")[0].strip()
    assert json.loads(profile_text) == profile
    assert json.loads(answers_text) == answers
    assert "Use browser_select_option only on native <select>" in prompt
    assert "CLICK the matching visible option" in prompt
    assert "aria-labelledby also" in prompt
    assert "search input normally becomes empty AFTER a successful choice" in prompt
    assert "NOT the job" in prompt
    assert "not asking whether" in prompt


def test_prefill_report_reaches_agent_context_and_no_model_sees_full_settings_secrets():
    harness = existing_non_easy.NonEasyTests("test_only_english_rejects_german_and_unknown")
    harness.setUp()
    try:
        report = [{"field_id": "question_auth", "question": "Authorization?", "source": "answers", "status": "filled"}]
        settings = harness.settings | {"private_api_key": "NOT-APPLICANT-CONTEXT"}
        with patch.object(ne, "prefill_greenhouse_application", return_value=report) as fill:
            _, _, runner = harness.run_session(settings=settings)
        fill.assert_called_once()
        actual = runner.call_args.kwargs["config_dict"]
        assert actual["_configured_prefill"] == report
        prompt = ne._build_prompt(harness.job, actual)
        assert "question_auth" in prompt
        assert "NOT-APPLICANT-CONTEXT" not in prompt
    finally:
        harness.doCleanups()


@pytest.fixture
def browser():
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("Set RUN_BROWSER_TESTS=1 for local Chromium fixtures")
    harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    harness.setUp()
    try:
        harness.page.goto(URL)
        harness.page.set_content(FIXTURE.read_text())
        yield harness
    finally:
        harness.doCleanups()


def test_prefill_commits_portalled_async_choices_from_config_and_never_submits(browser):
    page = browser.page
    settings = {"profile": PROFILE, "answers": ANSWERS}
    report = prefill_greenhouse_application(page, settings)
    assert page.evaluate("window.selections") == {
        "country": "United Kingdom +44", "candidate-location": "Berlin, Berlin, Germany",
        "question_auth": "Yes", "question_age": "Yes", "question_privacy": "I agree", "question_competitor": "No",
    }
    for field in ("first_name", "last_name", "email"):
        assert page.locator(f"#{field}").input_value() == PROFILE[field]
    assert page.locator("#phone").input_value() == PROFILE["phone_number"]
    assert page.locator("#years").input_value() == "0"
    assert page.locator("#native").input_value() == "no"
    assert page.locator("#notice").input_value() == "Two weeks"
    assert page.locator("#candidate-location").input_value() == ""  # Search cleared after real selection.
    assert page.locator("#question_auth").get_attribute("aria-invalid") == "false"
    assert page.locator("#unknown").input_value() == "a"
    assert page.locator("#mirror-unrelated").input_value() == ""
    assert page.locator("#resume").evaluate("el => el.files.length") == 0
    assert page.evaluate("window.submitCount") == page.evaluate("window.mirrorWrites") == 0
    by_id = {item["field_id"]: item for item in report}
    assert by_id["question_auth"]["status"] == "filled"
    assert by_id["years"]["status"] == "filled"
    assert by_id["unknown"]["status"] == "filled"
    assert by_id["unknown"]["source"] == "fallback.text"
    assert "mirror-unrelated" not in by_id and "phone-search" not in by_id
    assert page.evaluate("window.decoyClicks") == 0

    before = page.evaluate("window.optionClicks")
    prefill_greenhouse_application(page, settings)
    assert page.evaluate("window.optionClicks") == before  # Preserve committed choices, including blank search values.


def test_prefill_does_not_claim_failed_selection_succeeded(browser):
    page = browser.page
    page.evaluate("window.unresponsive = true; window.emptySuggestions = true")
    report = prefill_greenhouse_application(page, {"profile": PROFILE, "answers": ANSWERS})
    by_id = {item["field_id"]: item for item in report}
    assert by_id["question_auth"]["status"] == "retry_with_agent"
    assert by_id["candidate-location"]["status"] == "retry_with_agent"
    assert "question_auth" not in page.evaluate("window.selections")
    assert page.evaluate("window.submitCount") == 0


def test_missing_answers_use_user_requested_positive_negative_and_random_policy(browser):
    report = prefill_greenhouse_application(browser.page, {"profile": PROFILE, "answers": {}})
    selections = browser.page.evaluate("window.selections")
    assert selections["question_auth"] == selections["question_age"] == "Yes"
    assert selections["question_competitor"] == "No"
    assert selections["question_privacy"] == "I agree"
    for item in report:
        if item["field_id"] in {"question_auth", "question_age", "question_privacy", "question_competitor"}:
            assert item["status"] == "filled"
            assert item["source"].startswith("fallback.")
    assert browser.page.locator("#unknown").input_value() == "a"
    assert browser.page.locator("#years").input_value() == "1"


def test_unseen_dropdowns_get_negative_or_positive_outcomes_then_real_random_selection(browser):
    page = browser.page
    page.evaluate("""() => {
        window.addTestDropdown('visa', 'Will you need visa sponsorship?', ['Yes', 'No']);
        window.addTestDropdown('convictions', 'Do you have any unspent convictions?', ['Yes', 'No']);
        window.addTestDropdown('skills', 'Do you have the required skills and experience?', ['No', 'Ja']);
        window.addTestDropdown('source', 'How did you hear about us?', ['LinkedIn', 'Other']);
    }""")
    with patch("applypilot.linkedin.non_easy_questions.random.choice", side_effect=lambda options: options[-1]):
        report = prefill_greenhouse_application(page, {"profile": PROFILE, "answers": ANSWERS})
        selections = page.evaluate("window.selections")
        assert selections["visa"] == selections["convictions"] == "No"
        assert selections["skills"] == "Ja"
        assert selections["source"] == "Other"
        assert {item["field_id"]: item for item in report}["source"]["source"] == "fallback.random"
        clicks = page.evaluate("window.optionClicks")
        prefill_greenhouse_application(page, {"profile": PROFILE, "answers": ANSWERS})
        assert page.evaluate("window.optionClicks") == clicks
    assert page.evaluate("window.submitCount") == 0


def test_explicit_answers_override_opposite_favorable_and_negative_defaults(browser):
    page = browser.page
    page.evaluate("""() => {
        window.addTestDropdown('visa', 'Do you need a visa?', ['Yes', 'No']);
        window.addTestDropdown('skills', 'Do you have the required qualifications?', ['Yes', 'No']);
    }""")
    answers = ANSWERS | {"Do you need a visa?": "Yes", "Do you have the required qualifications?": "No"}
    prefill_greenhouse_application(page, {"profile": PROFILE, "answers": answers})
    selected = page.evaluate("window.selections")
    assert selected["visa"] == "Yes"
    assert selected["skills"] == "No"


def test_configured_agreement_can_be_committed_when_board_uses_yes_label(browser):
    page = browser.page
    page.evaluate("dropdowns.question_privacy.options = ['No', 'Yes']")
    report = prefill_greenhouse_application(page, {"profile": PROFILE, "answers": ANSWERS})
    assert page.evaluate("window.selections.question_privacy") == "Yes"
    assert {item["field_id"]: item for item in report}["question_privacy"]["status"] == "filled"


def test_structured_configured_answer_is_not_overwritten_with_a_random_single_choice(browser):
    page = browser.page
    page.evaluate("window.addTestDropdown('languages', 'Languages', ['English', 'German', 'French'])")
    report = prefill_greenhouse_application(page, {"profile": PROFILE, "answers": ANSWERS | {
        "Languages": ["English", "German"],
    }})
    assert "languages" not in page.evaluate("window.selections")
    assert {item["field_id"]: item for item in report}["languages"]["source"] == "configured.structured"


def test_prefill_honors_jobboard_filter_before_touching_any_field(browser, tmp_path):
    (tmp_path / "jobboards.json").write_text(json.dumps({"ashby": ["jobs.ashbyhq.com"]}))
    report = prefill_greenhouse_application(browser.page, {
        "profile": PROFILE, "answers": ANSWERS, "jobboard_filter": True, "_project_root": str(tmp_path),
    })
    assert report == []
    assert browser.page.locator("#first_name").input_value() == ""


def test_other_jobboards_with_identical_form_id_are_left_to_existing_agent(browser):
    browser.page.goto("https://example.test/apply")
    browser.page.set_content(FIXTURE.read_text())
    assert prefill_greenhouse_application(browser.page, {"profile": PROFILE, "answers": ANSWERS}) == []
    assert browser.page.locator("#first_name").input_value() == ""
