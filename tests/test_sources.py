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


class TestOracle(unittest.TestCase):
    """Shapes from Definity's Oracle Cloud Recruiting search."""

    TOKEN = "https://hdks.fa.ca2.oraclecloud.com|CX_1"
    REQ = {
        "Id": "9347", "Title": "Claims Assistant - Winter 2027 Co-op/Intern",
        "PrimaryLocation": "Waterloo, ONT, Canada", "PostedDate": "2026-09-18",
        "secondaryLocations": [{"Name": "Toronto, ONT, Canada"}], "WorkplaceType": "",
    }

    def test_requisition_becomes_a_posting(self):
        http = mock.Mock()
        http.json.return_value = {"items": [{"TotalJobsCount": 1, "requisitionList": [self.REQ]}]}
        [post] = sources.oracle(http, "Definity", self.TOKEN)
        self.assertEqual(http.json.call_count, len(sources.SEARCH_TERMS))
        self.assertEqual(post.uid, "oracle:hdks.fa.ca2.oraclecloud.com:9347")
        self.assertEqual(post.url, "https://hdks.fa.ca2.oraclecloud.com/hcmUI/"
                                   "CandidateExperience/en/sites/CX_1/job/9347")
        self.assertEqual(post.location, "Waterloo, ONT, Canada, Toronto, ONT, Canada")
        self.assertGreater(post.posted_at, 0)
        finder = http.json.call_args.kwargs["params"]["finder"]
        self.assertIn("siteNumber=CX_1", finder)


class TestJibe(unittest.TestCase):
    """Shapes from AMD's careers site (iCIMS Jibe)."""

    JOB = {"data": {
        "req_id": "91368", "title": "Short Term 2027 Software Engineering Intern/Co-Op",
        "full_location": "MARKHAM, Canada", "city": "MARKHAM", "state": "Ontario",
        "country": "Canada", "posted_date": "2026-09-01T07:11:00+0000",
        "canonical_url": "https://careers.amd.com/jobs/91368?lang=en-us",
        "description": "Offices in Austin, Texas and Toronto.",
    }}

    def test_job_becomes_a_posting_without_the_description(self):
        http = mock.Mock()
        http.json.return_value = {"totalCount": 1, "jobs": [self.JOB]}
        [post] = sources.jibe(http, "AMD", "https://careers.amd.com/")
        self.assertEqual(post.uid, "jibe:careers.amd.com:91368")
        self.assertEqual(post.location, "MARKHAM, Canada, MARKHAM, Ontario, Canada")
        self.assertNotIn("description", post.raw)
        self.assertGreater(post.posted_at, 0)

    def test_request_asks_for_english_us(self):
        http = mock.Mock()
        http.json.return_value = {}
        sources.jibe(http, "AMD", "https://careers.amd.com")
        kwargs = http.json.call_args.kwargs
        self.assertEqual(kwargs["params"]["location"], "Canada")
        self.assertTrue(kwargs["headers"]["Accept-Language"].startswith("en-US"))


class TestEightfold(unittest.TestCase):
    """Shapes from Qualcomm's PCSX search."""

    POS = {"id": 446721064018, "name": "FY27 Intern - Machine Learning Compiler Intern",
           "locations": ["Markham, Ontario, Canada"], "postedTs": 1789603200,
           "positionUrl": "/careers/job/446721064018", "workLocationOption": "onsite"}

    def test_position_becomes_a_posting(self):
        http = mock.Mock()
        http.json.return_value = {"data": {"count": 1, "positions": [self.POS]}}
        [post] = sources.eightfold(http, "Qualcomm", "qualcomm.eightfold.ai|qualcomm.com")
        self.assertEqual(post.uid, "eightfold:qualcomm.eightfold.ai:446721064018")
        self.assertEqual(post.url, "https://qualcomm.eightfold.ai/careers/job/446721064018")
        self.assertEqual(post.location, "Markham, Ontario, Canada")
        self.assertEqual(http.json.call_args.kwargs["params"]["domain"], "qualcomm.com")

    def test_domain_defaults_to_tenant_dot_com(self):
        http = mock.Mock()
        http.json.return_value = {}
        sources.eightfold(http, "Qualcomm", "qualcomm.eightfold.ai")
        self.assertEqual(http.json.call_args.kwargs["params"]["domain"], "qualcomm.com")


