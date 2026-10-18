import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from typer.testing import CliRunner

from applypilot.cli import app
from applypilot.linkedin import scrape


FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_job_sdui.html"
STANDALONE_FIXTURE = FIXTURE.with_name("linkedin_job_standalone.html")
URL = "https://www.linkedin.com/jobs/view/4467577090/"


class LinkedInScrapeTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"job_title": "Data AI Engineer", "location": "Berlin", "max_applications": 2}

    def test_title_words_not_substrings(self):
        for title, query, expected in (
            ("Data & AI Engineer (all genders)", "data AI engineer", True),
            ("AI / Data — Engineer", "Engineer Data AI", True),
            ("Retail Data Engineer", "AI", False),
            ("Data Engineering", "Data Engineer", False),
            ("Data Engineer", "Data AI Engineer", False),
            ("C++ Engineer", "C# Engineer", False),
            ("Senior C++ Engineer", "C++ Engineer", True),
            ("Engineer", " & ", False),
        ):
            with self.subTest(title=title, query=query):
                self.assertEqual(scrape.title_matches(title, query), expected)

    def test_search_title_authoritative_and_all_application_types(self):
        for old_url in (True, False):
            settings = self.settings | {
                "old_url": old_url,
                "search_url": "https://www.linkedin.com/jobs/search-results/?keywords=Wrong&f_AL=true&currentJobId=7&start=25",
                "search_params": {"keywords": "Also Wrong", "f_AL": "true", "f_TPR": "r86400"},
            }
            params = parse_qs(urlsplit(scrape.build_search_url(settings)).query)
            self.assertEqual(params["keywords"], ["Data AI Engineer"])
            self.assertNotIn("f_AL", params)
            self.assertNotIn("currentJobId", params)
            self.assertNotIn("start", params)

    def test_sdui_card_description_and_metadata(self):
        job = scrape.parse_job(FIXTURE.read_text(), URL)
        self.assertEqual(job["title"], "Data & AI Engineer (all genders)")
        self.assertEqual(job["company"], "PharmaHera Service GmbH")
        self.assertEqual(job["location"], "Berlin, Germany")
        self.assertEqual(job["posted"], "Reposted 5 days ago")
        self.assertEqual(job["applicants"], "Over 100 people clicked apply")
        self.assertEqual(job["workplace_type"], "Hybrid")
        self.assertEqual(job["job_type"], "Full-time")
        self.assertEqual(job["apply_url"], "https://jobs.pharma-hera.de/Data-AI-Engineer.html?src=linkedin")
        self.assertEqual(job["language"], "english")
        self.assertIn("€45,000 and €55,000", job["description"])
        self.assertIn("\nModeling in BigQuery", job["description"])
        self.assertIn("earliest possible starting date.", job["description"])
        self.assertNotIn("… more", job["description"])
        self.assertNotIn("Atida Gruppe", job["description"])
        self.assertNotIn("Restart Premium", job["job_card_text"])
        self.assertIn("Responses managed off LinkedIn", job["metadata"])
        self.assertIn("201-500 employees", job["company_details"])

    def test_missing_description_does_not_export_company_or_page(self):
        with self.assertRaisesRegex(ValueError, "No job description"):
            scrape.parse_job('<h1>Data AI Engineer</h1><span data-testid="expandable-text-box">Company description</span>', URL)

    def test_description_for_a_different_job_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not belong"):
            scrape.parse_job(FIXTURE.read_text(), "https://www.linkedin.com/jobs/view/999/")

    def test_empty_description_shell_is_not_a_job(self):
        with self.assertRaisesRegex(ValueError, "No job description"):
            scrape.parse_job('<div id="JobDetails_AboutTheJob_4467577090"><h2>About the job</h2></div>', URL)

    def test_legacy_description(self):
        job = scrape.parse_job('<div class="top-card-layout"><h1>Data AI Engineer</h1>'
                               '<a class="topcard__org-name-link" href="/company/example/">Example</a></div>'
                               '<div id="job-details">We are looking for your skills and experience.</div>', URL)
        self.assertEqual(job["company"], "Example")
        self.assertEqual(job["description"], "We are looking for your skills and experience.")

    def test_standalone_plain_title_is_not_salary_or_workplace_badge(self):
        job = scrape.parse_job(STANDALONE_FIXTURE.read_text(), URL)
        self.assertEqual(job["title"], "AI Product Engineer - London")
        self.assertEqual(job["company"], "Example Company")
        self.assertEqual(job["location"], "London Area, United Kingdom")
        self.assertEqual(job["applicants"], "Over 100 applicants")
        self.assertEqual(job["workplace_type"], "Hybrid")
        self.assertEqual(job["job_type"], "Full-time")
        self.assertIn("90K GBP/yr - 130K GBP/yr", job["badges"])
        self.assertNotIn("Sticky", job["job_card_text"])
        self.assertNotIn("Other AI", job["job_card_text"])
        self.assertIn("Apply with your CV.", job["description"])

    def test_saved_limit_excludes_language_rejections_and_duplicates(self):
        settings = self.settings | {"only_english": True, "max_applications": 2}
        candidates = [{"job_id": str(i), "title": "Data AI Engineer"} for i in range(7)]
        candidates[0]["title"] = "Retail Data Engineer"
        candidates.insert(4, candidates[3])
        jobs = {
            "1": {"title": "Data AI Engineer", "language": "german"},
            "2": {"title": "Data AI Engineer", "language": ""},
            "3": {"title": "Data AI Engineer", "language": "english", "description": "first"},
            "4": {"title": "Data AI Engineer", "language": "english", "description": "second"},
        }
        read = Mock(side_effect=lambda c: jobs[c["job_id"]])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            result = scrape.scrape_candidates(iter(candidates), read, settings, path)
            self.assertEqual(result["saved"], 2)
            self.assertEqual(result["attempted"], 4)
            self.assertEqual(result["stopped_reason"], "limit_reached")
            self.assertEqual(result["skipped"], 3)
            self.assertEqual(json.loads(path.read_text()), [jobs["3"], jobs["4"]])
            self.assertEqual(read.call_count, 4)

    def test_false_string_language_flag_allows_german(self):
        with tempfile.TemporaryDirectory() as directory:
            result = scrape.scrape_candidates(
                [{"job_id": "1", "title": "Data AI Engineer"}],
                lambda c: {"title": c["title"], "language": "german"},
                self.settings | {"only_english": "false"}, Path(directory) / "out.json",
            )
            self.assertEqual(result["saved"], 1)

    def test_progress_survives_interrupt(self):
        candidates = [{"job_id": str(i), "title": "Data AI Engineer"} for i in range(3)]
        read = Mock(side_effect=[ValueError("missing description"),
                                {"title": "Data AI Engineer", "language": "english"}, KeyboardInterrupt()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.json"
            with self.assertRaises(KeyboardInterrupt):
                scrape.scrape_candidates(candidates, read, self.settings | {"max_applications": 3}, path)
            self.assertEqual(len(json.loads(path.read_text())), 1)

    def test_failed_extractions_do_not_consume_saved_limit(self):
        def candidates():
            for i in range(7):
                yield {"job_id": str(i), "title": "AI Engineer"}
            self.fail("Fetched another candidate after saving the requested two jobs")

        read = Mock(side_effect=[ValueError("missing description")] * 5 + [
            {"title": "AI Engineer", "language": "english"},
            {"title": "AI Engineer", "language": "english"},
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.json"
            result = scrape.scrape_candidates(candidates(), read,
                self.settings | {"job_title": "AI", "max_applications": 2}, path)
            self.assertEqual(result["attempted"], 7)
            self.assertEqual(result["failed"], 5)
            self.assertEqual(result["saved"], 2)
            self.assertEqual(len(json.loads(path.read_text())), 2)
            self.assertEqual(result["stopped_reason"], "limit_reached")
            self.assertEqual(read.call_count, 7)

    def test_language_rejections_continue_until_results_exhausted(self):
        read = Mock(return_value={"title": "Data AI Engineer", "language": "german"})
        with tempfile.TemporaryDirectory() as directory:
            result = scrape.scrape_candidates(
                [{"job_id": str(i), "title": "Data AI Engineer"} for i in range(8)], read,
                self.settings | {"only_english": True}, Path(directory) / "out.json")
            self.assertEqual(result["attempted"], 8)
            self.assertEqual(result["saved"], 0)
            self.assertEqual(result["stopped_reason"], "results_exhausted")
            self.assertEqual(read.call_count, 8)

    def test_rechecks_title_from_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            result = scrape.scrape_candidates(
                [{"job_id": "1", "title": "Data AI Engineer"}],
                lambda c: {"title": "Retail Engineer", "language": "english"},
                self.settings, Path(directory) / "out.json",
            )
            self.assertEqual(result["saved"], 0)

    def test_config_alias_zero_and_validation(self):
        self.assertEqual(scrape.validate_config({"job_title": "AI", "location": "Berlin", "max_application": 3}), 3)
        for value in (-1, True, "2", 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scrape.validate_config(self.settings | {"max_applications": value})
        with self.assertRaises(ValueError):
            scrape.validate_config(self.settings | {"max_application": 5})
        with tempfile.TemporaryDirectory() as directory, patch.object(scrape, "sync_playwright") as browser:
            result = scrape.search_and_scrape(self.settings | {"max_applications": 0}, folder=directory)
            browser.assert_not_called()
            self.assertEqual(json.loads(Path(result["output"]).read_text()), [])

    def test_safe_filename_and_unique_runs(self):
        folder = Path("/tmp/scrapedjobs")
        path = scrape.output_path("../Data/AI: Engineer", folder)
        self.assertEqual(path.parent, folder)
        self.assertRegex(path.name, r"Data_AI_ Engineer_\d{4}-\d{2}-\d{2}_")
        self.assertNotEqual(scrape.output_path("AI", folder), scrape.output_path("AI", folder))

    def test_iterator_handles_lazy_cards_pagination_and_repeated_pages(self):
        a, b, c = [{"job_id": str(i), "title": "AI"} for i in range(3)]
        # Page 1 loads B after A; page 2 yields C; page 3 repeats C and stops.
        batches = [[a], [a, b], [a, b], [a, b], [a, b], [c], [c], [c], [c], [c], [c], [c]]
        page = Mock()
        with patch.object(scrape, "_collect_candidates", side_effect=batches), \
             patch.object(scrape, "_scroll_results", return_value=False), \
             patch.object(scrape, "_find_linkedin_next_page_button", return_value=Mock()), \
             patch.object(scrape, "_click_action_button", return_value=True) as click:
            self.assertEqual(list(scrape.iter_candidates(page)), [a, b, c])
            self.assertEqual(click.call_count, 2)

    def test_cli_uses_existing_config_and_options_without_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.settings))
            with patch.object(scrape, "search_and_scrape", return_value={"attempted": 1, "saved": 1, "skipped": 0, "failed": 0, "output": "out.json"}) as run:
                result = CliRunner().invoke(app, ["linkedin-scrape", "-c", str(path), "--headless", "--exactlocation"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Maximum saved jobs: 2", result.output)
            run.assert_called_once_with(self.settings, headless=True, exact_location=True, setup=False)

    def test_cli_reports_all_failed_run_as_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(self.settings))
            with patch.object(scrape, "search_and_scrape", return_value={
                "attempted": 2, "saved": 0, "skipped": 0, "failed": 2, "output": "out.json",
            }):
                result = CliRunner().invoke(app, ["linkedin-scrape", "-c", str(path)])
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("No jobs were saved", result.output)

    def test_invalid_config_fails_before_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"job_title": "AI"}')
            with patch.object(scrape, "sync_playwright") as browser:
                result = CliRunner().invoke(app, ["linkedin-scrape", "-c", str(path)])
                self.assertEqual(result.exit_code, 1)
                self.assertIn("location", result.output)
                browser.assert_not_called()


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1 to launch Chromium")
class LinkedInScrapeBrowserTests(unittest.TestCase):
    def setUp(self):
        self.playwright = scrape.sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True, executable_path=scrape.config.get_chrome_path())
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()

    def test_detail_navigation_uses_full_description(self):
        self.page.route("**/*", lambda route: route.fulfill(body=FIXTURE.read_text(), content_type="text/html"))
        job = scrape._read_job(self.page, {"job_id": "4467577090"})
        self.assertEqual(job["title"], "Data & AI Engineer (all genders)")
        self.assertIn("earliest possible starting date.", job["description"])
        self.assertNotIn("… more", job["description"])
        self.assertEqual(job["workplace_type"], "Hybrid")

    def test_in_place_reader_expands_only_current_job_without_navigation(self):
        self.page.set_content(FIXTURE.read_text())
        self.page.locator("#JobDetails_AboutTheJob_4467577090 [data-testid='expandable-text-box']").evaluate(
            """el => {
                el.innerHTML = 'Short description <button data-testid="expandable-text-button">more</button>';
                el.querySelector('button').onclick = () => { el.textContent = 'Expanded description for this job only.'; };
            }""",
        )
        url_before = self.page.url
        job = scrape.read_current_job(self.page, URL)
        self.assertEqual(self.page.url, url_before)
        self.assertEqual(job["description"], "Expanded description for this job only.")

    def test_plain_title_anchor_and_sdui_card_candidates(self):
        self.page.set_content(FIXTURE.read_text())
        candidates = scrape._collect_candidates(self.page)
        self.assertEqual(candidates, [{"job_id": "4467577090", "title": "Data & AI Engineer (all genders)"}])
        self.page.set_content('<div componentkey="job-card-component-ref-123"><p>Data AI Engineer</p></div>')
        self.assertEqual(scrape._collect_candidates(self.page), [{"job_id": "123", "title": "Data AI Engineer"}])

    def test_badges_cannot_overwrite_search_card_title(self):
        self.page.set_content('<div componentkey="job-card-component-ref-4467577090">'
                              '<p>AI Product Engineer - London</p></div>' + STANDALONE_FIXTURE.read_text())
        self.assertEqual(scrape._collect_candidates(self.page), [
            {"job_id": "4467577090", "title": "AI Product Engineer - London"},
        ])

    def test_standalone_page_is_saved_with_card_details(self):
        self.page.route("**/*", lambda route: route.fulfill(body=STANDALONE_FIXTURE.read_text(), content_type="text/html"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AI.json"
            result = scrape.scrape_candidates(
                [{"job_id": "4467577090", "title": "AI Product Engineer - London"}],
                lambda candidate: scrape._read_job(self.page, candidate),
                {"job_title": "AI", "location": "London", "only_english": True, "max_applications": 1}, path,
            )
            self.assertEqual(result["saved"], 1)
            job, = json.loads(path.read_text())
            self.assertEqual(job["title"], "AI Product Engineer - London")
            self.assertEqual(job["company"], "Example Company")
            self.assertIn("90K GBP/yr - 130K GBP/yr", job["job_card_text"])
            self.assertIn("Apply with your CV.", job["description"])

    def test_scroll_loads_more_and_paginates(self):
        self.page.set_content('''
            <div id="results" style="height:120px;overflow-y:auto"></div>
            <button aria-label="View next page">Next</button>
            <script>
              const results = document.getElementById('results');
              function card(id) {
                return '<div style="height:200px"><a href="/jobs/view/' + id + '/">AI Engineer</a></div>';
              }
              results.innerHTML = card(1);
              let loaded = false;
              results.addEventListener('scroll', () => {
                if (!loaded) { loaded = true; results.insertAdjacentHTML('beforeend', card(2)); }
              });
              document.querySelector('button').onclick = e => {
                results.innerHTML = card(3); results.scrollTop = 0; e.target.disabled = true;
              };
            </script>
        ''')
        candidates = list(scrape.iter_candidates(self.page))
        self.assertEqual([c["job_id"] for c in candidates], ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
