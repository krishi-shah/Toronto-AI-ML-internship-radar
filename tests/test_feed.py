"""Tests for the cleaner feed: one row per role, closed postings hidden, the
work term shown, and roles promoted by their job description.

The URLs below are the real ones that put RBC's AI Developer Intern in the
feed four times and KPMG's, Sun Life's and SOTI's twice each.
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402
import sources  # noqa: E402
from core import (  # noqa: E402
    AI_IN_DESCRIPTION,
    LOOSE,
    STRICT,
    Posting,
    Store,
    ai_description_score,
    classify,
    requisition_id,
)

RBC_BOARD = ("https://rbc.wd3.myworkdayjobs.com/en-US/RBCEARLYTALENT1/job/TORONTO-Ontario-Canada/"
             "XMLNAME-2027-Winter---GRM--AI-Developer-Intern---Innovation---AI-Center-of-"
             "Excellence--4-Months-_R-0000188001-1")
RBC_GLOBAL = ("https://rbc.wd3.myworkdayjobs.com/rbcglobal1/job/TORONTO-Ontario-Canada/"
              "XMLNAME-2027-Winter---GRM--AI-Developer-Intern---Innovation---AI-Center-of-"
              "Excellence--4-Months-_R-0000188001")


class StoreCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def post(self, uid, title="Machine Learning Intern", company="Royal Bank of Canada",
             url="https://example.com/x", source="", posted_at=0):
        return Posting(company=company, title=title, location="Toronto, ON", url=url,
                       uid=uid, source=source, posted_at=posted_at)


class TestRequisitionIds(unittest.TestCase):
    def test_real_feed_urls(self):
        cases = {
            RBC_BOARD: "R0000188001",
            RBC_GLOBAL: "R0000188001",
            "https://careers.kpmg.ca/jobs/34407?icims=1": "34407",
            "https://careers.kpmg.ca/jobs/34407": "34407",
            "https://sunlife.wd3.myworkdayjobs.com/en-US/Campus/job/Toronto-Ontario/"
            "AI-Developer-Co-op_JR00128581-1": "JR00128581",
            "https://soti.wd3.myworkdayjobs.com/SOTI-Next-Gen/job/Mississauga/"
            "Data-Scientist--Intern--Jan-2027-12-Months-_R10571": "R10571",
            "https://job-boards.greenhouse.io/tenstorrentuniversity/jobs/5259347007": "5259347007",
            "https://stripe.com/jobs/search?gh_jid=123456": "123456",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(requisition_id(url), expected)

    def test_a_generic_careers_page_has_no_id(self):
        """Two tracker rows linking the same careers page are not one role."""
        self.assertEqual(requisition_id("https://acme.com/careers"), "")


class TestOneRowPerRole(StoreCase):
    def test_rbc_board_and_tracker_copies_collapse(self):
        board = self.post(
            "workday:rbc.wd3.myworkdayjobs.com:R-0000188001",
            "2027 Winter – GRM, AI Developer Intern - Innovation & AI Center of "
            "Excellence (4 Months)", url=RBC_BOARD, posted_at=int(time.time()) - 3600)
        global_site = self.post(
            "workday:rbc.wd3.myworkdayjobs.com:R-0000188001-g",
            "AI Developer Intern - Innovation & AI Center of Excellence, Winter 2027",
            url=RBC_GLOBAL)
        tracker = self.post("tracker:negar:rbc", "AI Developer Intern (Winter 2027)",
                            company="RBC", url=RBC_BOARD)
        for row in (tracker, board, global_site):
            self.store.record(row, STRICT, "t")
        self.store.commit()
        rows = self.store.recent_jobs(STRICT, 50, 336)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["uid"].startswith("tracker:"))

    def test_kpmg_query_string_variant_collapses(self):
        self.store.record(self.post("jibe:kpmg:34407", "AI Builder Intern/Co-op", "KPMG",
                                    "https://careers.kpmg.ca/jobs/34407?icims=1"), STRICT, "t")
        self.store.record(self.post("tracker:x:1", "AI Builder Intern/Co-op, Toronto (Jan-April '27)",
                                    "KPMG", "https://careers.kpmg.ca/jobs/34407"), STRICT, "t")
        self.store.commit()
        self.assertEqual(self.store.count_jobs(STRICT, 336), 1)

    def test_different_roles_stay_apart(self):
        self.store.record(self.post("a", url="https://careers.kpmg.ca/jobs/1"), STRICT, "t")
        self.store.record(self.post("b", "Data Science Intern",
                                    url="https://careers.kpmg.ca/jobs/2"), STRICT, "t")
        self.store.commit()
        self.assertEqual(self.store.count_jobs(STRICT, 336), 2)

    def test_retitled_copy_counts_as_delivered(self):
        board = self.post("workday:rbc:1", "2027 Winter – GRM, AI Developer Intern", url=RBC_BOARD)
        self.store.record(board, STRICT, "t", notified=True)
        tracker = self.post("tracker:x:1", "AI Developer Intern (Winter 2027)", "RBC", RBC_BOARD)
        self.assertTrue(self.store.delivered(tracker, STRICT))

    def test_triage_alerts_once_for_a_retitled_copy(self):
        board = Posting("Royal Bank of Canada", "2027 Winter – GRM, AI Developer Intern",
                        "Toronto, ON", RBC_BOARD, "workday:rbc:1")
        tracker = Posting("RBC", "AI Developer Intern (Winter 2027)", "Toronto, ON",
                          RBC_GLOBAL, "tracker:x:1", source="t")
        new_strict, _, skipped = radar.triage([board, tracker], self.store)
        self.assertEqual([p.uid for p, _ in new_strict], ["workday:rbc:1"])
        self.assertEqual(skipped, 1)


class TestGroupBackfill(unittest.TestCase):
    def test_rows_stored_before_grouping_are_grouped_on_open(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE jobs (uid TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,"
            " company TEXT NOT NULL, title TEXT NOT NULL, location TEXT, url TEXT,"
            " source TEXT, tier TEXT, reason TEXT, first_seen INTEGER NOT NULL,"
            " posted_at INTEGER NOT NULL DEFAULT 0, seeded INTEGER NOT NULL DEFAULT 0,"
            " notified INTEGER NOT NULL DEFAULT 0, digested INTEGER NOT NULL DEFAULT 0)")
        now = int(time.time())
        for uid, company, title, url in [
            ("workday:rbc:1", "Royal Bank of Canada", "AI Developer Intern, GRM", RBC_BOARD),
            ("tracker:x:1", "RBC", "AI Developer Intern (Winter 2027)", RBC_GLOBAL),
        ]:
            conn.execute("INSERT INTO jobs (uid, fingerprint, company, title, location, url,"
                         " source, tier, reason, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (uid, "fp" + uid, company, title, "Toronto, ON", url, "s",
                          STRICT, "t", now))
        conn.commit()
        conn.close()

        store = Store(path)
        try:
            self.assertEqual(store.count_jobs(STRICT, 336), 1)
        finally:
            store.close()


class TestClosedPostings(StoreCase):
    def test_hidden_after_two_complete_listings_without_it(self):
        self.store.record(self.post("greenhouse:a:1", source="A [greenhouse]"), STRICT, "t")
        self.store.commit()
        stamp = int(time.time()) + 1
        self.store.mark_present("A [greenhouse]", ["greenhouse:a:other"], stamp)
        self.assertEqual(self.store.count_jobs(STRICT, 336), 1)
        self.store.mark_present("A [greenhouse]", ["greenhouse:a:other"], stamp + 1)
        self.assertEqual(self.store.count_jobs(STRICT, 336), 0)

    def test_reappearing_resets_the_count(self):
        self.store.record(self.post("greenhouse:a:1", source="A"), STRICT, "t")
        stamp = int(time.time()) + 1
        self.store.mark_present("A", [], stamp)
        self.store.mark_present("A", ["greenhouse:a:1"], stamp + 1)
        self.store.mark_present("A", [], stamp + 2)
        self.assertEqual(self.store.count_jobs(STRICT, 336), 1)

    def test_closed_employer_copy_hides_the_tracker_copy(self):
        """Trackers lag; the employer's own board is the authority."""
        self.store.record(self.post("greenhouse:a:1", source="A",
                                    url="https://boards.greenhouse.io/a/jobs/1234"), STRICT, "t")
        self.store.record(self.post("tracker:x:9", source="tracker",
                                    url="https://boards.greenhouse.io/a/jobs/1234"), STRICT, "t")
        stamp = int(time.time()) + 1
        self.store.mark_present("A", [], stamp)
        self.store.mark_present("A", [], stamp + 1)
        self.assertEqual(self.store.count_jobs(STRICT, 336), 0)

    def run_wave(self, result):
        task = ("A [workday]", lambda: result, False)
        radar._run_wave([task], self.store, quiet=True, stamp=int(time.time()) + 5)

    def test_capped_search_never_closes_anything(self):
        self.store.record(self.post("workday:a:1", source="A [workday]"), STRICT, "t")
        other = self.post("workday:a:2")
        for _ in range(3):
            self.run_wave(sources.Partial([other]))
        self.assertEqual(self.store.count_jobs(STRICT, 336), 1)

    def test_empty_answer_never_closes_anything(self):
        self.store.record(self.post("workday:a:1", source="A [workday]"), STRICT, "t")
        for _ in range(3):
            self.run_wave([])
        self.assertEqual(self.store.count_jobs(STRICT, 336), 1)

    def test_complete_listing_does_close(self):
        self.store.record(self.post("workday:a:1", source="A [workday]"), STRICT, "t")
        other = self.post("workday:a:2")
        self.run_wave([other])
        self.run_wave([other])
        self.assertEqual(self.store.count_jobs(STRICT, 336), 0)