class TestIcims(unittest.TestCase):
    """Both card templates seen live: Kinaxis ("Location") and Mackenzie ("Job Locations")."""

    KINAXIS = """
<li class="iCIMS_JobCardItem"><div class="row">
<div class="col-xs-6 header left"><span class="sr-only field-label">Location</span>
<span >
CA-Remote</span></div>
<div class="col-xs-12 title">
<a href="https://careers-kinaxis.icims.com/jobs/35377/co-op-intern-developer%2c-ai/job?in_iframe=1" class="iCIMS_Anchor" title="35377">
<span class="sr-only field-label">Title</span><h3 >
Co-op/Intern Developer, AI Innovation</h3></a></div>
<dl class="iCIMS_JobHeaderGroup"><div class="iCIMS_JobHeaderTag">
<dt class="iCIMS_JobHeaderField">Posted Date</dt>
<dd class="iCIMS_JobHeaderData"><span title="9/29/2026 5:30 PM">1 day ago</span></dd></div>
<div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">
<span class="sr-only field-label">Additional Locations</span></dt>
<dd class="iCIMS_JobHeaderData"><span >
CA-ON-Ottawa | CA-ON-Toronto</span></dd></div></dl>
</div></li>
<link rel="next" href="https://careers-kinaxis.icims.com/jobs/search?pr=1&amp;in_iframe=1" />"""

    MACKENZIE = """
<li class="iCIMS_JobCardItem"><div class="row">
<div class="col-xs-6 header left"><span class="sr-only field-label">Job Locations</span>
<span >
CA-ON-Greater Toronto Area</span></div>
<div class="col-xs-6 header right"><span class="sr-only field-label">Posted Date</span>
<span title="9/30/2026 3:08 PM">
3 hours ago</span></div>
<div class="col-xs-12 title">
<a href="https://careersen-mackenzieinvestments.icims.com/jobs/6014/winter-intern/job?in_iframe=1" class="iCIMS_Anchor" title="6014">
<span class="sr-only field-label">Job Title</span><h3 >
Winter Intern 2027 - AI Strategy &amp; Enablement</h3></a></div>
</div></li>"""

    def test_location_codes_are_spelled_out(self):
        self.assertEqual(sources._icims_place("CA-ON-Ottawa"), "Ottawa, ON, Canada")
        self.assertEqual(sources._icims_place("CA-Remote"), "Remote, Canada")
        self.assertEqual(sources._icims_place("Toronto"), "Toronto")

    def test_first_template(self):
        [card] = sources._icims_cards(self.KINAXIS)
        self.assertEqual(card["id"], "35377")
        self.assertEqual(card["title"], "Co-op/Intern Developer, AI Innovation")
        self.assertEqual(card["location"],
                         "Remote, Canada, Ottawa, ON, Canada, Toronto, ON, Canada")
        self.assertNotIn("in_iframe", card["url"])
        self.assertGreater(card["posted_at"], 0)

    def test_second_template(self):
        [card] = sources._icims_cards(self.MACKENZIE)
        self.assertEqual(card["title"], "Winter Intern 2027 - AI Strategy & Enablement")
        self.assertEqual(card["location"], "Greater Toronto Area, ON, Canada")
        self.assertGreater(card["posted_at"], 0)

    def test_pages_until_no_next_link_with_own_user_agent(self):
        http = mock.Mock()
        http.text.side_effect = [self.KINAXIS, self.MACKENZIE]
        posts = sources.icims(http, "Kinaxis", "https://careers-kinaxis.icims.com")
        self.assertEqual(http.text.call_count, 2)
        self.assertEqual(len(posts), 2)
        self.assertNotIn("KHTML", http.text.call_args.kwargs["headers"]["User-Agent"])


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


class TestSniffNewPlatforms(unittest.TestCase):
    def test_signatures_become_adapter_tokens(self):
        html = """
        <a href="https://careers-kinaxis.icims.com/jobs/intro">Jobs</a>
        <script src="https://static.icims.com/x.js"></script>
        <a href="https://qualcomm.eightfold.ai/careers">Careers</a>
        <a href="https://hdks.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions">Apply</a>
        """
        hits = sources._sniff_hits("https://www.example.com/careers", html)
        self.assertIn(("icims", "https://careers-kinaxis.icims.com"), hits)
        self.assertIn(("eightfold", "qualcomm.eightfold.ai|qualcomm.com"), hits)
        self.assertIn(("oracle", "https://hdks.fa.ca2.oraclecloud.com|CX_1"), hits)
        self.assertNotIn(("icims", "https://static.icims.com"), hits)

    def test_jibe_board_is_the_page_host(self):
        html = '<script src="https://app.jibecdn.com/prod/search.js"></script>'
        hits = sources._sniff_hits("https://careers.amd.com/careers-home/jobs", html)
        self.assertEqual(hits, [("jibe", "https://careers.amd.com")])

    def test_sniff_boards_drops_platforms_without_an_adapter(self):
        http = mock.Mock()
        http.text.return_value = '<a href="https://acme.successfactors.com/career">x</a>'
        self.assertEqual(sources.sniff_boards(http, "https://acme.com/careers"), [])


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
