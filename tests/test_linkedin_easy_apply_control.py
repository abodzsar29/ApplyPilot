from applypilot.linkedin.unified_apply import (
    _EASY_APPLY_SUBMIT_BUTTON_LABELS,
    _EASY_APPLY_SUBMIT_BUTTON_SELECTORS,
    _best_option_value,
    _choice_option_text,
    _collect_field_questions,
    _choose_dropdown_value,
    _detect_job_language,
    _fallback_text_answer,
    _field_requires_answer,
    _find_easy_apply_action_button,
    _find_easy_apply_control,
    _find_easy_apply_next_button,
    _find_linkedin_next_page_button,
    _form_step_signature,
    _extract_job_title_from_card,
    _find_easy_apply_limit_modal,
    _infer_question_answer,
    _is_photo_upload_input,
    _is_resume_upload_input,
    _job_language_allowed,
    _linkedin_location_matches_config,
    _ordered_linkedin_job_cards,
    _radio_group_has_checked_option,
    _select_resume_path_for_job,
    _uncheck_follow_company_checkbox,
)


class FakeElement:
    def __init__(
        self,
        *,
        text="",
        aria="",
        href="",
        element_id="",
        accept="",
        title="",
        value="",
        aria_checked="",
        visible=True,
        enabled=True,
        checked=False,
        box=None,
        elements_by_selector=None,
    ):
        self.text = text
        self.aria = aria
        self.href = href
        self.element_id = element_id
        self.accept = accept
        self.title = title
        self.value = value
        self.aria_checked = aria_checked
        self.visible = visible
        self.enabled = enabled
        self.checked = checked
        self.box = box or {"x": 1, "y": 1, "width": 120, "height": 32}
        self.elements_by_selector = elements_by_selector or {}

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def is_checked(self):
        return self.checked

    def text_content(self):
        return self.text

    def get_attribute(self, name):
        if name == "id":
            return self.element_id
        if name == "aria-label":
            return self.aria
        if name == "href":
            return self.href
        if name == "accept":
            return self.accept
        if name == "title":
            return self.title
        if name == "value":
            return self.value
        if name == "checked" and self.checked:
            return ""
        return None

    def query_selector_all(self, selector):
        return self.elements_by_selector.get(selector, [])

    def bounding_box(self):
        return self.box

    def evaluate(self, script, *args):
        if "aria-checked" in script:
            return self.aria_checked
        if "closest(`[role=" in script:
            return self.text
        if "jobs-document-upload__container" in script:
            return self.text
        if "aria-label" in script or "innerText" in script:
            return " ".join([self.aria, self.text]).strip()
        return None


class FakePage:
    def __init__(self, elements_by_selector):
        self.elements_by_selector = elements_by_selector

    def query_selector_all(self, selector):
        return self.elements_by_selector.get(selector, [])


class LabelScope:
    def __init__(self, labels):
        self.labels = labels

    def query_selector_all(self, selector):
        return [FakeElement(aria=label) for label in self.labels]


class FollowCheckbox:
    def __init__(self, *, aria_checked, checked=True):
        self.aria_checked = aria_checked
        self.checked = checked
        self.clicked = False

    def get_attribute(self, name):
        if name == "id":
            return "generated-follow-id"
        if name == "name":
            return ""
        if name == "checked" and self.checked:
            return ""
        return None

    def is_checked(self):
        return self.checked

    def evaluate(self, script, *args):
        if "aria-checked" in script:
            return self.aria_checked
        if "innerText" in script or "textContent" in script:
            return "Follow DAiNA Inc. to stay up to date with their page"
        if "target.click" in script:
            self.clicked = True
            self.checked = False
            self.aria_checked = "false"
            return True
        return ""

    def uncheck(self, **kwargs):
        self.checked = False
        self.aria_checked = "false"


class FollowScope:
    def __init__(self, checkbox):
        self.checkbox = checkbox

    def query_selector_all(self, selector):
        if selector == "input[type='checkbox']":
            return [self.checkbox]
        return []

    def query_selector(self, selector):
        return None


class ReviewSubmitScope:
    def __init__(self):
        self.review_button = FakeElement(text="Review", aria="Review your application")
        self.submit_button = FakeElement(text="Submit application", aria="Submit application")

    def text_content(self):
        return "Review your application Follow ondevi GmbH to stay up to date with their page."

    def query_selector_all(self, selector):
        if selector == "button[aria-label='Submit application']":
            return [self.submit_button]
        if selector == "button:has-text('Review')":
            return [self.review_button]
        if selector == "button, input[type='button'], input[type='submit']":
            return [self.review_button, self.submit_button]
        return []

    def evaluate(self, script):
        return False


