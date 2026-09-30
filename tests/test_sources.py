"""Tests for source parsing that shapes what reaches the feed."""

import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sources  # noqa: E402


def item(**overrides):
    base = {
        "id": "abc",
        "company_name": "Royal Bank of Canada",
        "title": "Data Engineer Co-op",
        "locations": ["Toronto, ON, Canada"],
        "url": "https://example.com/j/abc",
        "active": True,
    }
    base.update(overrides)
    return base


def title_of(**overrides):
    [post] = sources._tracker_from_json("t", "o/r", [item(**overrides)])
    return post.title


class TestTrackerSeasons(unittest.TestCase):
    """Regression: "Data Engineer Co-op, N/A" showed up in the live feed."""

    def test_placeholder_season_is_not_appended(self):
        for placeholder in ["N/A", "n/a", "TBD", "-", "", None, "Unknown"]:
            with self.subTest(season=placeholder):
                self.assertEqual(title_of(season=placeholder), "Data Engineer Co-op")

    def test_placeholder_inside_terms_list_is_dropped(self):
        self.assertEqual(
            title_of(season="N/A", terms=["N/A", "Winter 2027"]),
            "Data Engineer Co-op, Winter 2027",
        )

    def test_real_season_is_appended(self):
        self.assertEqual(
            title_of(season="Winter 2027"), "Data Engineer Co-op, Winter 2027"
        )


class TestSpeedyapplyTable(unittest.TestCase):
    """speedyapply names its apply-link column "Posting", not "Apply"."""

    README = (
        "| Company | Position | Location | Salary | Posting | Age |\n"
        "|---|---|---|---|---|---|\n"
        '| <a href="https://www.lyft.com"><strong>Lyft</strong></a> '
        "| Applied Scientist Intern - Summer 2027 | Toronto, ON | $58/hr "
        '| <a href="https://app.careerpuck.com/job-board/lyft/job/88"><img '
        'src="https://i.imgur.com/x.png" alt="Apply" width="70"/></a> | 0d |\n'
    )

    def test_row_is_parsed_with_the_apply_link(self):
        [row] = sources.parse_tracker_readme(self.README)
        self.assertEqual(row["company"], "Lyft")
        self.assertEqual(row["title"], "Applied Scientist Intern - Summer 2027")
        self.assertEqual(row["location"], "Toronto, ON")
        self.assertEqual(row["url"], "https://app.careerpuck.com/job-board/lyft/job/88")
        self.assertGreater(row["posted_at"], 0)


class TestCareersPageJsonLd(unittest.TestCase):
    """Careers pages that embed schema.org JobPosting data yield real rows."""

    PAGE = """<html><head>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@graph": [
      {"@type": "Organization", "name": "Acme"},
      {"@type": "JobPosting", "title": "Machine Learning Intern",
       "url": "/careers/ml-intern", "identifier": {"value": "R123"},
       "datePosted": "2026-09-25", "employmentType": "INTERN",
       "jobLocation": {"@type": "Place", "address": {
         "addressLocality": "Toronto", "addressRegion": "ON",
         "addressCountry": "CA"}}},
      {"@type": "JobPosting", "title": "Research Intern",
       "jobLocationType": "TELECOMMUTE",
       "applicantLocationRequirements": {"@type": "Country", "name": "Canada"}}
    ]}
    </script></head><body><a href="/careers/other">Other link</a></body></html>"""

    def setUp(self):
        self.posts = sources._jsonld_postings(
            "Acme", "https://acme.ai/careers/", self.PAGE
        )

    def test_job_postings_are_extracted(self):
        self.assertEqual([p.title for p in self.posts],
                         ["Machine Learning Intern", "Research Intern"])

    def test_fields_are_mapped(self):
        post = self.posts[0]
        self.assertEqual(post.url, "https://acme.ai/careers/ml-intern")
        self.assertEqual(post.location, "Toronto, ON, CA")
        self.assertEqual(post.uid, "html:Acme:R123")
        self.assertGreater(post.posted_at, 0)

    def test_remote_in_canada_is_carried(self):
        self.assertEqual(self.posts[1].location, "Remote, Canada")

    def test_extracted_posting_classifies(self):
        from core import STRICT, classify

        self.assertEqual(classify(self.posts[0]).tier, STRICT)

    def test_html_links_prefers_structured_data(self):
        http = mock.Mock()
        http.text.return_value = self.PAGE
        posts = sources.html_links(http, "Acme", "https://acme.ai/careers/")
        self.assertNotIn("Other link", [p.title for p in posts])
        self.assertEqual(len(posts), 2)

    def test_page_without_jsonld_falls_back_to_links(self):
        http = mock.Mock()
        http.text.return_value = '<a href="/careers/job/1">ML Intern</a>'
        posts = sources.html_links(http, "Acme", "https://acme.ai/careers/")
        self.assertEqual([p.title for p in posts], ["ML Intern"])

    def test_malformed_jsonld_is_ignored(self):
        page = '<script type="application/ld+json">{not json</script>'
        self.assertEqual(sources._jsonld_postings("Acme", "https://a.ai/", page), [])