class TestTerm(StoreCase):
    def test_term_is_stored(self):
        self.store.record(self.post("a", "ML Intern (Summer 2026, Winter 2027)"), STRICT, "t")
        row = self.store.recent_jobs(STRICT, 5, 336)[0]
        self.assertEqual(row["term"], "Winter 2027")

    def test_year_first_term_is_read(self):
        self.store.record(self.post(
            "a", "2027 Winter – GRM, AI Developer Intern - Innovation (4 Months)"), STRICT, "t")
        self.assertEqual(self.store.recent_jobs(STRICT, 5, 336)[0]["term"], "Winter 2027")

    def test_no_term_is_empty(self):
        self.store.record(self.post("a", "ML Intern"), STRICT, "t")
        self.assertEqual(self.store.recent_jobs(STRICT, 5, 336)[0]["term"], "")


ML_TEXT = ("You will train models in PyTorch, fine-tune LLMs for retrieval, and ship "
           "them with our MLOps team.")


class TestDescriptionScoring(unittest.TestCase):
    def test_boilerplate_ai_does_not_count(self):
        score, _ = ai_description_score(
            "Join our AI-powered fintech. We use AI to help customers. Python, SQL, React.")
        self.assertEqual(score, 0)

    def test_real_ml_work_counts(self):
        score, terms = ai_description_score(ML_TEXT)
        self.assertGreaterEqual(score, 3)
        self.assertIn("PyTorch", terms)

    def test_html_is_stripped(self):
        score, _ = ai_description_score("<p>Deep <b>learning</b></p>")
        self.assertEqual(score, 0)
        score, _ = ai_description_score("<li>deep learning</li><li>PyTorch</li><li>NLP</li>")
        self.assertEqual(score, 3)

    def test_promotes_a_tech_title_to_strict_with_a_reason(self):
        post = Posting("Acme", "Software Developer Co-op", "Toronto, ON", "u", "x")
        verdict = classify(post, ML_TEXT)
        self.assertEqual(verdict.tier, STRICT)
        self.assertEqual(verdict.reason, AI_IN_DESCRIPTION)
        self.assertIn("PyTorch", verdict.ai_terms)

    def test_never_promotes_outside_the_commute_area(self):
        post = Posting("Acme", "Software Developer Co-op", "Ottawa, ON", "u", "x")
        self.assertIsNone(classify(post, ML_TEXT).tier)

    def test_never_promotes_a_non_tech_title(self):
        post = Posting("Acme", "Marketing Intern", "Toronto, ON", "u", "x")
        self.assertNotEqual(classify(post, ML_TEXT).tier, STRICT)

    def test_new_grad_still_caps_at_loose(self):
        post = Posting("Acme", "New Grad Software Engineer", "Toronto, ON", "u", "x")
        self.assertEqual(classify(post, ML_TEXT).tier, LOOSE)


