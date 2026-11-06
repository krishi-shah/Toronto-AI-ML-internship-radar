"""Tracker short links resolved to the employer's posting URL.

vanshb03 wraps every apply link in go.vansh.nyc. Left alone, a third of the
strict feed linked a redirect, those rows never deduped against the employer's
own copy, and discovery could not see the boards behind them.
"""

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar  # noqa: E402
import sources  # noqa: E402
from core import STRICT, Posting, Store  # noqa: E402

GREENHOUSE = "https://job-boards.greenhouse.io/acme/jobs/7012345"


def response(status, location="", body=""):
    resp = mock.Mock()
    resp.status_code = status
    resp.is_redirect = status in (301, 302, 303, 307, 308)
    resp.headers = {"Location": location} if location else {}
    resp.text = body
    return resp


def tracker_post(url="https://go.vansh.nyc/1gR", location="Toronto, ON", uid="tracker:v/r:1"):
    return Posting(
        company="Acme", title="Machine Learning Intern", location=location,
        url=url, uid=uid, source="vansh-Summer2027",
    )


class TestIsShortLink(unittest.TestCase):
    def test_known_shorteners(self):
        for url in ["https://go.vansh.nyc/1gR", "https://bit.ly/x", "http://www.tinyurl.com/y"]:
            with self.subTest(url=url):
                self.assertTrue(sources.is_short_link(url))

    def test_employer_boards_are_not_short(self):
        for url in [GREENHOUSE, "https://jobs.ashbyhq.com/cohere/abc", "", "not a url"]:
            with self.subTest(url=url):
                self.assertFalse(sources.is_short_link(url))

    def test_shorteners_are_never_sniffed(self):
        self.assertIn("go.vansh.nyc", sources.SNIFF_DENY_HOSTS)


class TestResolveShortLink(unittest.TestCase):
    def test_follows_redirect_to_the_posting(self):
        http = mock.Mock()
        http.get.return_value = response(302, GREENHOUSE)
        self.assertEqual(sources.resolve_short_link(http, "https://go.vansh.nyc/1gR"), GREENHOUSE)
        # The employer's page itself is never fetched.
        self.assertEqual(http.get.call_count, 1)
        self.assertFalse(http.get.call_args.kwargs["allow_redirects"])

    def test_follows_a_chain_of_shorteners(self):
        http = mock.Mock()
        http.get.side_effect = [response(301, "https://bit.ly/abc"), response(302, GREENHOUSE)]
        self.assertEqual(sources.resolve_short_link(http, "https://go.vansh.nyc/1gR"), GREENHOUSE)

    def test_relative_location_is_joined(self):
        http = mock.Mock()
        http.get.side_effect = [response(302, "/2xy"), response(302, GREENHOUSE)]
        self.assertEqual(sources.resolve_short_link(http, "https://go.vansh.nyc/1gR"), GREENHOUSE)

    def test_meta_refresh_page(self):
        body = f'<html><meta http-equiv="refresh" content="0; url={GREENHOUSE}"></html>'
        http = mock.Mock()
        http.get.return_value = response(200, body=body)
        self.assertEqual(sources.resolve_short_link(http, "https://go.vansh.nyc/1gR"), GREENHOUSE)

    def test_dead_end_returns_empty(self):
        http = mock.Mock()
        http.get.return_value = response(200, body="<html>Link expired</html>")
        self.assertEqual(sources.resolve_short_link(http, "https://go.vansh.nyc/1gR"), "")

    def test_endless_shortener_loop_gives_up(self):
        http = mock.Mock()
        http.get.return_value = response(302, "https://go.vansh.nyc/again")
        self.assertEqual(sources.resolve_short_link(http, "https://go.vansh.nyc/1gR"), "")
        self.assertEqual(http.get.call_count, sources.SHORT_LINK_MAX_HOPS)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)


