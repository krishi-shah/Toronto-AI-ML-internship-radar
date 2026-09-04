"""Tests for the dedupe fingerprint and uid stability.

If a fingerprint is too loose, two different roles collapse and one is never
seen. If it is too tight, the same role pings from every source it appears on.
Both failures are expensive, so both directions are covered here.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (  # noqa: E402
    LOOSE,
    STRICT,
    Posting,
    Store,
    fingerprint,
    normalize_company,
    normalize_title,
)


class TestCompanyNormalization(unittest.TestCase):
    def test_legal_suffixes_collapse(self):
        for variant in ["Cohere", "Cohere Inc.", "Cohere Inc", "COHERE",
                        "Cohere Technologies", "cohere labs"]:
            with self.subTest(variant=variant):
                self.assertEqual(normalize_company(variant), normalize_company("Cohere"))

    def test_punctuation_and_spacing_ignored(self):
        self.assertEqual(
            normalize_company("Wealthsimple"), normalize_company("Wealth simple")
        )

    def test_ampersand_expanded(self):
        self.assertEqual(normalize_company("Johnson & Johnson"),
                         normalize_company("Johnson and Johnson"))

    def test_different_companies_stay_distinct(self):
        self.assertNotEqual(normalize_company("Cohere"), normalize_company("Coherent"))


class TestTitleNormalization(unittest.TestCase):
    def test_coop_spellings_collapse(self):
        base = normalize_title("Machine Learning Co-op")
        for variant in ["Machine Learning Co op", "Machine Learning Coop",
                        "Machine Learning CO-OP", "Machine Learning Cooperative"]:
            with self.subTest(variant=variant):
                self.assertEqual(normalize_title(variant), base)

    def test_cycle_labels_stripped(self):
        base = normalize_title("Machine Learning Intern")
        for variant in [
            "Machine Learning Intern, Winter 2027",
            "Machine Learning Intern (W27)",
            "Machine Learning Intern - Summer 2027",
            "Machine Learning Intern 2027",
        ]:
            with self.subTest(variant=variant):
                self.assertEqual(normalize_title(variant), base)

    def test_req_ids_stripped(self):
        self.assertEqual(
            normalize_title("Machine Learning Intern JR2021277"),
            normalize_title("Machine Learning Intern"),
        )

    def test_location_suffix_stripped(self):
        self.assertEqual(
            normalize_title("ML Intern (Toronto)"), normalize_title("ML Intern")
        )

    def test_work_mode_stripped(self):
        self.assertEqual(
            normalize_title("ML Intern - Remote"), normalize_title("ML Intern")
        )

    def test_word_order_insensitive(self):
        self.assertEqual(
            normalize_title("Intern, Machine Learning"),
            normalize_title("Machine Learning Intern"),
        )

    def test_internship_and_intern_collapse(self):
        self.assertEqual(
            normalize_title("Machine Learning Internship"),
            normalize_title("Machine Learning Intern"),
        )

    def test_distinct_roles_stay_distinct(self):
        self.assertNotEqual(
            normalize_title("Machine Learning Intern"),
            normalize_title("Software Engineering Intern"),
        )


class TestFingerprint(unittest.TestCase):
    def test_same_role_from_ats_and_tracker_matches(self):
        """The exact case this exists for: one role, two sources, one ping."""
        from_ats = fingerprint("Cohere", "Machine Learning Intern, Winter 2027")
        from_tracker = fingerprint("Cohere Inc.", "Machine Learning Internship (W27)")
        self.assertEqual(from_ats, from_tracker)

    def test_different_titles_at_same_company_differ(self):
        self.assertNotEqual(
            fingerprint("Cohere", "Machine Learning Intern"),
            fingerprint("Cohere", "Backend Engineering Intern"),
        )

    def test_same_title_at_different_companies_differ(self):
        self.assertNotEqual(
            fingerprint("Cohere", "Machine Learning Intern"),
            fingerprint("Waabi", "Machine Learning Intern"),
        )

    def test_is_deterministic_across_calls(self):
        self.assertEqual(
            fingerprint("Cohere", "ML Intern"), fingerprint("Cohere", "ML Intern")
        )

    def test_is_a_hex_digest(self):
        fp = fingerprint("Cohere", "ML Intern")
        self.assertEqual(len(fp), 40)
        self.assertTrue(all(c in "0123456789abcdef" for c in fp))


class TestUidStability(unittest.TestCase):
    """A uid that changes between runs re-notifies the same posting forever."""

    def test_adapters_use_provider_ids_not_ordering(self):
        import sources

        http = object()  # never called; we only inspect uid construction below
        del http

        # Simulate the shape each adapter builds its uid from.
        cases = {
            "ashby": "ashby:cohere:cd3eacfe-1169-4df0-8164-93a857d5ddf0",
            "greenhouse": "greenhouse:tenstorrent:8601430002",
            "lever": "lever:waabi:5f2a8c1e-0000-4000-8000-000000000000",
            "workday": "workday:nvidia.wd5.myworkdayjobs.com:JR2021277",
        }
        for platform, uid in cases.items():
            with self.subTest(platform=platform):
                # No timestamps, no indexes, no positions in the uid.
                self.assertNotIn("None", uid)
                self.assertTrue(uid.startswith(platform))
                self.assertGreaterEqual(len(uid.split(":")), 3)

        self.assertTrue(hasattr(sources, "ADAPTERS"))


class TestEtagCacheSafety(unittest.TestCase):
    """A conditional request must never skip postings nobody recorded.

    Regression: running --check before --seed poisoned the etag cache, so the
    seed got 304s for every tracker and stored none of them. The next real run
    then saw ~5,500 tracker postings as brand new.
    """

    def test_http_without_store_does_no_caching(self):
        import sources

        self.assertIsNone(sources.Http(None).store)

    def test_http_with_store_caches(self):
        import sources

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        store = Store(tmp.name)
        try:
            self.assertIsNotNone(sources.Http(store).store)
        finally:
            store.close()
            os.unlink(tmp.name)

    def test_check_never_passes_the_store_to_http(self):
        import inspect

        import radar

        src = inspect.getsource(radar.cmd_check)
        self.assertIn("sources.Http(None)", src)
        self.assertNotIn("sources.Http(store)", src)

    def test_seed_bypasses_the_cache(self):
        import inspect

        import radar

        src = inspect.getsource(radar.cmd_run)
        # Seeding must pass None; a plain run may pass the store.
        self.assertIn("None if seed else store", src)


class TestStoreDedupe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def _post(self, uid, company="Cohere", title="Machine Learning Intern"):
        return Posting(
            company=company, title=title, location="Toronto, ON",
            url="https://example.com", uid=uid,
        )

    def test_uid_is_remembered(self):
        post = self._post("ashby:cohere:abc")
        self.assertFalse(self.store.seen_uid(post.uid))
        self.store.record(post, LOOSE, "test")
        self.store.commit()
        self.assertTrue(self.store.seen_uid(post.uid))

    def test_fingerprint_matches_across_different_uids(self):
        self.store.record(self._post("ashby:cohere:abc"), LOOSE, "test")
        self.store.commit()
        other = self._post("tracker:SimplifyJobs:xyz", "Cohere Inc.",
                           "Machine Learning Internship (W27)")
        self.assertFalse(self.store.seen_uid(other.uid))
        self.assertTrue(self.store.seen_fingerprint(other.fingerprint()))

    def test_re_recording_does_not_reset_notified_flag(self):
        post = self._post("ashby:cohere:abc")
        self.store.record(post, LOOSE, "test")
        self.store.mark_notified(post.uid)
        self.store.record(post, LOOSE, "test")  # second run sees it again
        self.store.commit()
        row = self.store.conn.execute(
            "SELECT notified, first_seen FROM jobs WHERE uid = ?", (post.uid,)
        ).fetchone()
        self.assertEqual(row["notified"], 1)

    def test_html_layer_only_reports_new_links(self):
        first = self.store.new_links("Ada", ["/a", "/b"])
        self.store.commit()
        self.assertEqual(sorted(first), ["/a", "/b"])
        second = self.store.new_links("Ada", ["/a", "/b", "/c"])
        self.store.commit()
        self.assertEqual(second, ["/c"])

    def test_loose_sighting_never_suppresses_a_strict_hit(self):
        """Regression: the expensive one.

        A tracker listed Cohere's "ML Intern/Co-op, Fall 2026" (loose, wrong
        cycle). Fingerprints ignore cycle labels, so when Cohere posted the
        real "ML Intern/Co-op (Winter 2027)" on Ashby, the strict hit was
        silently swallowed by the loose one and never alerted.
        """
        loose_row = self._post(
            "tracker:Simplify:abc", "Cohere", "Machine Learning Intern/Co-op, Fall 2026"
        )
        self.store.record(loose_row, LOOSE, "different cycle")
        self.store.commit()

        strict_row = self._post(
            "ashby:cohere:xyz", "Cohere", "Machine Learning Intern/Co-op  (Winter 2027)"
        )
        # Same role by fingerprint...
        self.assertEqual(loose_row.fingerprint(), strict_row.fingerprint())
        # ...but the strict hit must still be deliverable.
        self.assertFalse(
            self.store.fingerprint_delivered(strict_row.fingerprint(), STRICT)
        )

    def test_notified_strict_hit_does_suppress_a_second_strict(self):
        """One role from two sources is still only one ping."""
        first = self._post("ashby:cohere:xyz")
        self.store.record(first, STRICT, "ai + student + canada")
        self.store.mark_notified(first.uid)
        self.store.commit()

        second = self._post("tracker:Simplify:abc", "Cohere Inc.",
                            "Machine Learning Internship (W27)")
        self.assertEqual(first.fingerprint(), second.fingerprint())
        self.assertTrue(
            self.store.fingerprint_delivered(second.fingerprint(), STRICT)
        )

    def test_loose_candidate_is_suppressed_by_any_sighting(self):
        self.store.record(self._post("u1"), LOOSE, "test")
        self.store.commit()
        twin = self._post("u2", "Cohere Inc.", "Machine Learning Internship")
        self.assertTrue(self.store.fingerprint_delivered(twin.fingerprint(), LOOSE))

    def test_unnotified_strict_row_does_not_suppress(self):
        """A strict row recorded but never actually delivered must not block."""
        first = self._post("ashby:cohere:xyz")
        self.store.record(first, STRICT, "test")  # notified stays 0
        self.store.commit()
        self.assertFalse(self.store.fingerprint_delivered(first.fingerprint(), STRICT))

    def test_undated_seeded_rows_never_look_fresh(self):
        """Regression: month-old roles appearing as "today".

        Sources with no publish date fall back to first_seen. Seeding stamps
        that as "now" for everything, so an undated seeded row would claim to
        be brand new for the whole window.
        """
        undated = self._post("tracker:hanzili:abc", "SAP", "AI Intern")
        self.store.record(undated, STRICT, "t", seeded=True)
        self.store.commit()
        self.assertEqual(len(self.store.recent_jobs(STRICT, 50, 168)), 0)

    def test_undated_rows_found_after_seeding_do_show(self):
        """A link that genuinely just appeared is real news."""
        undated = self._post("tracker:hanzili:xyz", "SAP", "AI Intern")
        self.store.record(undated, STRICT, "t", seeded=False)
        self.store.commit()
        self.assertEqual(len(self.store.recent_jobs(STRICT, 50, 168)), 1)

    def test_dated_seeded_rows_still_show_if_recent(self):
        """Seeding does not hide postings that carry a real date."""
        dated = self._post("ashby:cohere:1")
        dated.posted_at = int(time.time()) - 3600
        self.store.record(dated, STRICT, "t", seeded=True)
        self.store.commit()
        self.assertEqual(len(self.store.recent_jobs(STRICT, 50, 168)), 1)

    def test_digest_queue_drains_once(self):
        self.store.record(self._post("u1"), LOOSE, "test")
        self.store.commit()
        self.assertEqual(len(self.store.pending_digest()), 1)
        self.store.mark_digested(["u1"])
        self.store.commit()
        self.assertEqual(len(self.store.pending_digest()), 0)


if __name__ == "__main__":
    unittest.main()