class TestDescriber(StoreCase):
    def describer(self, limit=10):
        return radar.Describer(mock.Mock(), self.store, limit, 60)

    def test_triage_promotes_and_records_why(self):
        post = Posting("Acme", "Software Developer Co-op", "Toronto, ON",
                       "https://boards.greenhouse.io/acme/jobs/1234", "greenhouse:acme:1")
        with mock.patch.object(sources, "describe", return_value=sources.Description(ML_TEXT)):
            new_strict, _, _ = radar.triage([post], self.store, self.describer())
        self.assertEqual(len(new_strict), 1)
        row = self.store.recent_jobs(STRICT, 5, 336)[0]
        self.assertEqual(row["reason"], AI_IN_DESCRIPTION)
        self.assertIn("PyTorch", row["ai_terms"])

    def test_each_posting_is_read_once(self):
        post = Posting("Acme", "Software Developer Co-op", "Toronto, ON", "u", "greenhouse:acme:1")
        with mock.patch.object(sources, "describe", return_value=sources.Description("Python")) as d:
            describer = self.describer()
            describer.reclassify(post, classify(post))
            describer.reclassify(post, classify(post))
        self.assertEqual(d.call_count, 1)

    def test_budget_is_respected(self):
        posts = [Posting("Acme", f"Software Developer Co-op {i}", "Toronto, ON", "u", f"g:{i}")
                 for i in range(5)]
        with mock.patch.object(sources, "describe", return_value=sources.Description("x")) as d:
            describer = self.describer(limit=2)
            for post in posts:
                describer.reclassify(post, classify(post))
        self.assertEqual(d.call_count, 2)

    def test_a_dead_page_is_not_fatal(self):
        post = Posting("Acme", "Software Developer Co-op", "Toronto, ON", "u", "g:1")
        with mock.patch.object(sources, "describe", side_effect=RuntimeError("boom")):
            verdict = self.describer().reclassify(post, classify(post))
        self.assertEqual(verdict.tier, LOOSE)
        self.assertTrue(self.store.description_checked("g:1"))

    def test_backfill_promotes_rows_from_earlier_runs(self):
        post = Posting("Acme", "Software Developer Co-op", "Toronto, ON", "u", "g:1")
        self.store.record(post, LOOSE, "no AI signal")
        self.store.commit()
        with mock.patch.object(sources, "describe", return_value=sources.Description(ML_TEXT)):
            promoted = self.describer().backfill(168)
        self.assertEqual(promoted, 1)
        self.assertEqual(self.store.count_jobs(STRICT, 336), 1)
        self.assertEqual(self.store.count_jobs(LOOSE, 168), 0)

    def test_page_location_places_a_careers_link(self):
        """A careers-page link names no city; its posting page does."""
        post = Posting("Acme", "Software Developer Intern", "", "u", "html:Acme:u")
        verdict = classify(post)
        self.assertTrue(radar._wants_description(verdict))
        desc = sources.Description("Python and SQL", "Toronto, ON, Canada", 1_790_000_000)
        with mock.patch.object(sources, "describe", return_value=desc):
            verdict = self.describer().reclassify(post, verdict)
        self.assertEqual(verdict.tier, LOOSE)
        self.assertEqual(post.location, "Toronto, ON, Canada")


