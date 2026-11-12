"""Nightly explore run and the job sitemap source."""

import gzip
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import companies as cfg  # noqa: E402
import radar  # noqa: E402
import sources  # noqa: E402
from core import Posting, Store  # noqa: E402

US_ROW = "https://job-boards.greenhouse.io/acme/jobs/111"


def post(url, location, title="Machine Learning Intern", company="Acme"):
    return Posting(company=company, title=title, location=location, url=url, uid=url)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)


class TestAllBoards(unittest.TestCase):
    def test_us_rows_count_only_when_asked(self):
        rows = [post(US_ROW, "New York, NY")]
        self.assertEqual(sources.discover_boards(rows, set()), [])
        [board] = sources.discover_boards(rows, set(), canadian_only=False)
        self.assertEqual((board["platform"], board["token"]), ("greenhouse", "acme"))

    def test_known_boards_are_skipped(self):
        rows = [post(US_ROW, "New York, NY")]
        known = {sources.board_key("greenhouse", "acme")}
        self.assertEqual(sources.discover_boards(rows, known, canadian_only=False), [])


class TestCandidates(StoreCase):
    def add(self, *boards):
        self.store.candidates_add(
            {"platform": p, "token": t, "name": t, "hits": h} for p, t, h in boards)

    def test_never_checked_go_first_then_most_linked(self):
        self.add(("greenhouse", "small", 1), ("greenhouse", "big", 9), ("lever", "old", 50))
        self.store.candidates_checked("lever", "old", 0, 0, "0 jobs")
        due = [r["token"] for r in self.store.candidates_due(10, recheck_days=0)]
        self.assertEqual(due[:2], ["big", "small"])

    def test_checked_board_waits_for_recheck(self):
        self.add(("greenhouse", "acme", 1))
        self.store.candidates_checked("greenhouse", "acme", 0, 0, "0 jobs")
        self.assertEqual(self.store.candidates_due(10, recheck_days=30), [])

    def test_boards_already_in_discovery_are_skipped(self):
        self.add(("greenhouse", "acme", 1))
        self.store.discovered_add("greenhouse", "acme", "Acme", 1)
        self.assertEqual(self.store.candidates_due(10, recheck_days=30), [])

    def test_links_only_grow(self):
        self.add(("greenhouse", "acme", 5))
        self.add(("greenhouse", "acme", 2))
        [row] = self.store.candidates_due(10, 30)
        self.assertEqual(row["links"], 5)


class TestExplore(StoreCase):
    def run_explore(self, boards):
        self.store.candidates_add(
            {"platform": "greenhouse", "token": t, "name": t.title(), "hits": 1} for t in boards)
        self.store.commit()

        def fake(http, company, token):
            if token == "broken":
                raise RuntimeError("HTTP 500")
            if token == "toronto":
                return [post(f"https://job-boards.greenhouse.io/{token}/jobs/1", "Toronto, ON",
                             company=company)]
            return [post(f"https://job-boards.greenhouse.io/{token}/jobs/1", "Austin, TX",
                         company=company)]

        with mock.patch.dict(sources.ADAPTERS, {"greenhouse": fake}), \
                mock.patch.object(radar, "sync_auto_companies") as sync, \
                mock.patch("builtins.print"):
            radar.cmd_explore(self.store)
        return sync

    def test_board_with_canadian_student_role_joins_discovery(self):
        sync = self.run_explore(["toronto", "austin", "broken"])
        discovered = {r["token"]: r for r in self.store.discovered_rows()}
        self.assertEqual(set(discovered), {"toronto"})
        self.assertEqual(discovered["toronto"]["strict_jobs"], 1)
        self.assertGreater(discovered["toronto"]["ok_streak"], 0)
        sync.assert_called_once()

    def test_every_board_is_marked_checked(self):
        self.run_explore(["toronto", "austin", "broken"])
        rows = {r["token"]: r for r in self.store.conn.execute("SELECT * FROM candidates")}
        self.assertTrue(all(r["checked_at"] for r in rows.values()))
        self.assertTrue(rows["broken"]["result"].startswith("error"))
        self.assertEqual(rows["austin"]["canada"], 0)
        self.assertEqual(self.store.candidates_due(10, cfg.EXPLORE_RECHECK_DAYS), [])


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def urlset(*entries):
    body = "".join(
        f"<url><loc>{loc}</loc>" + (f"<lastmod>{mod}</lastmod>" if mod else "") + "</url>"
        for loc, mod in entries)
    return ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</urlset>")


def index(*locs):
    body = "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
    return ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/'
            f'sitemap/0.9">{body}</sitemapindex>')


JOB_PAGE = """<html><head><title>Ignored</title>
<script type="application/ld+json">{"@type": "JobPosting",
 "title": "Data Science Co-op (Winter 2027)", "datePosted": "2026-11-01",
 "jobLocation": {"@type": "Place", "address": {"addressLocality": "Toronto",
 "addressRegion": "ON", "addressCountry": "CA"}}}</script></head></html>"""


class FakeHttp:
    """Serves fixed bodies by URL and counts requests."""

    def __init__(self, pages, store=None):
        self.pages = pages
        self.store = store
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url not in self.pages:
            import requests
            raise requests.HTTPError(f"404 {url}")
        body = self.pages[url]
        resp = mock.Mock()
        resp.content = body if isinstance(body, bytes) else body.encode()
        resp.text = body if isinstance(body, str) else ""
        return resp

    def text(self, url, **kwargs):
        return self.get(url).text