class TestAmazon(unittest.TestCase):
    JOB = {
        "id_icims": "10535280",
        "title": "ML Systems Software Development Engineer Intern, Annapurna Labs - 2027",
        "normalized_location": "Toronto, Ontario, CAN",
        "location": "CA, ON, Toronto",
        "job_path": "/en/jobs/10535280/ml-systems-sde-intern",
        "posted_date": "September  9, 2026",
    }

    def test_postings_are_merged_across_search_terms(self):
        http = mock.Mock()
        http.json.return_value = {"hits": 1, "jobs": [self.JOB]}
        [post] = sources.amazon(http, "Amazon", "CAN")
        self.assertEqual(http.json.call_count, len(sources.WORKDAY_TERMS))
        self.assertEqual(post.uid, "amazon:10535280")
        self.assertEqual(
            post.url, "https://www.amazon.jobs/en/jobs/10535280/ml-systems-sde-intern"
        )
        self.assertIn("Toronto, Ontario", post.location)

    def test_padded_date_is_parsed(self):
        self.assertGreater(sources._amazon_date("September  9, 2026"), 0)
        self.assertEqual(sources._amazon_date("20 days"), 0)


class TestBadgeLinks(unittest.TestCase):
    """Regression: CIBC's strict hit linked to a shields.io badge, not the job."""

    def test_badge_wrapped_link_resolves_to_the_posting(self):
        cell = (" [![Apply](https://img.shields.io/badge/-Apply-blue?style=for-the-badge)]"
                "(https://cibc.wd3.myworkdayjobs.com/campus/job/Toronto-ON/AI_2619748) ")
        self.assertEqual(
            sources._cell_url(cell),
            "https://cibc.wd3.myworkdayjobs.com/campus/job/Toronto-ON/AI_2619748",
        )

    def test_html_badge_resolves_to_the_href(self):
        cell = ('<a href="https://jobs.example.com/1"><img '
                'src="https://i.imgur.com/JpkfjIq.png" alt="Apply"/></a>')
        self.assertEqual(sources._cell_url(cell), "https://jobs.example.com/1")

    def test_plain_markdown_link(self):
        self.assertEqual(sources._cell_url("[Apply](https://e.com/j/2)"), "https://e.com/j/2")

    def test_image_only_cell_has_no_url(self):
        self.assertEqual(sources._cell_url("![x](https://img.shields.io/badge/x)"), "")


class TestWorkdayMultiLocation(unittest.TestCase):
    """Regression: TD's Winter 2027 SWE co-op showed only "2 Locations"."""

    BASE = "https://td.wd3.myworkdayjobs.com/wday/cxs/td/TD_Bank_Careers/jobs"

    def make(self, title, location, path="/job/Toronto/X_R1"):
        from core import Posting

        return Posting(company="TD", title=title, location=location, url="u",
                       uid=f"workday:{path}", raw={"externalPath": path})

    def test_multi_location_student_role_is_resolved(self):
        http = mock.Mock()
        http.json.return_value = {"jobPostingInfo": {
            "location": "Toronto, Ontario",
            "additionalLocations": ["Mississauga, Ontario"],
            "country": {"descriptor": "Canada"},
        }}
        post = self.make("Software Engineer Intern/Co-op (Winter 2027)", "2 Locations")
        sources._resolve_workday_locations(http, self.BASE, [post])
        http.json.assert_called_once_with(
            "https://td.wd3.myworkdayjobs.com/wday/cxs/td/TD_Bank_Careers/job/Toronto/X_R1"
        )
        self.assertEqual(post.location, "Toronto, Ontario, Mississauga, Ontario, Canada")

    def test_single_location_and_non_student_roles_are_not_fetched(self):
        http = mock.Mock()
        posts = [self.make("Software Engineer Intern", "Toronto, Ontario"),
                 self.make("Senior Engineer", "3 Locations")]
        sources._resolve_workday_locations(http, self.BASE, posts)
        http.json.assert_not_called()

    def test_detail_calls_are_capped(self):
        http = mock.Mock()
        http.json.return_value = {}
        posts = [self.make("Intern", "2 Locations", f"/job/{i}")
                 for i in range(sources.WORKDAY_DETAIL_CAP + 10)]
        sources._resolve_workday_locations(http, self.BASE, posts)
        self.assertEqual(http.json.call_count, sources.WORKDAY_DETAIL_CAP)


class TestProbe(unittest.TestCase):
    """Only boards that actually carry postings are reported as hits."""

    def run_probe(self, adapters):
        def fail(*_):
            raise requests.HTTPError("404")

        patched = {p: adapters.get(p, fail) for p in sources.PROBE_PLATFORMS}
        with mock.patch.dict(sources.ADAPTERS, patched):
            return sources.probe(None, "deep-genomics")

    def test_hit_prints_a_paste_ready_line(self):
        lines = self.run_probe({"lever": lambda *_: [object(), object()]})
        self.assertIn("# lever            HIT   2 postings", lines)
        self.assertIn(
            '    {"name": "Deep Genomics", "platform": "lever", '
            '"token": "deep-genomics", "ai_native": False},',
            lines,
        )

    def test_empty_board_is_a_miss(self):
        """SmartRecruiters answers 200 with nothing for any slug at all."""
        lines = self.run_probe({"smartrecruiters": lambda *_: []})
        self.assertFalse(any("HIT" in line for line in lines))
        self.assertIn("# smartrecruiters  miss  (0 postings)", lines)

    def test_every_platform_is_reported(self):
        lines = self.run_probe({})
        self.assertEqual(len(lines), len(sources.PROBE_PLATFORMS))


if __name__ == "__main__":
    unittest.main()