class TestDescribeSources(unittest.TestCase):
    def test_lever_and_ashby_need_no_request(self):
        http = mock.Mock()
        desc = sources.describe(http, "https://jobs.lever.co/x/y", {"descriptionPlain": ML_TEXT * 3})
        self.assertIn("PyTorch", desc.text)
        http.json.assert_not_called()

    def test_greenhouse_reads_the_job_api(self):
        http = mock.Mock()
        http.json.return_value = {"content": "&lt;p&gt;PyTorch &amp;amp; LLMs&lt;/p&gt;",
                                  "location": {"name": "Toronto"}}
        desc = sources.describe(http, "https://job-boards.greenhouse.io/acme/jobs/1234567", {})
        http.json.assert_called_once_with(
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1234567")
        self.assertEqual(desc.text, "PyTorch & LLMs")
        self.assertEqual(desc.location, "Toronto")

    def test_workday_reads_the_cxs_detail(self):
        http = mock.Mock()
        http.json.return_value = {"jobPostingInfo": {"jobDescription": "<p>PyTorch</p>",
                                                     "location": "Toronto"}}
        desc = sources.describe(http, RBC_BOARD, {})
        called = http.json.call_args[0][0]
        self.assertTrue(called.startswith(
            "https://rbc.wd3.myworkdayjobs.com/wday/cxs/rbc/RBCEARLYTALENT1/job/"))
        self.assertEqual(desc.text, "PyTorch")

    def test_other_pages_use_their_jobposting(self):
        http = mock.Mock()
        http.text.return_value = (
            '<script type="application/ld+json">{"@type": "JobPosting", "title": "Intern",'
            ' "description": "&lt;p&gt;Deep learning&lt;/p&gt;", "datePosted": "2026-10-01",'
            ' "jobLocation": {"address": {"addressLocality": "Toronto",'
            ' "addressRegion": "ON"}}}</script>')
        desc = sources.describe(http, "https://acme.com/jobs/1", {})
        self.assertEqual(desc.text, "Deep learning")
        self.assertEqual(desc.location, "Toronto, ON")
        self.assertGreater(desc.posted_at, 0)

    def test_page_without_jobposting_falls_back_to_its_text(self):
        http = mock.Mock()
        http.text.return_value = "<html><script>x=1</script><body>We use PyTorch</body></html>"
        self.assertEqual(sources.describe(http, "https://acme.com/jobs/1", {}).text,
                         "We use PyTorch")


if __name__ == "__main__":
    unittest.main()