def test_find_easy_apply_control_accepts_sdui_search_results_current_job_id_link():
    easy_apply = FakeElement(
        aria="Easy Apply to this job",
        href="https://www.linkedin.com/jobs/search-results/?currentJobId=4433807747&keywords=Marketing&f_AL=true",
    )
    page = FakePage({"a[aria-label='Easy Apply to this job'][href*='currentJobId=']": [easy_apply]})

    assert _find_easy_apply_control(page) is easy_apply


def test_find_easy_apply_control_accepts_linkedin_apply_label_with_apply_text():
    linkedin_apply = FakeElement(
        text="Apply",
        aria="LinkedIn Apply to this job",
        href="https://www.linkedin.com/jobs/search-results/?currentJobId=4440082565&keywords=Financial%20Analyst&f_AL=true",
    )
    page = FakePage({"a[aria-label='LinkedIn Apply to this job'][href*='currentJobId=']": [linkedin_apply]})

    assert _find_easy_apply_control(page) is linkedin_apply


def test_find_easy_apply_control_rejects_search_results_link_without_current_job_id():
    easy_apply_like = FakeElement(
        aria="Easy Apply to this job",
        href="https://www.linkedin.com/jobs/search-results/?keywords=Marketing&f_AL=true",
    )
    page = FakePage({"a[aria-label='Easy Apply to this job']": [easy_apply_like]})

    assert _find_easy_apply_control(page) is None


def test_ordered_linkedin_job_cards_keeps_sequential_order_when_random_disabled():
    cards = ["a", "b", "c"]

    assert _ordered_linkedin_job_cards(cards, False) == [(0, "a"), (1, "b"), (2, "c")]


def test_ordered_linkedin_job_cards_randomizes_page_local_order(monkeypatch):
    cards = ["a", "b", "c"]

    def reverse_order(items):
        items.reverse()

    monkeypatch.setattr("applypilot.linkedin.unified_apply.random_module.shuffle", reverse_order)

    assert _ordered_linkedin_job_cards(cards, True) == [(2, "c"), (1, "b"), (0, "a")]


def test_extract_job_title_from_sdui_hidden_title_span():
    title = FakeElement(text="Technical (CRM) Project Manager (Germany) (Verified job)")
    card = FakeElement(elements_by_selector={
        "p span:not([aria-hidden='true'])": [title],
    })

    assert _extract_job_title_from_card(card) == "Technical (CRM) Project Manager (Germany) (Verified job)"


def test_extract_job_title_from_accessibility_label():
    labelled_title = FakeElement(aria="Job title, Email Marketing / CRM Account Manager")
    card = FakeElement(elements_by_selector={
        "[aria-label^='Job title']": [labelled_title],
    })

    assert _extract_job_title_from_card(card) == "Email Marketing / CRM Account Manager"


def test_linkedin_location_match_allows_city_with_radius():
    assert _linkedin_location_matches_config("Munich (50 mi)", "Munich, Bavaria, Germany")


def test_linkedin_location_match_rejects_different_country():
    assert not _linkedin_location_matches_config("Switzerland", "Munich, Bavaria, Germany")


def test_resume_upload_skips_photo_image_only_field():
    photo_input = FakeElement(
        text="Photo Upload JPG, JPEG, GIF, PNG (2 MB)",
        accept="image/jpg,image/jpeg,image/gif,image/png",
    )

    assert not _is_resume_upload_input(FakePage({}), photo_input, "data/Anabell_Oeynhausen_Lebenslauf.pdf")


def test_resume_upload_accepts_pdf_resume_field():
    resume_input = FakeElement(
        text="Resume Upload PDF, DOC, DOCX",
        accept="application/pdf,.doc,.docx",
    )

    assert _is_resume_upload_input(FakePage({}), resume_input, "data/Anabell_Oeynhausen_Lebenslauf.pdf")


def test_photo_upload_accepts_configured_image_field():
    photo_input = FakeElement(
        text="Photo Upload JPG, JPEG, GIF, PNG (2 MB)",
        accept="image/jpg,image/jpeg,image/gif,image/png",
    )

    assert _is_photo_upload_input(FakePage({}), photo_input, "data/profile_photo.jpg")