class TestSitemap(StoreCase):
    SITE = "https://careers.example.com"

    def pages(self):
        return {
            f"{self.SITE}/sitemap.xml": index(f"{self.SITE}/sitemap-pages.xml",
                                              f"{self.SITE}/sitemap-jobs.xml"),
            f"{self.SITE}/sitemap-jobs.xml": urlset(
                (f"{self.SITE}/jobs/1-data-science-coop", iso(2)),
                (f"{self.SITE}/jobs/2-old-role", iso(90)),
                (f"{self.SITE}/jobs/3-undated", ""),
                (f"{self.SITE}/about/team", iso(1)),
            ),
            f"{self.SITE}/jobs/1-data-science-coop": JOB_PAGE,
        }

    def test_reads_recent_job_pages_through_the_index(self):
        http = FakeHttp(self.pages())
        [p] = sources.sitemap(http, "Example", f"{self.SITE}/sitemap.xml")
        self.assertEqual(p.title, "Data Science Co-op (Winter 2027)")
        self.assertIn("Toronto", p.location)
        self.assertTrue(p.uid.startswith("sitemap:careers.example.com:/jobs/1"))
        # Only the jobs child sitemap was opened, and only the recent posting.
        self.assertNotIn(f"{self.SITE}/sitemap-pages.xml", http.calls)
        self.assertNotIn(f"{self.SITE}/jobs/2-old-role", http.calls)
        self.assertNotIn(f"{self.SITE}/about/team", http.calls)

    def test_page_is_read_once_per_lastmod(self):
        http = FakeHttp(self.pages(), store=self.store)
        sources.sitemap(http, "Example", f"{self.SITE}/sitemap.xml")
        http.calls.clear()
        [p] = sources.sitemap(http, "Example", f"{self.SITE}/sitemap.xml")
        self.assertNotIn(f"{self.SITE}/jobs/1-data-science-coop", http.calls)
        self.assertEqual(p.title, "Data Science Co-op (Winter 2027)")

    def test_falls_back_to_page_title(self):
        pages = self.pages()
        pages[f"{self.SITE}/jobs/1-data-science-coop"] = (
            '<html><meta property="og:title" content="ML Intern, Toronto"></html>')
        [p] = sources.sitemap(FakeHttp(pages), "Example", f"{self.SITE}/sitemap-jobs.xml")
        self.assertEqual(p.title, "ML Intern, Toronto")
        self.assertGreater(p.posted_at, time.time() - 3 * 86400)

    def test_custom_pattern_and_gzip(self):
        pages = {
            f"{self.SITE}/s.xml.gz": gzip.compress(urlset(
                (f"{self.SITE}/en/role/77", iso(1)), (f"{self.SITE}/jobs/9", iso(1))).encode()),
            f"{self.SITE}/en/role/77": JOB_PAGE,
        }
        [p] = sources.sitemap(FakeHttp(pages), "Example", f"{self.SITE}/s.xml.gz|/role/\\d+")
        self.assertEqual(p.title, "Data Science Co-op (Winter 2027)")

    def test_removed_posting_is_skipped(self):
        pages = self.pages()
        del pages[f"{self.SITE}/jobs/1-data-science-coop"]
        self.assertEqual(sources.sitemap(FakeHttp(pages), "Example", f"{self.SITE}/sitemap.xml"), [])

    def test_too_many_postings_is_partial(self):
        entries = [(f"{self.SITE}/jobs/{i}", iso(1)) for i in range(sources.SITEMAP_MAX_PAGES + 5)]
        pages = {f"{self.SITE}/s.xml": urlset(*entries)}
        pages.update({loc: JOB_PAGE for loc, _ in entries})
        found = sources.sitemap(FakeHttp(pages), "Example", f"{self.SITE}/s.xml")
        self.assertIsInstance(found, sources.Partial)
        self.assertEqual(len(found), sources.SITEMAP_MAX_PAGES)

    def test_find_sitemaps_reads_robots(self):
        pages = self.pages()
        pages[f"{self.SITE}/robots.txt"] = f"User-agent: *\nSitemap: {self.SITE}/sitemap.xml\n"
        found = sources.find_sitemaps(FakeHttp(pages), f"{self.SITE}/careers")
        self.assertEqual(found[0], (f"{self.SITE}/sitemap.xml", 1))

    def test_registered_as_a_platform(self):
        self.assertIs(sources.ADAPTERS["sitemap"], sources.sitemap)


class TestBoardsFoundThroughTrackers(unittest.TestCase):
    def test_previously_unreachable_employers_are_configured(self):
        names = {c["name"] for c in cfg.COMPANIES}
        for name in ["OMERS", "Ontario Teachers' Pension Plan", "Intact", "General Motors",
                     "Clio", "CPP Investments", "Uber"]:
            with self.subTest(name=name):
                self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()


class TestIssueBoards(unittest.TestCase):
    def test_cisco_reads_workday_without_a_browser(self):
        [cisco] = [c for c in cfg.COMPANIES if c["name"] == "Cisco"]
        self.assertEqual(cisco["platform"], "workday")
        self.assertFalse(cisco.get("browser"))
        self.assertEqual(sources.board_from_url(
            "https://cisco.wd5.myworkdayjobs.com/en-US/cisco_careers/job/Toronto/123"),
            ("workday", cisco["token"]))

    def test_hydro_one_is_configured(self):
        [hydro] = [c for c in cfg.COMPANIES if c["name"] == "Hydro One"]
        self.assertEqual(hydro["platform"], "successfactors")
