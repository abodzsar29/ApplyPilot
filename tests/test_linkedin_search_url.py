from urllib.parse import parse_qs, urlsplit

from applypilot.linkedin.search import _build_linkedin_search_url as build_legacy_search_url
from applypilot.linkedin.unified_apply import _build_linkedin_search_url as build_unified_search_url


def _query_params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_linkedin_search_url_location_overrides_stale_search_url_fields():
    config = {
        "job_title": "Junior Developer",
        "location": "London",
        "search_url": (
            "https://www.linkedin.com/jobs/search-results/"
            "?currentJobId=4422427458&keywords=Junior%20Developer"
            "&origin=JOBS_HOME_KEYWORD_HISTORY&geoId=103035651&distance=0.0&f_AL=true"
        ),
    }

    for build_url in (build_unified_search_url, build_legacy_search_url):
        url = build_url(config)
        params = _query_params(url)

        assert url.startswith("https://www.linkedin.com/jobs/search-results/")
        assert params["keywords"] == ["Junior Developer"]
        assert params["location"] == ["London"]
        assert params["f_AL"] == ["true"]
        assert "currentJobId" not in params
        assert "geoId" not in params
        assert "distance" not in params
        assert "origin" not in params


def test_linkedin_search_url_can_use_old_url_with_config_keywords():
    old_url = (
        "https://www.linkedin.com/jobs/search-results/"
        "?currentJobId=4422427458&keywords=Junior%20Developer"
        "&origin=JOBS_HOME_KEYWORD_HISTORY&geoId=103035651&distance=0.0&f_AL=true"
    )
    config = {
        "job_title": "Mechanical Engineer",
        "location": "London",
        "search_url": old_url,
        "old_url": True,
    }
    expected_url = (
        "https://www.linkedin.com/jobs/search-results/"
        "?currentJobId=4422427458&keywords=Mechanical%20Engineer"
        "&origin=JOBS_HOME_KEYWORD_HISTORY&geoId=103035651&distance=0.0&f_AL=true"
    )

    for build_url in (build_unified_search_url, build_legacy_search_url):
        assert build_url(config) == expected_url


def test_linkedin_search_params_can_intentionally_set_geo_id():
    config = {
        "job_title": "Junior Developer",
        "location": "London",
        "search_params": {
            "geoId": "90009496",
            "distance": "25",
        },
    }

    params = _query_params(build_unified_search_url(config))

    assert params["location"] == ["London"]
    assert params["geoId"] == ["90009496"]
    assert params["distance"] == ["25"]