def test_photo_upload_rejects_pdf_photo_path():
    photo_input = FakeElement(
        text="Photo Upload JPG, JPEG, GIF, PNG (2 MB)",
        accept="image/jpg,image/jpeg,image/gif,image/png",
    )

    assert not _is_photo_upload_input(FakePage({}), photo_input, "data/Anabell_Oeynhausen_Lebenslauf.pdf")


def test_find_linkedin_next_page_button_skips_hidden_duplicate():
    hidden_next = FakeElement(text="Next", aria="View next page", visible=False)
    visible_next = FakeElement(text="Next", aria="View next page")
    page = FakePage({
        "button[data-testid='pagination-controls-next-button-visible']": [hidden_next, visible_next],
    })

    assert _find_linkedin_next_page_button(page) is visible_next


class ScrollableFakeModal:
    def __init__(self, button):
        self.button = button
        self.scrolled = False

    def query_selector_all(self, selector):
        if not self.scrolled:
            return []
        if selector in _EASY_APPLY_SUBMIT_BUTTON_SELECTORS or selector == "button, input[type='button'], input[type='submit']":
            return [self.button]
        return []

    def evaluate(self, script):
        self.scrolled = True
        return True


def test_find_easy_apply_action_button_scrolls_long_resume_modal_to_submit_button():
    submit = FakeElement(text="Submit application")
    modal = ScrollableFakeModal(submit)

    assert _find_easy_apply_action_button(
        modal,
        _EASY_APPLY_SUBMIT_BUTTON_SELECTORS,
        _EASY_APPLY_SUBMIT_BUTTON_LABELS,
    ) is submit


def test_find_easy_apply_next_button_ignores_stale_review_button_on_submit_step():
    scope = ReviewSubmitScope()

    assert _find_easy_apply_next_button(scope) is None


def test_easy_apply_limit_modal_is_detected():
    limit_modal = FakeElement(
        text=(
            "You reached today’s Easy Apply limit "
            "Save this job and continue applying tomorrow. Got it"
        )
    )
    page = FakePage({"dialog[data-testid='dialog']": [limit_modal]})

    assert _find_easy_apply_limit_modal(page) is limit_modal


def test_radio_group_has_checked_option_detects_preselected_resume_choice():
    selected_resume = FakeElement(checked=True)
    older_resume = FakeElement(checked=False)
    page = FakePage({"input[type='radio'][name='radio-group-resume']": [selected_resume, older_resume]})

    assert _radio_group_has_checked_option(page, "radio-group-resume")


def test_radio_group_ignores_stale_checked_input_when_visible_wrapper_is_false():
    stale_radio = FakeElement(text="Ja", checked=True, aria_checked="false")
    other_radio = FakeElement(text="Nein", checked=False, aria_checked="false")
    page = FakePage({"input[type='radio'][name='onsite-question']": [stale_radio, other_radio]})

    assert not _radio_group_has_checked_option(page, "onsite-question")


def test_german_work_authorization_question_selects_ja():
    label = "Sind Sie rechtlich befugt, in diesem Land zu arbeiten: Deutschland?"
    profile = {}
    answers = {"authorized_to_work": "Yes"}

    inferred = _infer_question_answer(label, profile, answers)
    selected = _best_option_value(label, [("Ja", "Ja"), ("Nein", "Nein")], inferred, profile, answers)

    assert inferred == "Yes"
    assert selected == "Ja"


def test_unknown_binary_question_prefers_ja_option():
    selected = _best_option_value(
        "Können Sie sich vorstellen, abwechselnd vor Ort und remote zu arbeiten?",
        [("Nein", "Nein"), ("Ja", "Ja")],
        None,
        {},
        {},
    )

    assert selected == "Ja"


def test_choice_option_text_reads_visible_sdui_radio_wrapper_text():
    radio = FakeElement(text="Ja")

    assert _choice_option_text(radio, "radio") == "Ja"


def test_salary_question_uses_configured_salary_answer():
    answers = {"Please confirm your salary expectations*": "60000-80000"}

    assert _infer_question_answer(
        "Expected Salary Range (annual gross base salary, please specify the currency)*",
        {},
        answers,
    ) == "60000-80000"
    assert _fallback_text_answer("Expected Salary Range*", {}, {}) == "60000-80000 EUR"


