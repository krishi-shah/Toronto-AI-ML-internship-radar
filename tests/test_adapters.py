"""Tests for the adapters added for wider coverage: portfolio job boards
(Getro, Consider), SuccessFactors career sites, and the startup ATSes found
behind Canadian tracker postings (BambooHR, Rippling, JazzHR).

Fixtures are trimmed copies of what each live board returned on 17 Oct 2026.
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sources  # noqa: E402
from core import STRICT, classify  # noqa: E402


def response(payload):
    resp = mock.Mock()
    resp.json.return_value = payload
    return resp


GETRO_PAGE = (
    '<script id="__NEXT_DATA__" type="application/json">'
    + json.dumps({"props": {"pageProps": {"network": {"id": "1201"}}}})
    + "</script>"
)
GETRO_JOB = {
    "id": 97100001,
    "title": "ML Engineering Intern - Infrastructure",
    "url": "https://jobs.ashbyhq.com/peripheral/3f1c2d4e-1111-4222-8333-944455556666",
    "locations": ["Toronto, ON, Canada"],
    "work_mode": "on_site",
    "seniority": "internship",
    "created_at": 1791975491,
    "slug": "97100001-ml-engineering-intern",
    "organization": {"name": "Peripheral Labs", "slug": "peripheral-labs"},
}


class TestGetro(unittest.TestCase):
    def fetch(self, pages, count):
        http = mock.Mock()
        http.text.return_value = GETRO_PAGE
        http.request.side_effect = [
            response({"results": {"jobs": jobs, "count": count}}) for jobs in pages
        ]
        return http, sources.getro(http, "Inovia", "careers.inovia.vc")

    def test_postings_carry_the_portfolio_company(self):
        _, [post] = self.fetch([[GETRO_JOB]], 1)
        self.assertEqual(post.company, "Peripheral Labs")
        self.assertEqual(post.uid, "getro:careers.inovia.vc:97100001")
        self.assertEqual(post.source, "Inovia [getro]")
        self.assertTrue(post.url.startswith("https://jobs.ashbyhq.com/peripheral/"))
        self.assertEqual(classify(post).tier, STRICT)

    def test_search_uses_the_network_id_and_a_canada_filter(self):
        http, _ = self.fetch([[GETRO_JOB]], 1)
        url = http.request.call_args[0][1]
        body = http.request.call_args[1]["json"]
        self.assertEqual(url, "https://api.getro.com/api/v2/collections/1201/search/jobs")
        self.assertEqual(body["filters"], {"searchable_locations": ["Canada"]})

    def test_pages_until_the_count_is_reached(self):
        http, posts = self.fetch([[GETRO_JOB] * 20, [GETRO_JOB] * 5], 25)
        self.assertEqual(http.request.call_count, 2)
        self.assertEqual(len(posts), 25)

    def test_page_without_board_data_raises(self):
        http = mock.Mock()
        http.text.return_value = "<html>not a getro board</html>"
        with self.assertRaises(RuntimeError):
            sources.getro(http, "X", "jobs.example.com")


CONSIDER_PAGE = ('<script>window.serverInitialData = {"csrfToken":"tok-123",'
                 '"board":{"id":"golden-ventures","isParent":true},"x":1}</script>')
CONSIDER_JOB = {
    "jobId": "4400001", "title": "Machine Learning Intern", "companyName": "Ada",
    "url": "https://job-boards.greenhouse.io/ada18/jobs/4400001",
    "locations": ["Toronto, ON"], "remote": False, "timeStamp": "2026-10-16T16:24:39Z",
    "jobSeniorities": [{"label": "Internship"}],
}


class TestConsider(unittest.TestCase):
    def test_csrf_token_and_board_reach_the_search(self):
        http = mock.Mock()
        http.text.return_value = CONSIDER_PAGE
        http.request.return_value = response({"jobs": [CONSIDER_JOB], "total": 1, "meta": {}})
        [post] = sources.consider(http, "Golden Ventures", "jobs.golden.ventures")
        kwargs = http.request.call_args[1]
        self.assertEqual(kwargs["headers"]["x-csrf-token"], "tok-123")
        self.assertEqual(kwargs["json"]["board"], {"id": "golden-ventures", "isParent": True})
        self.assertEqual(post.company, "Ada")
        self.assertEqual(post.uid, "consider:jobs.golden.ventures:4400001")
        self.assertGreater(post.posted_at, 0)

    def test_sequence_cursor_pages_onward(self):
        http = mock.Mock()
        http.text.return_value = CONSIDER_PAGE
        http.request.side_effect = [
            response({"jobs": [CONSIDER_JOB] * 100, "total": 101, "meta": {"sequence": "abc"}}),
            response({"jobs": [CONSIDER_JOB], "total": 101, "meta": {}}),
        ]
        posts = sources.consider(http, "Golden Ventures", "jobs.golden.ventures")
        self.assertEqual(len(posts), 101)
        self.assertEqual(http.request.call_args[1]["json"]["meta"]["sequence"], "abc")


SF_ROW = '''<tr class="data-row">
<td class="colTitle"><span class="jobTitle hidden-phone">
<a href="/job/Toronto-Velocity-Corporate-Security-InternshipCo-op-Winter-2027-ON/605881917/"
   class="jobTitle-link">Velocity - Corporate Security Internship/Co-op - Winter 2027</a></span>
</td><td class="colDate"><span class="jobDate">Oct 7, 2026
</span></td><td class="colLocation"><span class="jobLocation">
    Toronto, ON, CA
</span></td></tr>'''
SF_ROW_NO_PLACE = '''<tr class="data-row"><td>
<a href="/job/Brampton-Data-Analyst-Co-op-ON-L6T-4X6/1393997900/" class="jobTitle-link">Data Analyst Co-op</a>
</td></tr>'''


class TestSuccessFactors(unittest.TestCase):
    def page(self, rows, total):
        return f"Results <b>1 – {len(rows)}</b> of <b>{total}</b><table>{''.join(rows)}</table>"

    def test_row_is_parsed(self):
        http = mock.Mock()
        http.text.return_value = self.page([SF_ROW], 1)
        [post] = sources.successfactors(
            http, "Scotiabank", "https://jobs.scotiabank.com/search/?q=internship&locationsearch=Ontario")
        self.assertEqual(post.title, "Velocity - Corporate Security Internship/Co-op - Winter 2027")
        self.assertEqual(post.location, "Toronto, ON, CA")
        self.assertEqual(post.uid, "successfactors:jobs.scotiabank.com:605881917")
        self.assertTrue(post.url.startswith("https://jobs.scotiabank.com/job/Toronto-"))
        self.assertGreater(post.posted_at, 0)

    def test_place_falls_back_to_the_posting_path(self):
        http = mock.Mock()
        http.text.return_value = self.page([SF_ROW_NO_PLACE], 1)
        [post] = sources.successfactors(http, "Rogers", "https://jobs.rogers.com/search/?q=intern")
        self.assertEqual(post.location, "Brampton, ON")

    def test_pages_with_startrow(self):
        http = mock.Mock()
        first = self.page([SF_ROW.replace("605881917", str(n)) for n in range(25)], 30)
        http.text.side_effect = [first, self.page([SF_ROW_NO_PLACE], 30)]
        posts = sources.successfactors(http, "Rogers", "https://jobs.rogers.com/search/?q=intern")
        self.assertEqual(len(posts), 26)
        self.assertTrue(http.text.call_args[0][0].endswith("&startrow=25"))

    def test_sniffed_from_its_asset_host(self):
        hits = sources._sniff_hits(
            "https://jobs.l3harris.com/job/Waterdown-Co-Op/1/",
            '<link href="https://rmkcdn.successfactors.com/6543df76/a.css">')
        self.assertEqual(hits, [
            ("successfactors", "https://jobs.l3harris.com/search/?q=&locationsearch=Ontario")])


class TestStartupAts(unittest.TestCase):
    def test_bamboohr(self):
        http = mock.Mock()
        http.json.return_value = {"result": [{
            "id": "43", "jobOpeningName": "Machine Learning Intern",
            "employmentStatusLabel": "Intern", "location": {"city": "Toronto", "state": "Ontario"},
            "atsLocation": {}, "isRemote": None}]}
        [post] = sources.bamboohr(http, "Signal 1", "signal1")
        self.assertEqual(post.url, "https://signal1.bamboohr.com/careers/43")
        self.assertEqual(post.location, "Toronto, Ontario")
        self.assertEqual(post.uid, "bamboohr:signal1:43")

    def test_rippling(self):
        http = mock.Mock()
        http.json.return_value = {"totalPages": 1, "items": [{
            "id": "e52b9e0d", "name": "ML Research Intern",
            "url": "https://ats.rippling.com/fluidai-medical-careers/jobs/e52b9e0d",
            "locations": [{"city": "Kitchener", "stateCode": "ON", "country": "Canada",
                           "workplaceType": "ON_SITE"}]}]}
        [post] = sources.rippling(http, "Fluid AI", "fluidai-medical-careers")
        self.assertEqual(post.location, "Kitchener, ON, Canada")
        self.assertEqual(classify(post).tier, STRICT)

    def test_jazzhr(self):
        http = mock.Mock()
        http.text.return_value = (
            "<ul class='list-group'><li class=\"list-group-item\"><h3>"
            '<a href="https://techinsights.applytojob.com/apply/bKNlLKEsBR/Circuit-Analysis-Coop">'
            " Circuit Analysis Co-op Student (Winter 2027) </a></h3>"
            "<ul class='list-inline'><li><i class='fa fa-map-marker'></i>Ottawa, ON, Canada</li></ul>"
            "</li></ul>")
        [post] = sources.jazzhr(http, "TechInsights", "techinsights")
        self.assertEqual(post.title, "Circuit Analysis Co-op Student (Winter 2027)")
        self.assertEqual(post.location, "Ottawa, ON, Canada")
        self.assertEqual(post.uid, "jazzhr:techinsights:bKNlLKEsBR")

    def test_tracker_links_lead_to_the_boards(self):
        for url, board in [
            ("https://signal1.bamboohr.com/careers/43", ("bamboohr", "signal1")),
            ("https://ats.rippling.com/fluidai-medical-careers/jobs/e52b", ("rippling", "fluidai-medical-careers")),
            ("https://techinsights.applytojob.com/apply/bKNl/x", ("jazzhr", "techinsights")),
        ]:
            with self.subTest(url=url):
                self.assertEqual(sources.board_from_url(url), board)


class TestCappedSearches(unittest.TestCase):
    def test_workday_search_at_its_cap_is_partial(self):
        def page(*_args, json, **_kwargs):
            jobs = [{"title": "Intern", "bulletFields": [f"R{json['offset']}-{i}"],
                     "externalPath": "/job/x"} for i in range(sources.WORKDAY_PAGE)]
            return response({"total": 5000, "jobPostings": jobs})

        http = mock.Mock()
        http.request.side_effect = page
        with mock.patch.object(sources, "WORKDAY_TERMS", ["intern"]):
            posts = sources.workday(http, "Acme", "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/Ext/jobs")
        self.assertIsInstance(posts, sources.Partial)

    def test_workday_search_that_ends_is_complete(self):
        http = mock.Mock()
        http.request.return_value = response({"total": 1, "jobPostings": [
            {"title": "Intern", "bulletFields": ["R1"], "externalPath": "/job/x"}]})
        with mock.patch.object(sources, "WORKDAY_TERMS", ["intern"]):
            posts = sources.workday(http, "Acme", "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/Ext/jobs")
        self.assertNotIsInstance(posts, sources.Partial)

    def test_careers_page_scrape_is_always_partial(self):
        http = mock.Mock()
        http.text.return_value = '<a href="/careers/job/1">ML Intern</a>'
        self.assertIsInstance(sources.html_links(http, "Acme", "https://acme.ai/careers/"),
                              sources.Partial)


if __name__ == "__main__":
    unittest.main()
