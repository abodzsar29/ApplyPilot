import os
from unittest.mock import Mock, patch

import pytest
import test_linkedin_non_easy as existing_non_easy

from applypilot.linkedin import unified_apply as easy


@pytest.fixture
def page():
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("Set RUN_BROWSER_TESTS=1 to launch Chromium")
    harness = existing_non_easy.NonEasyBrowserTests("test_sdui_easy_apply_exclusion")
    harness.setUp()
    try:
        yield harness.page
    finally:
        harness.doCleanups()


def location_popover(page, *, control='button aria-label="Location"', editor=None, icon=""):
    tag = control.split()[0]
    editor = editor or (
        '<div componentkey="LocationTypeahead_TextEditor" contenteditable="true" '
        'role="textbox" aria-label="Location"></div>'
    )
    page.set_content(f"""
        <input componentkey="semanticSearchBox" value="Software">
        <{control} id="location-control" aria-controls="location-popover">
            {icon}<span><p id="active-location">Hungary</p></span>
        </{tag}>
        <div id="location-popover" hidden>
            {editor}
            <div componentkey="semanticSearchBoxLocationList"></div>
        </div>
        <script>
            const control = document.getElementById('location-control');
            const popover = document.getElementById('location-popover');
            control.onclick = () => {{ popover.hidden = false; }};
            const editor = popover.querySelector('input, [contenteditable]');
            editor.oninput = () => {{
                const list = popover.querySelector('[componentkey="semanticSearchBoxLocationList"]');
                list.innerHTML = '<a href="/jobs/search-results/?location=Berlin%2C+Germany">Berlin, Germany</a>';
                list.firstElementChild.onclick = event => {{
                    event.preventDefault();
                    document.getElementById('active-location').textContent = 'Berlin, Germany';
                    popover.hidden = true;
                }};
            }};
        </script>
    """)


def test_repairs_labelled_location_button_without_old_svg(page):
    location_popover(page)
    assert easy._active_linkedin_search_location(page) == "Hungary"
    assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")
    assert easy._active_linkedin_search_location(page) == "Berlin, Germany"
    assert page.locator('[componentkey="semanticSearchBox"]').input_value() == "Software"


def test_repairs_inline_legacy_location_input(page):
    page.set_content('''
        <input id="jobs-search-box-keyword-id" value="Software">
        <input id="jobs-search-box-location-id" aria-label="City, state, or zip code" value="Hungary">
        <script>
            const locationInput = document.getElementById('jobs-search-box-location-id');
            locationInput.onkeydown = event => {
                if (event.key === 'Enter') document.body.dataset.searchLocation = locationInput.value;
            };
        </script>
    ''')
    assert easy._active_linkedin_search_location(page) == "Hungary"
    assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")
    assert page.locator("body").get_attribute("data-search-location") == "Berlin, Germany"
    assert page.locator("#jobs-search-box-keyword-id").input_value() == "Software"


@pytest.mark.parametrize("control", [
    'div role="button" aria-label="search location"', 'button aria-label="LOCATION"', 'div aria-label="Location"',
])
def test_supports_button_roles_and_case_insensitive_location_labels(page, control):
    location_popover(page, control=control)
    assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")


@pytest.mark.parametrize("icon_id", ["location-marker-small", "location-marker-medium"])
def test_icon_control_handles_nested_label_and_different_icon_size(page, icon_id):
    location_popover(
        page, control="div",
        icon=f'<span><svg id="{icon_id}" width="20" height="20"></svg></span>',
    )
    assert easy._active_linkedin_search_location(page) == "Hungary"
    assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")


@pytest.mark.parametrize("editor", [
    '<input type="text" aria-label="Search location">',
    '<input type="search" placeholder="City, state, or zip code">',
])
def test_supports_native_and_control_scoped_popover_editors(page, editor):
    location_popover(page, editor=editor)
    assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")
    assert page.locator('[componentkey="semanticSearchBox"]').input_value() == "Software"


def test_ignores_hidden_stale_location_controls_and_editors(page):
    location_popover(page)
    page.evaluate('''() => {
        const stale = document.createElement('div');
        stale.hidden = true;
        stale.innerHTML = '<button aria-label="Location"><svg id="location-marker-small"></svg>'
            + '<p>London, United Kingdom</p></button><input aria-label="Location" value="London">';
        document.body.prepend(stale);
    }''')
    assert easy._active_linkedin_search_location(page) == "Hungary"
    assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")


def test_prefers_current_location_over_generic_location_filter_button(page):
    location_popover(page)
    page.evaluate('''() => {
        const filter = document.createElement('button');
        filter.setAttribute('aria-label', 'Location');
        filter.textContent = 'Location';
        filter.onclick = () => { document.body.dataset.wrongControlClicked = 'true'; };
        document.body.prepend(filter);
    }''')
    assert easy._active_linkedin_search_location(page) == "Hungary"
    assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")
    assert page.locator("body").get_attribute("data-wrong-control-clicked") is None


