"""Untrusted scraped data must never become a dangerous link or a crash."""

import gzip
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402
import radar  # noqa: E402
import requests  # noqa: E402
import sources  # noqa: E402
from core import STRICT, Posting, Store  # noqa: E402

BAD_URLS = [
    "javascript:alert(document.cookie)",
    " JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///C:/Windows/System32/calc.exe",
    "ms-settings:privacy",
    "https://e.com/\nInjected",
]


class TestSafeUrl(unittest.TestCase):
    def test_only_http_and_https(self):
        for url in BAD_URLS:
            with self.subTest(url=url):
                self.assertEqual(notify.safe_url(url), "")
        self.assertEqual(notify.safe_url("https://jobs.ashbyhq.com/x"), "https://jobs.ashbyhq.com/x")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "r.db"))
        for i, url in enumerate(BAD_URLS + ["https://good.example/job"]):
            self.store.record(Posting("Acme", f"ML Intern {i}", "Toronto, ON", url, f"u{i}",
                                      posted_at=int(time.time())), STRICT, "t")
        self.store.commit()

    def tearDown(self):
        self.store.close()


class TestRenderedLinks(StoreCase):
    def test_dashboard_never_links_a_bad_scheme(self):
        html = "".join(notify._row_html(r, "s") for r in self.store.recent_jobs("strict", 50))
        self.assertNotIn("javascript:", html.lower())
        self.assertNotIn('href="data:', html)
        self.assertNotIn("ms-settings", html)
        self.assertIn('href="https://good.example/job"', html)

    def test_feeds_drop_bad_links(self):
        items = notify._feed_items(self.store)
        self.assertEqual([i["url"] for i in items], ["https://good.example/job"])

    def test_readme_never_links_a_bad_scheme(self):
        rows = "".join(notify._md_row(r) for r in self.store.recent_jobs("strict", 50))
        self.assertNotIn("](javascript", rows.lower())
        self.assertNotIn("](data:", rows)

    def test_toast_refuses_bad_launch_links(self):
        with mock.patch.object(notify.sys, "platform", "win32"), \
                mock.patch.object(notify.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stderr="")
            notify._raise_toast("t", "b", "ms-settings:privacy")
            script = run.call_args.kwargs["input"]
        self.assertNotIn("ms-settings", script)
        self.assertNotIn("activationType", script)


class TestGeneratedConfigIsInert(unittest.TestCase):
    def test_hostile_board_name_stays_a_string(self):
        evil = "x'}]; import os; os.system('echo pwned') #"
        text = radar.render_auto_companies(
            [{"name": evil, "platform": "greenhouse", "token": "a\"b\nc"}])
        scope: dict = {}
        exec(compile(text, "auto_companies.py", "exec"), scope)  # noqa: S102
        self.assertEqual(scope["AUTO_COMPANIES"][0]["name"], evil)


class FakeResponse:
    def __init__(self, body, length=None):
        self.body = body
        self.headers = {"Content-Length": str(length)} if length is not None else {}
        self.url = "https://e.com/big"
        self.closed = False

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def close(self):
        self.closed = True


class TestDownloadLimits(unittest.TestCase):
    def test_declared_length_over_cap_is_refused(self):
        resp = FakeResponse(b"", length=sources.MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(sources.ResponseTooLarge):
            sources._read_capped(resp)
        self.assertTrue(resp.closed)

    def test_streamed_body_over_cap_is_refused(self):
        with mock.patch.object(sources, "MAX_RESPONSE_BYTES", 1000):
            with self.assertRaises(sources.ResponseTooLarge):
                sources._read_capped(FakeResponse(b"x" * 1001))

    def test_body_under_cap_is_kept(self):
        resp = FakeResponse(b"hello")
        sources._read_capped(resp)
        self.assertEqual(resp._content, b"hello")

    def test_zip_bomb_sitemap_is_refused(self):
        bomb = gzip.compress(b"0" * 5000)
        http = mock.Mock()
        http.get.return_value = mock.Mock(content=bomb)
        with mock.patch.object(sources, "MAX_RESPONSE_BYTES", 1000):
            with self.assertRaises(sources.ResponseTooLarge):
                sources._xml_text(http, "https://e.com/s.xml.gz")

    def test_private_addresses_are_never_fetched(self):
        http = sources.Http(None)
        for url in ["http://169.254.169.254/latest/meta-data", "http://localhost:8080/",
                    "http://127.0.0.1/", "http://10.1.2.3/", "http://[::1]/", "http://db.internal/"]:
            with self.subTest(url=url), mock.patch.object(http.session, "request") as req:
                with self.assertRaises(sources.BlockedURL):
                    http.get(url)
                req.assert_not_called()
        self.assertTrue(issubclass(sources.BlockedURL, requests.RequestException))


if __name__ == "__main__":
    unittest.main()
