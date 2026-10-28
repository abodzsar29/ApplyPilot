"""Semantic external-form defaults, configuration precedence, and recovery."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import non_easy as ne
from applypilot.linkedin.intelligent_apply import PreparedApplication
from applypilot.linkedin.non_easy_questions import choose_fallback_option, fallback_direction, fallback_text


@pytest.mark.parametrize("question,direction,expected", [
    ("Are you authorized to work in the UK?", "positive", "Yes"),
    ("Do you have permission to work in Germany without visa sponsorship?", "positive", "Yes"),
    ("Can you work without sponsorship?", "positive", "Yes"),
    ("Do you have the required qualifications?", "positive", "Yes"),
    ("Have you worked with Kubernetes? What is your experience?", "positive", "Yes"),
    ("Are you willing to relocate?", "positive", "Yes"),
    ("Do you need a visa?", "negative", "No"),
    ("Will you now or in the future require sponsorship?", "negative", "No"),
    ("Do you have any unspent criminal convictions?", "negative", "No"),
    ("Have you ever been convicted of an offence?", "negative", "No"),
    ("Can you confirm you have no unspent convictions?", "positive", "Yes"),
    ("Have you never been convicted of a crime?", "positive", "Yes"),
    ("Are you employed by a client, partner or competitor?", "negative", "No"),
    ("Do you have a conflict of interest?", "negative", "No"),
])
def test_semantic_direction_handles_negation(question, direction, expected):
    assert fallback_direction(question) == direction
    assert choose_fallback_option(question, ["No", "Yes"]) == (expected, f"fallback.{direction}")


@pytest.mark.parametrize("question,options,expected", [
    ("Haben Sie Erfahrung?", ["Nein", "Ja"], "Ja"),
    ("Do you need visa support?", ["Ja", "Nein"], "Nein"),
    ("What is your work authorization status?", [
        "I can't work here yet", "I can work here, but only for my current employer",
        "I can work here, but only for a limited time", "I can work here without any restrictions",
    ], "I can work here without any restrictions"),
    ("What is your work authorization status?", ["No restrictions", "Limited time"], "No restrictions"),
    ("What is your language proficiency?", [
        "None", "Elementary proficiency", "Limited working proficiency", "Professional working proficiency",
        "Full professional working proficiency", "Native or bilingual proficiency",
    ], "Native or bilingual proficiency"),
    ("What is your highest education?", ["High school", "Bachelor", "Master", "PhD"], "PhD"),
    ("What is your language proficiency?", ["A2", "B2", "C2", "C1"], "C2"),
    ("How many years of experience?", ["None", "1-2 years", "3-5 years", "10+ years"], "10+ years"),
    ("How many years of experience?", ["2.5 years", "4 years"], "4 years"),
    ("When are you available?", ["Within one month", "Immediately", "Within three months"], "Immediately"),
    ("Do you need visa sponsorship?", ["I do not require sponsorship", "I need sponsorship"],
     "I do not require sponsorship"),
    ("Do you need a visa?", ["Required", "Not required"], "Not required"),
    ("Job Applicant Data Privacy Notice", ["I decline", "I agree"], "I agree"),
])
def test_most_favorable_or_least_adverse_actual_option(question, options, expected):
    assert choose_fallback_option(question, options)[0] == expected


def test_unclassified_options_are_random_and_preserved_on_retry():
    options = ["Select...", "LinkedIn", "Other"]
    with patch("applypilot.linkedin.non_easy_questions.random.choice", return_value="Other") as chooser:
        assert choose_fallback_option("How did you hear about us?", options) == ("Other", "fallback.random")
        assert chooser.call_args.args[0] == ["LinkedIn", "Other"]
        assert choose_fallback_option("How did you hear about us?", options, current="Other") == (
            "Other", "fallback.random_preserved",
        )
        chooser.assert_called_once()


def test_unusable_options_are_a_control_problem_not_an_unknown_answer():
    assert choose_fallback_option("Unknown question", ["Select...", "Choose an option"]) is None


@pytest.mark.parametrize("question,input_type,expected", [
    ("Describe something", "text", "a"), ("How many projects?", "text", "1"),
    ("Annual salary?", "text", "1"), ("Something unknown", "number", "1"),
    ("Email", "email", "a@example.com"), ("URL", "url", "https://example.com"),
])
def test_type_compatible_free_text_defaults(question, input_type, expected):
    assert fallback_text(question, input_type) == expected


def test_old_unknown_answer_result_retries_same_application_before_returning():
    with patch.object(ne, "_run_external_application_once", side_effect=[
        ("failed:unknown_required_question", 10), ("ready", 20),
    ]) as run:
        result = ne._run_external_application("openrouter", "model", Mock(), {"application_url": "same-job"}, {}, 9222, True)
    assert result == ("ready", 30)
    assert run.call_count == 2
    assert run.call_args_list[0].args[3] == run.call_args_list[1].args[3]
    assert run.call_args_list[1].args[4]["_answer_fallback_retry"] is True


def test_repeated_agent_noncompliance_stops_instead_of_marking_unknown_job_failed_and_moving_on():
    harness = existing_non_easy.NonEasyTests("test_only_english_rejects_german_and_unknown")
    harness.setUp()
    try:
        with (patch.object(ne, "_run_external_application_once", return_value=("failed:unknown_answer", 1)),
              pytest.raises(ne.AnswerFallbackError, match="stopping rather than silently skipping")):
            ne._run_external_application("openrouter", "model", Mock(), {}, {}, 9222, False)
        summary, _, runner = harness.run_session(ne.AnswerFallbackError("agent did not click"), jobs=2)
        assert summary["stopped_reason"] == "answer_fallback_not_executed"
        assert summary["failed"] == summary["applied"] == 0
        runner.assert_called_once()
        with patch.object(ne, "_run_external_application_once", return_value=("failed:upload_rejected", 1)) as run:
            assert ne._run_external_application("openrouter", "model", Mock(), {}, {}, 9222, False)[0] == (
                "failed:upload_rejected"
            )
            run.assert_called_once()
    finally:
        harness.doCleanups()


def test_agent_prompt_uses_new_policy_without_old_required_unknown_failure_rule():
    harness = existing_non_easy.NonEasyTests("test_only_english_rejects_german_and_unknown")
    harness.setUp()
    try:
        prompt = ne._build_prompt(harness.job, harness.settings | {"_answer_fallback_retry": True})
        assert "MISSING-ANSWER FALLBACK (USER-REQUESTED DEFAULTS)" in prompt
        assert "randomly choose" in prompt
        assert "negative/least adverse" in prompt
        assert "CONTINUE THE SAME APPLICATION" in prompt
        assert "If a required question would force fabrication" not in prompt
        assert "truthfully from the provided profile/answers only" not in prompt
    finally:
        harness.doCleanups()


def test_stopped_agent_policy_failure_is_logged_as_stopped_not_failed():
    audit = Mock()
    application = PreparedApplication(Path("."), Path("cv.pdf"), Path("published.pdf"), "CV", "hash", audit)
    application.finish("stopped")
    audit.record.assert_called_once_with("application", "STOPPED", "Application outcome: stopped")
    audit.close.assert_called_once()