def test_selects_matching_suggestion_instead_of_wrong_first_result(page):
    location_popover(page)
    page.evaluate('''() => {
        const editor = document.querySelector('[contenteditable]');
        editor.oninput = () => {
            const list = document.querySelector('[componentkey="semanticSearchBoxLocationList"]');
            list.innerHTML = '<a href="/jobs/search-results/?location=Berlin%2CNJ">Berlin, New Jersey, USA</a>'
                + '<a href="/jobs/search-results/?location=Berlin%2CGermany">Berlin, Germany</a>';
            list.onclick = event => {
                event.preventDefault();
                document.getElementById('active-location').textContent = event.target.textContent;
                document.getElementById('location-popover').hidden = true;
            };
        };
    }''')
    assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")
    assert easy._active_linkedin_search_location(page) == "Berlin, Germany"


def test_waits_for_async_role_options_and_committed_location(page):
    location_popover(page, editor='<input aria-label="Location" role="combobox" aria-controls="locations">')
    page.evaluate('''() => {
        document.querySelector('[componentkey="semanticSearchBoxLocationList"]').remove();
        const editor = document.querySelector('#location-popover input');
        editor.oninput = () => setTimeout(() => {
            const list = document.createElement('div');
            list.id = 'locations';
            list.setAttribute('role', 'listbox');
            list.innerHTML = '<div role="option">Berlin, Germany</div>';
            list.onclick = () => setTimeout(() => {
                document.getElementById('active-location').textContent = 'Berlin, Germany';
                document.getElementById('location-popover').hidden = true;
            }, 350);
            document.getElementById('location-popover').append(list);
        }, 350);
    }''')
    assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")


def test_already_correct_location_does_not_open_editor_or_wait(page):
    location_popover(page)
    page.locator("#active-location").evaluate("el => el.textContent = 'Berlin, Germany'")
    with patch.object(page, "wait_for_timeout") as wait:
        assert easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")
    wait.assert_not_called()
    assert page.locator("#location-popover").is_hidden()


def test_rejected_selection_does_not_count_draft_text_as_search_location(page):
    location_popover(page, editor='<input aria-label="Location">')
    page.evaluate('''() => {
        const editor = document.querySelector('#location-popover input');
        editor.oninput = () => {
            const list = document.querySelector('[componentkey="semanticSearchBoxLocationList"]');
            list.innerHTML = '<a href="/jobs/search-results/?location=Berlin%2CGermany">Berlin, Germany</a>';
            list.onclick = event => { event.preventDefault(); };
        };
    }''')
    original_wait = easy._wait_for_linkedin_location_match
    with patch.object(easy, "_wait_for_linkedin_location_match", side_effect=(
        lambda page, target: original_wait(page, target, timeout_ms=400)
    )):
        assert not easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")
    assert easy._active_linkedin_search_location(page) == "Hungary"
    assert page.locator('#location-popover input').input_value() == "Berlin, Germany"
    page.locator("#location-control").evaluate("el => el.hidden = true")
    assert easy._active_linkedin_search_location(page) == ""


def test_does_not_fill_unrelated_keyword_input_when_location_editor_is_missing(page):
    location_popover(page)
    page.locator("[contenteditable]").evaluate("el => el.remove()")
    assert not easy._ensure_configured_linkedin_search_location(page, "Berlin, Germany")
    assert page.locator('[componentkey="semanticSearchBox"]').input_value() == "Software"
    assert easy._active_linkedin_search_location(page) == "Hungary"


def test_matching_wait_is_bounded_and_does_not_accept_unreadable_or_wrong_location():
    browser_page = Mock()
    for observed in ("Hungary", ""):
        with patch.object(easy, "_active_linkedin_search_location", return_value=observed) as read:
            assert not easy._wait_for_linkedin_location_match(browser_page, "Berlin, Germany", timeout_ms=400)
        assert read.call_count == 3
    with patch.object(easy, "_active_linkedin_search_location", side_effect=["", "Hungary", "Berlin, Germany"]):
        assert easy._wait_for_linkedin_location_match(browser_page, "Berlin, Germany", timeout_ms=400)


def test_missing_location_control_retries_then_stops_without_other_field_edits():
    browser_page = Mock()
    browser_page.locator.return_value.first.count.return_value = 0
    browser_page.evaluate.return_value = {"clicked": False, "popupIds": []}
    with patch.object(easy, "_active_linkedin_search_location", return_value="Hungary"):
        assert not easy._ensure_active_linkedin_search_location(browser_page, "Berlin, Germany")
    assert browser_page.evaluate.call_count == 12
    assert browser_page.wait_for_timeout.call_count == 11
    browser_page.locator.return_value.first.fill.assert_not_called()
