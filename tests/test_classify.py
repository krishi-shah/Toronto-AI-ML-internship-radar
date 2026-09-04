"""Tests for the tier classifier.

The classifier is one of two places where a bug silently costs a job, so the
fixtures below are real title/location shapes taken from live boards rather
than invented strings.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import LOOSE, STRICT, Posting, classify  # noqa: E402


def p(title, location="Toronto, Ontario, Canada", company="Acme",
      ai_native=False, raw=None):
    return Posting(
        company=company,
        title=title,
        location=location,
        url="https://example.com/j/1",
        uid="u1",
        ai_native=ai_native,
        raw=raw or {},
    )


def tier(*args, **kwargs):
    return classify(p(*args, **kwargs)).tier


class TestStrictTier(unittest.TestCase):
    """AI signal + student signal + Canada signal fires an instant ping."""

    def test_canonical_ml_intern(self):
        self.assertEqual(tier("Machine Learning Intern"), STRICT)

    def test_ai_abbreviation_in_title(self):
        self.assertEqual(tier("AI Engineer Intern, Winter 2027"), STRICT)

    def test_ml_abbreviation_in_title(self):
        self.assertEqual(tier("ML Engineering Co-op"), STRICT)

    def test_applied_scientist_intern(self):
        self.assertEqual(tier("Applied Scientist Intern"), STRICT)

    def test_research_intern_deep_learning(self):
        self.assertEqual(tier("Deep Learning Research Intern"), STRICT)

    def test_data_science_coop(self):
        self.assertEqual(tier("Data Science Co-op Student"), STRICT)

    def test_llm_intern(self):
        self.assertEqual(tier("LLM Infrastructure Intern"), STRICT)

    def test_perception_intern_for_av_company(self):
        self.assertEqual(tier("Perception Engineering Intern"), STRICT)

    def test_remote_counts_as_location(self):
        self.assertEqual(tier("NLP Intern", location="Remote"), STRICT)

    def test_new_grad_ml_role(self):
        self.assertEqual(tier("New Grad Machine Learning Engineer"), STRICT)

    def test_winter_2027_cycle_token(self):
        self.assertEqual(tier("AI Intern (W27)"), STRICT)


class TestCoopSpellingVariants(unittest.TestCase):
    """Boards spell co-op five different ways. All must reach strict."""

    def test_variants(self):
        for spelling in [
            "Machine Learning Co-op",
            "Machine Learning Co op",
            "Machine Learning Coop",
            "Machine Learning Cooperative Education",
            "Machine Learning CO-OP",
        ]:
            with self.subTest(spelling=spelling):
                self.assertEqual(tier(spelling), STRICT)


class TestAiNativeCompanyPromotion(unittest.TestCase):
    """A generic title at an AI-native company still earns an instant ping."""

    def test_generic_title_at_ai_native_company_is_strict(self):
        self.assertEqual(
            tier("Software Engineer Intern", company="Cohere", ai_native=True), STRICT
        )

    def test_same_title_elsewhere_is_loose(self):
        self.assertEqual(
            tier("Software Engineer Intern", company="Faire", ai_native=False), LOOSE
        )

    def test_ai_native_still_needs_student_signal(self):
        self.assertIsNone(
            tier("Staff Software Engineer", company="Cohere", ai_native=True)
        )

    def test_ai_native_still_needs_canada_signal(self):
        self.assertEqual(
            tier("Software Engineer Intern", location="San Francisco, CA",
                 company="Cohere", ai_native=True),
            None,
        )


class TestLooseTier(unittest.TestCase):
    """Ambiguous goes to loose, never to the bin."""

    def test_opaque_rotational_title(self):
        self.assertEqual(tier("Technology Analyst, Rotational"), LOOSE)

    def test_generic_swe_intern(self):
        self.assertEqual(tier("Software Engineer Intern"), LOOSE)

    def test_business_intern_in_canada(self):
        self.assertEqual(tier("Product Management Intern"), LOOSE)

    def test_blank_location_never_binned(self):
        self.assertEqual(tier("Machine Learning Intern", location=""), LOOSE)

    def test_unparseable_location_goes_loose(self):
        self.assertEqual(tier("ML Intern", location="Multiple Locations"), LOOSE)

    def test_summer_2027_demoted_out_of_strict(self):
        self.assertEqual(tier("Machine Learning Intern, Summer 2027"), LOOSE)

    def test_later_cycle_is_never_rejected(self):
        self.assertIsNotNone(tier("Data Science Intern (S27)"))


class TestPastCycles(unittest.TestCase):
    """Cycles earlier than the target are rejected: that recruiting is over.

    Target is Winter 2027 (companies.TARGET_CYCLE = (2027, 1)).
    """

    def test_fall_2026_is_rejected(self):
        self.assertIsNone(tier("AI Research Intern - Fall 2026"))

    def test_summer_2026_is_rejected(self):
        self.assertIsNone(tier("Machine Learning Intern, Summer 2026"))

    def test_winter_2026_is_rejected(self):
        self.assertIsNone(tier("ML Intern (Winter 2026)"))

    def test_short_form_past_cycle_is_rejected(self):
        self.assertIsNone(tier("Data Science Intern F26"))

    def test_winter_2027_still_strict(self):
        self.assertEqual(tier("ML Intern, Winter 2027"), STRICT)

    def test_future_cycle_is_only_demoted(self):
        self.assertEqual(tier("ML Intern, Fall 2027"), LOOSE)

    def test_multi_cycle_posting_keeps_the_best_one(self):
        """A req spanning several terms is live if any of them is the target."""
        self.assertEqual(
            tier("Machine Learning Intern - Summer 2026, Winter 2027, Fall 2027"),
            STRICT,
        )

    def test_multi_cycle_all_past_is_rejected(self):
        self.assertIsNone(tier("ML Intern - Summer 2026, Fall 2026"))

    def test_no_cycle_named_is_still_eligible(self):
        self.assertEqual(tier("Machine Learning Intern"), STRICT)

    def test_bare_year_does_not_reject(self):
        """"2026 Intern" has no season, so it is too ambiguous to drop."""
        self.assertIsNotNone(tier("2026 Intern, Research Scientist"))


class TestCycleOutsideTitle(unittest.TestCase):
    """Trackers hide the work term in a details column, not the title.

    Regression: a SAP iXp intern role whose only cycle signal was
    "Intern - 4mo - $18-$36/hr - Fall 2026" in the details column sailed
    through as strict, and showed up on the dashboard a month after it closed.
    """

    def test_fall_2026_in_details_is_rejected(self):
        post = p(
            "SAP iXp Intern - Software Developer (Agentic AI & LLM systems)",
            location="Montreal, Quebec (Hybrid)",
            company="SAP",
            raw={"details": "Intern - 4mo - $18-$36/hr - Fall 2026"},
        )
        self.assertIsNone(classify(post).tier)

    def test_cycle_in_terms_field_is_read(self):
        post = p("Machine Learning Intern", raw={"terms": ["Fall 2026"]})
        self.assertIsNone(classify(post).tier)

    def test_target_cycle_in_details_still_strict(self):
        post = p("Machine Learning Intern",
                 raw={"details": "Co-op - 8mo - Winter 2027"})
        self.assertEqual(classify(post).tier, STRICT)

    def test_details_without_a_cycle_changes_nothing(self):
        post = p("Machine Learning Intern", raw={"details": "Co-op - 8mo - $20/hr"})
        self.assertEqual(classify(post).tier, STRICT)


class TestCycleParsing(unittest.TestCase):
    def test_parses_long_and_short_forms(self):
        from core import parse_cycles

        self.assertEqual(parse_cycles("Winter 2027"), [(2027, 1)])
        self.assertEqual(parse_cycles("W27"), [(2027, 1)])
        self.assertEqual(parse_cycles("Fall 2026"), [(2026, 9)])
        self.assertEqual(parse_cycles("Summer '27"), [(2027, 5)])

    def test_parses_multiple_sorted(self):
        from core import parse_cycles

        self.assertEqual(
            parse_cycles("Summer 2026, Winter 2027, Fall 2027"),
            [(2026, 5), (2027, 1), (2027, 9)],
        )

    def test_no_cycle_returns_empty(self):
        from core import parse_cycles

        self.assertEqual(parse_cycles("Machine Learning Intern"), [])
        self.assertEqual(parse_cycles(""), [])


class TestFreshness(unittest.TestCase):
    """Postings older than MAX_AGE_HOURS are recorded but never surfaced."""

    def test_recent_posting_is_fresh(self):
        import radar

        self.assertTrue(radar.is_fresh(int(time.time()) - 3600))

    def test_posting_older_than_the_window_is_stale(self):
        import companies as cfg
        import radar

        older = int(time.time()) - (cfg.MAX_AGE_HOURS + 48) * 3600
        self.assertFalse(radar.is_fresh(older))

    def test_unknown_date_counts_as_fresh(self):
        """Several sources expose no date; dropping them all would be worse."""
        import radar

        self.assertTrue(radar.is_fresh(0))

    def test_boundary_just_inside_window(self):
        import companies as cfg
        import radar

        self.assertTrue(
            radar.is_fresh(int(time.time()) - cfg.MAX_AGE_HOURS * 3600 + 120)
        )

    def test_boundary_just_outside_window(self):
        import companies as cfg
        import radar

        self.assertFalse(
            radar.is_fresh(int(time.time()) - cfg.MAX_AGE_HOURS * 3600 - 120)
        )


class TestDayLabels(unittest.TestCase):
    """Ages are shown in calendar days, newest first."""

    def setUp(self):
        import notify

        self.label = notify._day_label
        self.now = int(time.time())

    def test_just_posted_is_today(self):
        self.assertEqual(self.label(self.now)[0], "Today")

    def test_a_few_hours_ago_is_still_today(self):
        self.assertEqual(self.label(self.now - 3 * 3600)[0], "Today")

    def test_thirty_hours_ago_is_yesterday_not_today(self):
        """Calendar days, not 24-hour blocks: 11pm last night is Yesterday."""
        self.assertEqual(self.label(self.now - 30 * 3600)[1], 1)

    def test_multiple_days_are_counted(self):
        self.assertEqual(self.label(self.now - 4 * 86400), ("4 days ago", 4))

    def test_today_is_flagged_for_highlighting(self):
        self.assertEqual(self.label(self.now)[1], 0)

    def test_bad_timestamps_do_not_crash(self):
        for ts in (0, -1, 99_999_999_999_999):
            with self.subTest(ts=ts):
                label, days = self.label(ts)
                self.assertIsInstance(label, str)
                self.assertIsInstance(days, int)


class TestSecondaryLocations(unittest.TestCase):
    """Ashby lists a US primary with Toronto in secondaryLocations."""

    def test_toronto_in_secondary_locations_reaches_strict(self):
        post = p(
            "Machine Learning Intern",
            location="New York",
            raw={
                "location": "New York",
                "secondaryLocations": [
                    {"location": "San Francisco"},
                    {"location": "Toronto",
                     "address": {"postalAddress": {"addressCountry": "Canada"}}},
                ],
            },
        )
        self.assertEqual(classify(post).tier, STRICT)

    def test_us_only_secondary_locations_still_rejected(self):
        post = p(
            "Machine Learning Intern",
            location="New York",
            raw={
                "location": "New York",
                "secondaryLocations": [{"location": "San Francisco, CA"}],
            },
        )
        self.assertIsNone(classify(post).tier)

    def test_is_remote_flag_counts_as_location_signal(self):
        post = p("AI Intern", location="", raw={"isRemote": True})
        self.assertEqual(classify(post).tier, STRICT)


class TestRejects(unittest.TestCase):
    """Reject only: unpaid, volunteer, high school, PhD-only."""

    def test_unpaid(self):
        self.assertIsNone(tier("Unpaid Machine Learning Intern"))

    def test_volunteer(self):
        self.assertIsNone(tier("Volunteer AI Research Intern"))

    def test_high_school(self):
        self.assertIsNone(tier("High School Summer Intern, AI"))

    def test_phd_only(self):
        self.assertIsNone(tier("Research Intern (PhD only)"))

    def test_plain_phd_mention_is_not_rejected(self):
        """Plenty of ML internships prefer a PhD but accept master's students."""
        self.assertEqual(tier("PhD Machine Learning Research Intern"), STRICT)

    def test_non_student_role_is_binned(self):
        self.assertIsNone(tier("Senior Machine Learning Engineer"))

    def test_explicit_us_location_is_binned(self):
        self.assertIsNone(tier("ML Intern", location="Seattle, WA"))

    def test_explicit_india_location_is_binned(self):
        self.assertIsNone(tier("ML Intern", location="Bangalore, India"))


class TestFalsePositiveGuards(unittest.TestCase):
    """Short tokens like 'ai' and 'ml' must not match inside other words."""

    def test_email_does_not_match_ai(self):
        self.assertEqual(tier("Email Marketing Intern"), LOOSE)

    def test_html_does_not_match_ml(self):
        self.assertEqual(tier("HTML Developer Intern"), LOOSE)

    def test_retail_does_not_match_ai(self):
        self.assertEqual(tier("Retail Operations Intern"), LOOSE)

    def test_maintenance_does_not_match_ai(self):
        self.assertEqual(tier("Maintenance Technician Intern"), LOOSE)


class TestCanadianCities(unittest.TestCase):
    def test_cities_and_provinces(self):
        for location in [
            "Toronto, ON",
            "Toronto, Ontario",
            "Waterloo, ON, Canada",
            "Montréal, QC",
            "Vancouver, British Columbia",
            "Ottawa, Canada",
            "Mississauga, ON",
            "Calgary, AB",
        ]:
            with self.subTest(location=location):
                self.assertEqual(tier("ML Intern", location=location), STRICT)


if __name__ == "__main__":
    unittest.main()
