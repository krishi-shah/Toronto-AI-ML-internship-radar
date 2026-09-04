"""Tests for the README feed.

This backend is the one whose output is published, so the fixtures below are
deliberately hostile: titles carrying pipes and HTML, and a posting URL with a
``javascript:`` scheme of the kind a community-edited tracker repo could carry.

The other thing under test is restraint. The feed is committed by a job that
runs every ten minutes, so an unchanged feed must produce no write at all.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402
from notify import LISTINGS_END, LISTINGS_START, ReadmeWriter, _md, _md_url  # noqa: E402

TEMPLATE = f"""# Radar

Hand-written intro that must survive.

{LISTINGS_START}
{LISTINGS_END}

## How it works

Hand-written tail that must survive.
"""


def job(title="Machine Learning Intern", company="Cohere",
        location="Toronto, Ontario, Canada", url="https://example.com/j/1",
        posted_at=None, shown_at=None):
    now = int(time.time())
    return {
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "posted_at": now if posted_at is None else posted_at,
        "shown_at": now if shown_at is None else shown_at,
    }


def health(source="Cohere [ashby]", ok=1, job_count=143, last_error=None):
    return {
        "source": source,
        "ok": ok,
        "job_count": job_count,
        "last_error": last_error,
    }


class FakeStore:
    def __init__(self, strict=(), loose=(), health_rows=()):
        self._strict = list(strict)
        self._loose = list(loose)
        self._health = list(health_rows)

    def recent_jobs(self, tier, limit=60, max_age_hours=None):
        rows = self._strict if tier == "strict" else self._loose
        return rows[:limit]

    def health_rows(self):
        return self._health


class ReadmeCase(unittest.TestCase):
    """Renders into a throwaway copy of the template."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "README.md")
        self.write(TEMPLATE)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def render(self, **kwargs):
        writer = ReadmeWriter(FakeStore(**kwargs), path=self.path)
        return writer.render()


class TestEscaping(unittest.TestCase):
    """Scraped text must not be able to break a row or inject markup."""

    def test_pipe_would_shift_every_column(self):
        self.assertEqual(_md("Intern | ML"), "Intern \\| ML")

    def test_newlines_would_end_the_row(self):
        self.assertEqual(_md("Data\nScientist"), "Data Scientist")

    def test_html_is_neutralised(self):
        self.assertEqual(_md("<b>Intern</b>"), "&lt;b&gt;Intern&lt;/b&gt;")

    def test_link_syntax_is_neutralised(self):
        self.assertEqual(_md("[click](evil)"), "\\[click\\](evil)")

    def test_backslash_escaped_before_specials(self):
        self.assertEqual(_md("a\\b"), "a\\\\b")

    def test_none_is_empty(self):
        self.assertEqual(_md(None), "")


class TestUrlSafety(unittest.TestCase):
    def test_https_kept(self):
        self.assertEqual(_md_url("https://example.com/j/1"), "https://example.com/j/1")

    def test_javascript_scheme_refused(self):
        self.assertEqual(_md_url("javascript:alert(1)"), "")

    def test_data_scheme_refused(self):
        self.assertEqual(_md_url("data:text/html;base64,AAAA"), "")

    def test_relative_path_refused(self):
        self.assertEqual(_md_url("/jobs/1"), "")

    def test_parens_encoded_so_the_link_does_not_end_early(self):
        self.assertEqual(
            _md_url("https://example.com/a(b)c"), "https://example.com/a%28b%29c"
        )

    def test_newline_refused(self):
        self.assertEqual(_md_url("https://example.com/\nx"), "")


class TestRendering(ReadmeCase):
    def test_hand_written_sections_survive(self):
        self.assertTrue(self.render(strict=[job()], health_rows=[health()]))
        body = self.read()
        self.assertIn("Hand-written intro that must survive.", body)
        self.assertIn("Hand-written tail that must survive.", body)
        self.assertIn(LISTINGS_START, body)
        self.assertIn(LISTINGS_END, body)

    def test_posting_becomes_a_linked_row(self):
        self.render(strict=[job()], health_rows=[health()])
        self.assertIn(
            "| [Machine Learning Intern](https://example.com/j/1) | Cohere |",
            self.read(),
        )

    def test_unsafe_url_renders_as_plain_text(self):
        self.render(strict=[job(url="javascript:alert(1)")])
        body = self.read()
        self.assertIn("| Machine Learning Intern |", body)
        self.assertNotIn("javascript:", body)

    def test_todays_posting_is_emphasised(self):
        self.render(strict=[job()])
        self.assertIn("| **Today** |", self.read())

    def test_undated_posting_says_found_not_posted(self):
        self.render(strict=[job(posted_at=0)])
        self.assertIn("found today", self.read())

    def test_empty_tier_states_the_window(self):
        self.render()
        self.assertIn("_Nothing in the last 7 days._", self.read())

    def test_failing_source_is_visible(self):
        self.render(health_rows=[health(ok=0, last_error="HTTPError: 403 Forbidden")])
        self.assertIn("**FAIL** HTTPError: 403 Forbidden", self.read())

    def test_missing_markers_leave_the_file_alone(self):
        self.write("# Radar\n\nNo markers here.\n")
        self.assertFalse(self.render(strict=[job()]))
        self.assertEqual(self.read(), "# Radar\n\nNo markers here.\n")

    def test_missing_file_is_not_fatal(self):
        writer = ReadmeWriter(FakeStore(), path=os.path.join(self.dir.name, "nope.md"))
        self.assertFalse(writer.render())


class TestNoOpWhenUnchanged(ReadmeCase):
    """An unchanged feed must not produce a diff for the scheduled job."""

    def test_second_render_of_the_same_feed_writes_nothing(self):
        rows = [job()]
        self.render(strict=rows, health_rows=[health()])
        first = self.read()

        # Pretend an hour passed: only the stamp line would differ.
        stale_lines = []
        for line in first.splitlines():
            if line.startswith("_Updated "):
                line = "_Updated Monday 01 January, 00:00 Toronto &middot; stale._"
            stale_lines.append(line)
        stale = "\n".join(stale_lines) + "\n"
        self.write(stale)

        self.assertTrue(self.render(strict=rows, health_rows=[health()]))
        self.assertEqual(self.read(), stale)

    def test_new_posting_does_produce_a_write(self):
        self.render(strict=[job()], health_rows=[health()])
        before = self.read()
        self.render(
            strict=[job(), job(title="Data Scientist Intern", url="https://e.com/2")],
            health_rows=[health()],
        )
        self.assertNotEqual(self.read(), before)
        self.assertIn("Data Scientist Intern", self.read())


class TestNotifierSelection(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("RADAR_NOTIFIERS", None)

    def test_env_overrides_the_configured_list(self):
        os.environ["RADAR_NOTIFIERS"] = "readme"
        built = notify.build_notifiers(["dashboard", "toast"], FakeStore())
        self.assertEqual([n.name for n in built], ["readme"])

    def test_configured_list_used_when_env_is_absent(self):
        built = notify.build_notifiers(["dashboard"], FakeStore())
        self.assertEqual([n.name for n in built], ["dashboard"])

    def test_unknown_name_is_skipped_not_fatal(self):
        self.assertEqual(notify.build_notifiers(["nope"], FakeStore()), [])


if __name__ == "__main__":
    unittest.main()
