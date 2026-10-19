"""Config and literal matching rules shared by the LinkedIn apply commands."""

import pytest

from applypilot.linkedin.filters import configured_title_exclusion, title_exclusion_label, title_is_ignored


@pytest.mark.parametrize("settings", [{}, {"to_ignore": None}, {"to_ignore": ""}, {"to_ignore": " \t "}])
def test_empty_title_exclusion_is_disabled(settings):
    exclusion = configured_title_exclusion(settings)
    assert exclusion == ""
    assert not title_is_ignored("Climate Simulation & AI Scientist, Built Environment", exclusion)


def test_configured_title_exclusion_trims_outer_whitespace():
    assert configured_title_exclusion({"to_ignore": "  AI Scientist  "}) == "AI Scientist"


@pytest.mark.parametrize("value", [True, False, 12, ["Scientist", None], [12], [["senior"]], {"title": "Scientist"}])
def test_title_exclusion_rejects_non_text_settings(value):
    with pytest.raises(ValueError, match="to_ignore"):
        configured_title_exclusion({"to_ignore": value})


@pytest.mark.parametrize(("title", "exclusion", "ignored"), [
    ("Climate Simulation & AI Scientist, Built Environment", "sCiEnTiSt", True),
    ("Climate Simulation & AI Scientist, Built Environment", "ai scientist", True),
    ("Senior Software Engineer", "SENIOR", True),
    ("Senior Software Engineer", "scientist", False),
    ("Straße Developer", "STRASSE", True),
    ("C++ Engineer", "c++", True),
    ("C Engineer", "C++", False),
    ("AI Scientist", "AI.*", False),
    ("AI Scientist", "", False),
    ("", "Scientist", False),
])
def test_title_exclusion_is_a_case_insensitive_literal_substring(title, exclusion, ignored):
    assert title_is_ignored(title, exclusion) is ignored


def test_title_exclusion_accepts_list_and_trims_or_ignores_empty_entries():
    exclusions = configured_title_exclusion({"to_ignore": [" Senior ", "", " \t", "LEAD", "AI Scientist"]})
    assert exclusions == ["Senior", "LEAD", "AI Scientist"]
    for title in ("SENIOR Software Engineer", "Team Lead", "Junior ai scientist"):
        assert title_is_ignored(title, exclusions)
    assert not title_is_ignored("Software Engineer", exclusions)
    assert title_exclusion_label(exclusions) == "Senior, LEAD, AI Scientist"


@pytest.mark.parametrize("value", [[], ["", "  "]])
def test_empty_list_disables_title_exclusion(value):
    exclusions = configured_title_exclusion({"to_ignore": value})
    assert exclusions == []
    assert not title_is_ignored("Senior Engineer", exclusions)
    assert title_exclusion_label(exclusions) == "disabled"


def test_list_entries_remain_literal_phrases_not_regex_or_comma_delimited():
    assert not title_is_ignored("Senior Engineer", ["senior, lead", "AI.*"])
    assert title_is_ignored("C++ Engineer", ["principal", "c++"])
    assert title_is_ignored("Straße Developer", ["senior", "STRASSE"])
