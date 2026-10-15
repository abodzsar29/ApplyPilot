from applypilot.stepstone.apply import build_stepstone_search_url


def test_stepstone_search_url_uses_title_location_and_quick_apply_filter():
    url = build_stepstone_search_url({"job_title": "Python Developer", "location": "Berlin"})

    assert url == (
        "https://www.stepstone.de/jobs/python-developer/in-berlin"
        "?radius=30&am=INTERNAL&searchOrigin=Resultlist_top-search"
    )


def test_stepstone_search_url_preserves_explicit_filtered_url():
    url = build_stepstone_search_url(
        {
            "search_url": "https://www.stepstone.de/jobs/developer/in-berlin?radius=30&am=INTERNAL",
            "job_title": "Ignored",
            "location": "Ignored",
        }
    )

    assert url == "https://www.stepstone.de/jobs/developer/in-berlin?radius=30&am=INTERNAL"