def test_unknown_years_question_defaults_to_four():
    assert _infer_question_answer("How many years of relevant experience do you have?", {}, {}) == "4"
    assert _fallback_text_answer("Number of years", {}, {}) == "4"


def test_italian_phone_country_dropdown_uses_configured_country_code():
    options = [
        FakeElement(text="Seleziona un’opzione", value="Seleziona un’opzione"),
        FakeElement(text="Germania (+49)", value="Germania (+49)"),
        FakeElement(text="Italia (+39)", value="Italia (+39)"),
    ]
    select = FakeElement(elements_by_selector={"option": options})
    profile = {"phone_country_code": "+49"}

    assert _infer_question_answer("Prefisso telefonico Paese", profile, {}) == "+49"
    assert _choose_dropdown_value(select, "Prefisso telefonico Paese", profile, {}) == "Germania (+49)"


def test_consent_checkbox_label_is_treated_as_required():
    checkbox = FakeElement()

    assert _field_requires_answer(checkbox, "Personal data consent*")


def test_form_step_signature_uses_unique_field_questions():
    scope = LabelScope([
        "Are you happy to work hybrid?*",
        "Are you happy to work hybrid?*",
        "German language proficiency*",
    ])
    questions = _collect_field_questions(scope)

    assert questions == ["Are you happy to work hybrid?*", "German language proficiency*"]
    assert _form_step_signature(scope) == "are you happy to work hybrid|german language proficiency"


def test_follow_company_checkbox_trusts_sdui_aria_false_over_checked_input():
    checkbox = FollowCheckbox(aria_checked="false", checked=True)
    scope = FollowScope(checkbox)

    assert not _uncheck_follow_company_checkbox(scope)
    assert not checkbox.clicked
    assert checkbox.checked


def test_follow_company_checkbox_clicks_visible_wrapper_when_aria_true():
    checkbox = FollowCheckbox(aria_checked="true", checked=True)
    scope = FollowScope(checkbox)

    assert _uncheck_follow_company_checkbox(scope)
    assert checkbox.clicked
    assert checkbox.aria_checked == "false"


def test_detect_job_language_identifies_german_description():
    text = (
        "Wir suchen eine erfahrene Marketing Managerin mit sehr guten Deutschkenntnissen. "
        "Zu deinen Aufgaben gehoeren Kampagnen, Zusammenarbeit mit dem Team und die Bewerbung."
    )

    assert _detect_job_language(text) == "german"


def test_detect_job_language_identifies_english_description():
    text = (
        "We are looking for a Marketing Manager with strong communication skills. "
        "You will work with our team on campaign responsibilities and business requirements."
    )

    assert _detect_job_language(text) == "english"


def test_select_resume_path_uses_detected_language_specific_resume():
    config = {
        "resume_paths": {
            "english": "data/cv_english.pdf",
            "german": "data/cv_german.pdf",
        }
    }

    selected_path, language = _select_resume_path_for_job(
        config,
        "Wir suchen dich fuer unser Team mit Erfahrung und Deutschkenntnissen.",
        "data/default.pdf",
    )

    assert language == "german"
    assert selected_path == "data/cv_german.pdf"


def test_select_resume_path_falls_back_when_language_is_unclear():
    selected_path, language = _select_resume_path_for_job(
        {"resume_paths": {"english": "data/cv_english.pdf", "german": "data/cv_german.pdf"}},
        "CRM Salesforce HubSpot",
        "data/default.pdf",
    )

    assert language == ""
    assert selected_path == "data/default.pdf"


def test_select_resume_path_respects_disabled_language_detection():
    selected_path, language = _select_resume_path_for_job(
        {
            "auto_select_resume_by_job_language": False,
            "resume_paths": {"english": "data/cv_english.pdf", "german": "data/cv_german.pdf"},
        },
        "We are looking for a candidate with experience and communication skills for our team.",
        "data/default.pdf",
    )

    assert language == ""
    assert selected_path == "data/default.pdf"


def test_only_english_allows_english_jobs():
    assert _job_language_allowed({"only_english": True}, "english")


def test_only_english_rejects_german_and_unclear_jobs():
    assert not _job_language_allowed({"only_english": True}, "german")
    assert not _job_language_allowed({"only_english": True}, "")


def test_only_english_disabled_allows_any_detected_language():
    assert _job_language_allowed({"only_english": False}, "german")
    assert _job_language_allowed({}, "")
