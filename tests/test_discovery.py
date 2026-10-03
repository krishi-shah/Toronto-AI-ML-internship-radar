"""Tests for automatic board discovery.

Discovery turns tracker rows into new sources, so a bug here either misses
boards or, worse, starts scraping boards that have nothing to do with Canada.
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
from core import Posting, Store  # noqa: E402


def post(url, location="Toronto, ON", company="Acme"):
    return Posting(company=company, title="ML Intern", location=location,
                   url=url, uid=url)


class TestBoardFromUrl(unittest.TestCase):
    """URL shapes taken from real tracker rows."""

    def test_known_posting_urls(self):
        cases = {
            "https://job-boards.greenhouse.io/doordashusa/jobs/8233953":
                ("greenhouse", "doordashusa"),
            "https://boards.greenhouse.io/figma/jobs/6178851004?gh_jid=6178851004":
                ("greenhouse", "figma"),
            "https://boards.greenhouse.io/embed/job_app?for=wealthsimple&token=123":
                ("greenhouse", "wealthsimple"),
            "https://jobs.ashbyhq.com/quora/cf34f80e-fe5c-454d-bc9a-4c59993ffda0/application":
                ("ashby", "quora"),
            "https://jobs.lever.co/waabi/1b2c3d4e-aaaa-bbbb-cccc-1234567890ab":
                ("lever", "waabi"),
            "https://jobs.smartrecruiters.com/Ubisoft2/744000012345678":
                ("smartrecruiters", "Ubisoft2"),
            "https://apply.workable.com/huggingface/j/ABC123/":
                ("workable", "huggingface"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sources.board_from_url(url), expected)

    def test_workday_urls_become_cxs_tokens(self):
        cases = {
            "https://rbc.wd3.myworkdayjobs.com/RBCEARLYTALENT1/job/TORONTO-Ontario-Canada/X_R-1":
                "https://rbc.wd3.myworkdayjobs.com/wday/cxs/rbc/RBCEARLYTALENT1/jobs",
            "https://manulife.wd3.myworkdayjobs.com/en-US/MFCJH_Jobs/job/Toronto-Ontario/X_JR1":
                "https://manulife.wd3.myworkdayjobs.com/wday/cxs/manulife/MFCJH_Jobs/jobs",
            "https://ciena.wd5.myworkdayjobs.com/Careers/job/Ottawa/Intern_R031752":
                "https://ciena.wd5.myworkdayjobs.com/wday/cxs/ciena/Careers/jobs",
            "https://wd3.myworkdaysite.com/recruiting/magna/Magna/job/Milton-Ontario-CA/Coop_R1":
                "https://magna.wd3.myworkdayjobs.com/wday/cxs/magna/Magna/jobs",
            "https://wd5.myworkdaysite.com/en-US/recruiting/devonenergy/Careers/job/OK/X_R2":
                "https://devonenergy.wd5.myworkdayjobs.com/wday/cxs/devonenergy/Careers/jobs",
        }
        for url, token in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sources.board_from_url(url), ("workday", token))

    def test_composite_platform_urls(self):
        cases = {
            "https://hdks.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/9324":
                ("oracle", "https://hdks.fa.ca2.oraclecloud.com|CX_1"),
            "https://eedu.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1003/job/202601653":
                ("oracle", "https://eedu.fa.em3.oraclecloud.com|CX_1003"),
            "https://qualcomm.eightfold.ai/careers/job/446721229661":
                ("eightfold", "qualcomm.eightfold.ai|qualcomm.com"),
            "https://careers.amd.com/jobs/91308?icims=1":
                ("jibe", "https://careers.amd.com"),
            "https://careers.publicisgroupe.com/jobs/155173?lang=en-us&icims=1":
                ("jibe", "https://careers.publicisgroupe.com"),
            "https://careers-kinaxis.icims.com/jobs/35372/job?mobile=true&needsRedirect=false":
                ("icims", "https://careers-kinaxis.icims.com"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sources.board_from_url(url), expected)

    def test_non_board_urls(self):
        for url in [
            "https://careers.amd.com/jobs/91308",
            "https://www.amazon.jobs/en/jobs/10535280/sde-intern",
            "https://app.careerpuck.com/job-board/lyft/job/8843341002",
            "https://boards.greenhouse.io/figma",
            "https://jobs.lever.co/",
            "",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(sources.board_from_url(url))


class TestDiscoverBoards(unittest.TestCase):
    def test_canadian_postings_name_boards(self):
        boards = sources.discover_boards(
            [post("https://jobs.lever.co/kepler/1b2c3d4e-aaaa-bbbb-cccc-1234567890ab",
                  company="Kepler Communications"),
             post("https://jobs.lever.co/kepler/2b2c3d4e-aaaa-bbbb-cccc-1234567890ab",
                  location="Remote in Canada", company="Kepler Communications")],
            known=set(),
        )
        self.assertEqual(boards, [{"platform": "lever", "token": "kepler",
                                   "hits": 2, "name": "Kepler Communications"}])

    def test_non_canadian_postings_never_produce_a_board(self):
        boards = sources.discover_boards(
            [post("https://jobs.lever.co/zoox/1b2c3d4e-aaaa-bbbb-cccc-1234567890ab",
                  location="Foster City, CA"),
             post("https://jobs.lever.co/zoox/2b2c3d4e-aaaa-bbbb-cccc-1234567890ab",
                  location="")],
            known=set(),
        )
        self.assertEqual(boards, [])

    def test_configured_boards_are_skipped_case_insensitively(self):
        url = "https://rbc.wd3.myworkdayjobs.com/rbcearlytalent1/job/Toronto/X_R-1"
        known = {sources.board_key(
            "workday", "https://rbc.wd3.myworkdayjobs.com/wday/cxs/rbc/RBCEARLYTALENT1/jobs")}
        self.assertEqual(sources.discover_boards([post(url)], known), [])

    def test_ranked_by_canadian_hits(self):
        urls = (["https://jobs.lever.co/a/1b2c3d4e-aaaa-bbbb-cccc-1234567890ab"]
                + [f"https://jobs.lever.co/b/{i}b2c3d4e-aaaa-bbbb-cccc-1234567890ab"
                   for i in range(3)])
        boards = sources.discover_boards([post(u) for u in urls], known=set())
        self.assertEqual([b["token"] for b in boards], ["b", "a"])


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)


class TestDiscoveredStore(StoreCase):
    def test_pick_is_capped_and_ranked(self):
        for i, hits in enumerate([1, 9, 4]):
            self.store.discovered_add("lever", f"t{i}", f"N{i}", hits)
        picked = self.store.discovered_pick(2)
        self.assertEqual([r["token"] for r in picked], ["t1", "t2"])

    def test_repeated_failures_drop_a_board(self):
        self.store.discovered_add("lever", "dead", "Dead", 5)
        for _ in range(Store.DISCOVERY_MAX_FAILS):
            self.store.discovered_fail("lever", "dead", "HTTPError: 404")
        self.assertEqual(self.store.discovered_pick(10), [])

    def test_rediscovery_does_not_revive_a_failing_board(self):
        self.store.discovered_add("lever", "dead", "Dead", 5)
        for _ in range(Store.DISCOVERY_MAX_FAILS):
            self.store.discovered_fail("lever", "dead", "HTTPError: 404")
        self.store.discovered_add("lever", "dead", "Dead", 5)
        self.assertEqual(self.store.discovered_pick(10), [])

    def test_success_resets_failures(self):
        self.store.discovered_add("lever", "flaky", "Flaky", 1)
        self.store.discovered_fail("lever", "flaky", "timeout")
        self.store.discovered_ok("lever", "flaky", jobs=10, canada=3)
        row = self.store.discovered_rows()[0]
        self.assertEqual((row["fails"], row["job_count"], row["canada_hits"]), (0, 10, 3))

    def test_stale_boards_drop_out(self):
        self.store.discovered_add("lever", "old", "Old", 3)
        stale = int(time.time()) - (Store.DISCOVERY_STALE_DAYS + 1) * 86400
        self.store.conn.execute("UPDATE discovered SET last_canada = ?", (stale,))
        self.assertEqual(self.store.discovered_pick(10), [])

    def test_top_boards_first_then_least_recently_attempted(self):
        for token, hits in [("big", 50), ("mid", 20), ("old", 5), ("new", 3)]:
            self.store.discovered_add("lever", token, token, hits)
        now = int(time.time())
        self.store.conn.execute("UPDATE discovered SET last_attempt=? WHERE token='mid'", (now,))
        self.store.conn.execute("UPDATE discovered SET last_attempt=? WHERE token='old'",
                                (now - 3600,))
        picked = [r["token"] for r in self.store.discovered_pick(10, top=1)]
        self.assertEqual(picked, ["big", "new", "old", "mid"])

    def test_outcomes_stamp_the_attempt(self):
        self.store.discovered_add("lever", "a", "A", 1)
        self.store.discovered_add("lever", "b", "B", 1)
        self.store.discovered_ok("lever", "a", jobs=1, canada=1)
        self.store.discovered_fail("lever", "b", "boom")
        self.assertTrue(all(r["last_attempt"] for r in self.store.discovered_rows()))

    def test_old_database_gains_last_attempt(self):
        self.store.close()
        conn = sqlite3.connect(self.tmp.name)
        conn.executescript(
            "DROP TABLE discovered; CREATE TABLE discovered (platform TEXT NOT NULL,"
            " token TEXT NOT NULL, name TEXT NOT NULL, first_seen INTEGER NOT NULL,"
            " last_canada INTEGER NOT NULL, last_ok INTEGER, last_error TEXT,"
            " fails INTEGER NOT NULL DEFAULT 0, canada_hits INTEGER NOT NULL DEFAULT 0,"
            " job_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (platform, token));")
        conn.close()
        self.store = Store(self.tmp.name)
        self.store.discovered_add("lever", "a", "A", 1)
        self.assertIsNone(self.store.discovered_pick(1)[0]["last_attempt"])

    def test_summary_counts_active_boards(self):
        self.store.discovered_add("lever", "a", "A", 1)
        self.store.discovered_add("lever", "b", "B", 1)
        self.store.discovered_ok("lever", "a", jobs=5, canada=1)
        self.store.discovered_fail("lever", "b", "boom")
        self.assertEqual(self.store.discovered_summary(),
                         {"ok": 1, "failing": 1, "total": 2, "promoted": 0, "demoted": 0})


class TestDiscoveryWave(StoreCase):
    """The second wave scrapes discovered boards without touching health."""

    def test_discovered_boards_are_scraped_but_kept_out_of_health(self):
        tracker_row = post("https://jobs.lever.co/kepler/1b2c3d4e-aaaa-bbbb-cccc-1234567890ab",
                           company="Kepler")
        board_posts = [post("https://jobs.lever.co/kepler/x", company="Kepler"),
                       post("https://jobs.lever.co/kepler/y", location="Berlin, Germany")]

        def fake_lever(http, name, token):
            self.assertEqual((name, token), ("Kepler", "kepler"))
            return board_posts

        tasks = [("Tracker [tracker]", lambda: [tracker_row], False)]
        with mock.patch.object(radar, "build_tasks", return_value=tasks), \
                mock.patch.dict(sources.ADAPTERS, {"lever": fake_lever}), \
                mock.patch.object(radar.cfg, "COMPANIES", []), \
                mock.patch.object(radar.cfg, "DISCOVERY_MAX_BOARDS", 10):
            postings, errors = radar.fetch_all(None, self.store, quiet=True)

        self.assertEqual(len(postings), 3)
        self.assertEqual(errors, {})
        self.assertEqual([r["source"] for r in self.store.health_rows()],
                         ["Tracker [tracker]"])
        row = self.store.discovered_rows()[0]
        self.assertEqual((row["job_count"], row["canada_hits"]), (2, 1))

    def test_failing_discovered_board_is_not_a_health_error(self):
        tracker_row = post("https://jobs.lever.co/gone/1b2c3d4e-aaaa-bbbb-cccc-1234567890ab")

        def broken(*_):
            raise RuntimeError("404")

        tasks = [("Tracker [tracker]", lambda: [tracker_row], False)]
        with mock.patch.object(radar, "build_tasks", return_value=tasks), \
                mock.patch.dict(sources.ADAPTERS, {"lever": broken}), \
                mock.patch.object(radar.cfg, "COMPANIES", []), \
                mock.patch.object(radar.cfg, "DISCOVERY_MAX_BOARDS", 10):
            _, errors = radar.fetch_all(None, self.store, quiet=True)

        self.assertEqual(errors, {})
        self.assertEqual(self.store.discovered_rows()[0]["fails"], 1)

    def test_boards_past_the_deadline_are_skipped_not_failed(self):
        tracker_row = post("https://jobs.lever.co/kepler/1b2c3d4e-aaaa-bbbb-cccc-1234567890ab")
        called = []
        tasks = [("Tracker [tracker]", lambda: [tracker_row], False)]
        with mock.patch.object(radar, "build_tasks", return_value=tasks), \
                mock.patch.dict(sources.ADAPTERS, {"lever": lambda *a: called.append(a) or []}), \
                mock.patch.object(radar.cfg, "COMPANIES", []), \
                mock.patch.object(radar.cfg, "DISCOVERY_TIME_BUDGET_S", -1):
            postings, errors = radar.fetch_all(None, self.store, quiet=True)
        self.assertEqual((called, errors, len(postings)), ([], {}, 1))
        row = self.store.discovered_rows()[0]
        self.assertEqual((row["fails"], row["last_attempt"]), (0, None))

    def test_discovery_can_be_turned_off(self):
        tracker_row = post("https://jobs.lever.co/kepler/1b2c3d4e-aaaa-bbbb-cccc-1234567890ab")
        tasks = [("Tracker [tracker]", lambda: [tracker_row], False)]
        with mock.patch.object(radar, "build_tasks", return_value=tasks), \
                mock.patch.object(radar.cfg, "DISCOVERY_MAX_BOARDS", 0):
            postings, _ = radar.fetch_all(None, self.store, quiet=True)
        self.assertEqual(len(postings), 1)
        self.assertEqual(self.store.discovered_rows(), [])


class TestHostSniffing(StoreCase):
    """Unrecognised careers hosts are sniffed a few at a time into discovery."""

    def test_only_unplaced_canadian_non_aggregator_hosts(self):
        postings = [
            post("https://jobs.l3harris.com/job/ottawa/intern/1"),
            post("https://jobs.l3harris.com/job/ottawa/intern/2"),
            post("https://www.stripe.com/jobs/listing/intern/1", location="San Francisco, CA"),
            post("https://zapply.jobs/l/d/workday-magna-x"),
            post("https://jobs.lever.co/waabi/1b2c3d4e-aaaa-bbbb-cccc-1234567890ab"),
            post("https://vectorinstitute.ai/careers/1"),
        ]
        hosts = sources.unplaced_hosts(postings, skip={"vectorinstitute.ai"})
        self.assertEqual([(h["host"], h["hits"]) for h in hosts], [("jobs.l3harris.com", 2)])

    def test_found_boards_join_discovery_and_hosts_rest(self):
        self.store.hosts_add("careers.example.com", "https://careers.example.com/j/1", "Ex", 3)
        self.store.hosts_add("jobs.nothing.com", "https://jobs.nothing.com/j/1", "No", 1)
        results = {"https://careers.example.com/j/1": [("greenhouse", "example")],
                   "https://jobs.nothing.com/j/1": []}
        with mock.patch.object(sources, "sniff_boards", lambda http, url: results[url]), \
                mock.patch.object(radar.cfg, "COMPANIES", []):
            found = radar._sniff_hosts(None, self.store, [], quiet=True)
        self.assertEqual(found, 1)
        [board] = self.store.discovered_rows()
        self.assertEqual((board["platform"], board["token"], board["name"]),
                         ("greenhouse", "example", "Ex"))
        self.assertEqual({r["host"]: r["result"] for r in self.store.hosts_rows()},
                         {"careers.example.com": "greenhouse example",
                          "jobs.nothing.com": "no supported ATS"})

    def test_hosts_are_not_resniffed_within_the_interval(self):
        self.store.hosts_add("careers.example.com", "https://careers.example.com/j/1", "Ex", 3)
        self.store.hosts_sniffed("careers.example.com", "no supported ATS")
        self.assertEqual(self.store.hosts_due(8, every_days=14), [])
        old = int(time.time()) - 31 * 86400
        self.store.conn.execute("UPDATE hosts SET last_sniffed=?", (old,))
        self.assertEqual(len(self.store.hosts_due(8, every_days=14)), 1)

    def test_sniff_backoff_depends_on_the_result(self):
        now = int(time.time())
        samples = [
            ("opaque.example", "no supported ATS", 8 * 86400, False),
            ("oldopaque.example", "no supported ATS", 31 * 86400, True),
            ("freshhit.example", "greenhouse acme", 3 * 86400, False),
            ("hit.example", "greenhouse acme", 8 * 86400, True),
            ("fresher.example", "error: HTTPError", 8 * 86400, False),
            ("err.example", "error: HTTPError", 15 * 86400, True),
        ]
        for host, result, age, due in samples:
            self.store.hosts_add(host, f"https://{host}/j", host, 1)
            self.store.hosts_sniffed(host, result)
            self.store.conn.execute(
                "UPDATE hosts SET last_sniffed=? WHERE host=?", (now - age, host)
            )
        due_hosts = {row["host"] for row in self.store.hosts_due(20, every_days=14)}
        for host, _result, _age, due in samples:
            self.assertEqual(host in due_hosts, due, host)


class TestPromotion(StoreCase):
    def _ok(self, token, name, **counts):
        self.store.discovered_add("lever", token, name, counts.get("canada", 1))
        self.store.discovered_ok(
            "lever", token, jobs=counts.get("jobs", 2), canada=counts.get("canada", 1),
            student=counts.get("student", 0), strict=counts.get("strict", 0),
        )

    def test_strict_hit_is_promoted(self):
        self._ok("kepler", "Kepler", strict=1, student=1)
        rows, demoted = radar.select_auto_companies(self.store)
        self.assertEqual([(r["token"], r["ai_native"]) for r in rows], [("kepler", False)])
        self.assertEqual(demoted, 0)

    def test_no_student_roles_are_not_promoted(self):
        self._ok("empty", "Empty", student=0, strict=0)
        self.assertEqual(radar.select_auto_companies(self.store)[0], [])

    def test_two_student_roles_are_promoted(self):
        self._ok("d2l", "D2L", student=2, strict=0)
        self.assertEqual(radar.select_auto_companies(self.store)[0][0]["name"], "D2L")

    def test_denylist_and_curated_boards_are_skipped(self):
        self._ok("prolific", "Prolific", strict=4, student=4)
        self._ok("kepler", "Kepler", strict=1, student=1)
        with mock.patch.object(radar.cfg, "COMPANIES",
                                [{"name": "Kepler", "platform": "lever", "token": "kepler"}]):
            rows, _ = radar.select_auto_companies(self.store)
        self.assertEqual(rows, [])

    def test_three_failures_demote_a_promoted_board(self):
        self._ok("kepler", "Kepler", strict=1, student=1)
        for _ in range(3):
            self.store.discovered_fail("lever", "kepler", "HTTPError: 404")
        with mock.patch.object(radar.cfg, "AUTO_COMPANIES",
                                [{"name": "Kepler", "platform": "lever", "token": "kepler",
                                  "ai_native": False}]):
            rows, demoted = radar.select_auto_companies(self.store)
        self.assertEqual(rows, [])
        self.assertEqual(demoted, 1)

    def test_one_empty_scrape_keeps_a_promotion_and_two_drop_it(self):
        entry = {"name": "Kepler", "platform": "lever", "token": "kepler", "ai_native": False}
        self._ok("kepler", "Kepler", strict=1, student=2)
        self.store.discovered_ok("lever", "kepler", jobs=3, canada=0, student=0, strict=0)
        with mock.patch.object(radar.cfg, "AUTO_COMPANIES", [entry]):
            rows, demoted = radar.select_auto_companies(self.store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(demoted, 0)
        self.store.discovered_ok("lever", "kepler", jobs=3, canada=0, student=0, strict=0)
        with mock.patch.object(radar.cfg, "AUTO_COMPANIES", [entry]):
            rows, demoted = radar.select_auto_companies(self.store)
        self.assertEqual((rows, demoted), ([], 1))

    def test_generated_file_is_stable(self):
        self._ok("kepler", "Kepler", strict=1, student=1)
        path = os.path.join(os.path.dirname(self.tmp.name), "auto_companies.py")
        rows, _ = radar.select_auto_companies(self.store)
        self.assertTrue(radar.write_auto_companies(path, rows))
        first = open(path, encoding="utf-8").read()
        self.assertFalse(radar.write_auto_companies(path, rows))
        self.assertEqual(open(path, encoding="utf-8").read(), first)
        namespace: dict = {}
        exec(first, namespace)
        self.assertIs(namespace["AUTO_COMPANIES"][0]["ai_native"], False)
        self.assertNotIn("false", first)

    def test_check_does_not_rewrite_auto_companies(self):
        with mock.patch.object(radar, "fetch_all", return_value=([], {})), \
                mock.patch.object(radar, "sync_auto_companies") as sync:
            radar.cmd_check(self.store)
        sync.assert_not_called()


class TestSlack(StoreCase):
    def test_a_failed_source_is_retried_while_time_remains(self):
        calls = {"n": 0}

        def flaky(http, name, token):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return []

        company = {"name": "Acme", "platform": "lever", "token": "acme", "ai_native": False}
        with mock.patch.object(radar.cfg, "COMPANIES", [company]), \
                mock.patch.object(radar.cfg, "AUTO_COMPANIES", []), \
                mock.patch.object(radar.cfg, "TRACKERS", []), \
                mock.patch.object(radar.cfg, "DISCOVERY_MAX_BOARDS", 0), \
                mock.patch.dict(sources.ADAPTERS, {"lever": flaky}):
            _postings, errors = radar.fetch_all(None, self.store, quiet=True)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(errors, {})

    def test_no_retry_after_the_deadline(self):
        calls = {"n": 0}

        def broken(http, name, token):
            calls["n"] += 1
            raise RuntimeError("boom")

        company = {"name": "Acme", "platform": "lever", "token": "acme", "ai_native": False}
        with mock.patch.object(radar.cfg, "COMPANIES", [company]), \
                mock.patch.object(radar.cfg, "AUTO_COMPANIES", []), \
                mock.patch.object(radar.cfg, "TRACKERS", []), \
                mock.patch.object(radar.cfg, "DISCOVERY_MAX_BOARDS", 0), \
                mock.patch.object(radar.cfg, "DISCOVERY_TIME_BUDGET_S", -1), \
                mock.patch.dict(sources.ADAPTERS, {"lever": broken}):
            _postings, errors = radar.fetch_all(None, self.store, quiet=True)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(errors), 1)


class TestDefaultLocation(unittest.TestCase):
    def test_blank_locations_are_filled_and_real_ones_kept(self):
        thunk = radar._with_default_location(
            lambda: [post("u1", location=""), post("u2", location="Ottawa, ON")],
            "Toronto, ON",
        )
        self.assertEqual([p.location for p in thunk()], ["Toronto, ON", "Ottawa, ON"])

    def test_configured_location_reaches_html_postings(self):
        company = {"name": "Vector Institute", "platform": "html",
                   "token": "https://vectorinstitute.ai/careers/", "location": "Toronto, ON"}
        with mock.patch.object(radar.cfg, "COMPANIES", [company]), \
                mock.patch.object(radar.cfg, "TRACKERS", []), \
                mock.patch.object(sources, "html_links",
                                  return_value=[post("https://v.ai/job/1", location="")]):
            [(_, thunk, _)] = radar.build_tasks(None)
            self.assertEqual(thunk()[0].location, "Toronto, ON")


if __name__ == "__main__":
    unittest.main()