class TestResolveShortLinks(StoreCase):
    def run_resolver(self, posts, resolved=GREENHOUSE, **kwargs):
        with mock.patch.object(sources, "resolve_short_link", return_value=resolved) as fn:
            n = radar.resolve_short_links(
                mock.Mock(), self.store, posts, True, time.monotonic() + 60, **kwargs)
        return n, fn

    def test_swaps_url_and_keeps_the_short_link(self):
        post = tracker_post()
        n, _ = self.run_resolver([post])
        self.assertEqual(n, 1)
        self.assertEqual(post.url, GREENHOUSE)
        self.assertEqual(post.raw["short_url"], "https://go.vansh.nyc/1gR")
        self.assertEqual(post.uid, "tracker:v/r:1")

    def test_each_link_is_looked_up_once_ever(self):
        self.run_resolver([tracker_post()])
        again = tracker_post()
        n, fn = self.run_resolver([again])
        fn.assert_not_called()
        self.assertEqual(n, 1)
        self.assertEqual(again.url, GREENHOUSE)

    def test_dead_end_is_cached_and_not_retried(self):
        post = tracker_post()
        self.run_resolver([post], resolved="")
        self.assertEqual(post.url, "https://go.vansh.nyc/1gR")
        _, fn = self.run_resolver([tracker_post()])
        fn.assert_not_called()

    def test_error_is_retried_next_run(self):
        with mock.patch.object(sources, "resolve_short_link", side_effect=OSError("boom")):
            radar.resolve_short_links(
                mock.Mock(), self.store, [tracker_post()], True, time.monotonic() + 60)
        _, fn = self.run_resolver([tracker_post()])
        fn.assert_called_once()

    def test_non_canadian_rows_are_skipped(self):
        post = tracker_post(location="New York, NY")
        _, fn = self.run_resolver([post])
        fn.assert_not_called()
        self.assertEqual(post.url, "https://go.vansh.nyc/1gR")

    def test_duplicate_links_cost_one_lookup(self):
        posts = [tracker_post(uid="a"), tracker_post(uid="b")]
        n, fn = self.run_resolver(posts)
        fn.assert_called_once()
        self.assertEqual(n, 2)

    def test_per_run_limit(self):
        posts = [tracker_post(url=f"https://go.vansh.nyc/{i}", uid=str(i)) for i in range(5)]
        _, fn = self.run_resolver(posts, limit=2)
        self.assertEqual(fn.call_count, 2)


class TestRefreshUrl(StoreCase):
    def test_resolved_row_merges_with_the_employer_copy(self):
        employer = Posting(company="Acme", title="Machine Learning Intern, Winter 2027",
                           location="Toronto, ON", url=GREENHOUSE, uid="greenhouse:acme:7012345")
        # The tracker retitled the role, so only the requisition id can match.
        tracker = Posting(company="Acme Inc", title="ML Intern", location="Toronto, ON",
                          url="https://go.vansh.nyc/1gR", uid="tracker:v/r:1")
        self.store.record(employer, STRICT, "ai + student + ontario")
        self.store.record(tracker, STRICT, "ai + student + ontario")
        self.assertNotEqual(self.store.group_for(employer), self.store.group_for(tracker))

        tracker.url = GREENHOUSE
        self.assertTrue(self.store.refresh_url(tracker))
        rows = {r["uid"]: r for r in self.store.conn.execute("SELECT uid, url, group_id FROM jobs")}
        self.assertEqual(rows[tracker.uid]["url"], GREENHOUSE)
        self.assertEqual(rows[tracker.uid]["group_id"], rows[employer.uid]["group_id"])

    def test_unchanged_or_unknown_rows_are_left_alone(self):
        post = tracker_post()
        self.assertFalse(self.store.refresh_url(post))
        self.store.record(post, STRICT, "ai + student + ontario")
        self.assertFalse(self.store.refresh_url(post))


class TestStudentSearchTerms(unittest.TestCase):
    def test_every_search_adapter_shares_the_full_list(self):
        self.assertIs(sources.WORKDAY_TERMS, sources.STUDENT_SEARCH_TERMS)
        self.assertIs(sources.SEARCH_TERMS, sources.STUDENT_SEARCH_TERMS)
        for term in ["PEY", "coop", "co-op", "work term", "internship"]:
            with self.subTest(term=term):
                self.assertIn(term, sources.STUDENT_SEARCH_TERMS)


if __name__ == "__main__":
    unittest.main()
