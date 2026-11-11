"""Postings added by hand in manual_postings.py."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402
import requests  # noqa: E402
import sources  # noqa: E402
from core import LOOSE, STRICT, classify  # noqa: E402

JOB_PAGE = """<html><script type="application/ld+json">{"@type": "JobPosting",
 "title": "Machine Learning Co-op (Summer 2027)", "datePosted": "2026-11-08",
 "hiringOrganization": {"name": "Example AI"},
 "jobLocation": {"@type": "Place", "address": {"addressLocality": "Toronto",
 "addressRegion": "ON", "addressCountry": "CA"}}}</script></html>"""

EMPLOYER = "https://careers.example.ai/jobs/123"
GLASSDOOR = "https://www.glassdoor.ca/job-listing/ml-intern-example-JV_IC2281069.htm?jl=1009"


def http_with(pages):
    http = mock.Mock()

    def text(url, **kwargs):
        if url not in pages:
            raise requests.HTTPError(f"404 {url}")
        return pages[url]
    http.text.side_effect = text
    return http


class TestManualPostings(unittest.TestCase):
    def test_employer_link_is_read_from_the_page(self):
        [post] = sources.manual(http_with({EMPLOYER: JOB_PAGE}), [{"url": EMPLOYER}])
        self.assertEqual(post.title, "Machine Learning Co-op (Summer 2027)")
        self.assertIn("Toronto", post.location)
        self.assertGreater(post.posted_at, 0)
        self.assertEqual(classify(post).tier, STRICT)

    def test_aggregator_link_is_never_opened(self):
        http = http_with({})
        entry = {"url": GLASSDOOR, "title": "Machine Learning Intern", "company": "Example AI",
                 "location": "Toronto, ON", "term": "Summer 2027", "added": "2026-11-10"}
        [post] = sources.manual(http, [entry])
        http.text.assert_not_called()
        self.assertEqual(post.url, GLASSDOOR)
        self.assertEqual(classify(post).tier, STRICT)

    def test_aggregator_link_without_details_is_skipped(self):
        http = http_with({})
        with mock.patch("builtins.print"):
            found = sources.manual(http, [{"url": GLASSDOOR}])
        self.assertEqual(found, [])
        http.text.assert_not_called()

    def test_entry_fields_override_the_page(self):
        [post] = sources.manual(http_with({EMPLOYER: JOB_PAGE}),
                                [{"url": EMPLOYER, "title": "Data Analyst Intern"}])
        self.assertEqual(post.title, "Data Analyst Intern")

    def test_term_is_used_when_the_posting_names_none(self):
        entry = {"url": GLASSDOOR, "title": "ML Intern", "company": "X",
                 "location": "Toronto, ON", "term": "Winter 2027"}
        [post] = sources.manual(http_with({}), [entry])
        self.assertEqual(classify(post).tier, LOOSE)

    def test_failed_page_makes_the_listing_incomplete(self):
        with mock.patch("builtins.print"):
            found = sources.manual(http_with({}), [{"url": EMPLOYER}, {
                "url": GLASSDOOR, "title": "ML Intern", "company": "X", "location": "Toronto"}])
        self.assertIsInstance(found, sources.Partial)
        self.assertEqual(len(found), 1)

    def test_uid_is_stable(self):
        self.assertEqual(sources.manual_uid(EMPLOYER + "/"), sources.manual_uid(EMPLOYER))

    def test_wired_in_only_when_entries_exist(self):
        with mock.patch.object(radar, "_manual_entries", return_value=[]):
            self.assertFalse(any("[manual]" in t[0] for t in radar.build_tasks(mock.Mock())))
        with mock.patch.object(radar, "_manual_entries", return_value=[{"url": EMPLOYER}]):
            self.assertTrue(any("[manual]" in t[0] for t in radar.build_tasks(mock.Mock())))


if __name__ == "__main__":
    unittest.main()
