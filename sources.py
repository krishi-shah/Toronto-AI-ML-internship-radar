"""Source adapters: ATS JSON APIs, HTML careers-page fallback, and community
trackers. Plus the ``--sniff`` helper that turns a careers URL into a config
line.

Every adapter takes a company display name and a platform token, and returns a
list of :class:`core.Posting`. Adapters raise on failure; the orchestrator in
``radar.py`` records the exception against the source and keeps going, so one
dead board never fails a run.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from html import unescape
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests

from core import Posting

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 25
MAX_RETRIES = 3
POLITENESS_GAP = 0.4  # seconds between requests to the same host


class Http:
    """Shared HTTP client: real User-Agent, backoff, per-host politeness.

    Several of these endpoints reject the stock ``python-requests`` agent
    outright, so the browser UA is not optional.

    ``etag_store`` is used for conditional requests and nothing else. Pass it
    ONLY from a caller that records every posting it receives. A 304 means
    "unchanged since the last fetch", so if a fetch stored an etag without
    recording its postings, the next fetch skips them and they are lost --
    which is exactly how a ``--check`` before a ``--seed`` silently produced
    an empty seed. ``--check`` and ``--seed`` therefore pass nothing.
    """

    def __init__(self, etag_store: Any = None) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-CA,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self.store = etag_store
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._guard = threading.Lock()

    def _throttle(self, url: str) -> threading.Lock:
        host = urlparse(url).netloc
        with self._guard:
            lock = self._host_locks.setdefault(host, threading.Lock())
        lock.acquire()
        last = self._host_last.get(host, 0.0)
        wait = POLITENESS_GAP - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        return lock

    def _release(self, url: str, lock: threading.Lock) -> None:
        self._host_last[urlparse(url).netloc] = time.time()
        lock.release()

    def request(
        self,
        method: str,
        url: str,
        *,
        etag_key: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[requests.Response]:
        """Issue a request with retry/backoff. Returns ``None`` on HTTP 304."""
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        headers = dict(kwargs.pop("headers", {}) or {})

        if etag_key and self.store is not None:
            prev = self.store.get_etag(etag_key)
            if prev:
                headers["If-None-Match"] = prev

        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            lock = self._throttle(url)
            try:
                resp = self.session.request(method, url, headers=headers, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                self._release(url, lock)
                time.sleep(2**attempt)
                continue
            else:
                self._release(url, lock)

            if resp.status_code == 304:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = 2**attempt
                if retry_after and retry_after.isdigit():
                    delay = min(int(retry_after), 30)
                last_exc = requests.HTTPError(f"HTTP {resp.status_code} for {url}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
                    continue
                raise last_exc

            resp.raise_for_status()
            if etag_key and self.store is not None and resp.headers.get("ETag"):
                self.store.set_etag(etag_key, resp.headers["ETag"])
            return resp

        raise last_exc or RuntimeError(f"request failed: {url}")

    def get(self, url: str, **kwargs: Any) -> Optional[requests.Response]:
        return self.request("GET", url, **kwargs)

    def json(self, url: str, **kwargs: Any) -> Any:
        resp = self.get(url, **kwargs)
        return None if resp is None else resp.json()

    def text(self, url: str, **kwargs: Any) -> Optional[str]:
        resp = self.get(url, **kwargs)
        return None if resp is None else resp.text


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _date(*candidates: Any) -> int:
    """First parseable publish date among ``candidates``, as Unix seconds.

    Sources disagree wildly: ISO-8601 strings, epoch seconds, epoch
    milliseconds, and Workday's prose ("Posted 30+ Days Ago"). Returns 0 when
    nothing parses, which callers treat as "unknown, assume fresh".
    """
    for value in candidates:
        ts = _one_date(value)
        if ts:
            return ts
    return 0


_WORKDAY_AGE_RE = re.compile(r"(\d+)\+?\s*day", re.IGNORECASE)


def _one_date(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # epoch milliseconds
            ts /= 1000.0
        return int(ts) if 946_684_800 < ts < 4_102_444_800 else 0

    if not isinstance(value, str):
        return 0
    text = value.strip()
    if not text:
        return 0

    if text.lstrip("-").isdigit():
        return _one_date(float(text))

    lowered = text.lower()
    if "today" in lowered or "just posted" in lowered:
        return int(time.time())
    if "yesterday" in lowered:
        return int(time.time()) - 86400
    m = _WORKDAY_AGE_RE.search(lowered)
    if m:
        return int(time.time()) - int(m.group(1)) * 86400

    iso = text.replace("Z", "+00:00")
    for candidate in (iso, iso[:19], iso[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return 0


def _s(val: Any) -> str:
    """Coerce a possibly-missing JSON value to a clean string."""
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return ", ".join(_s(v) for v in val if v)
    if isinstance(val, dict):
        for key in ("name", "text", "label", "city", "location", "title"):
            if val.get(key):
                return _s(val[key])
        return ""
    return str(val).strip()


def _join(*parts: Any) -> str:
    seen: list[str] = []
    for p in parts:
        t = _s(p)
        if t and t not in seen:
            seen.append(t)
    return ", ".join(seen)


# --------------------------------------------------------------------------
# Layer 1: ATS adapters
#
# Shapes below were verified against live boards, not taken on trust.
# --------------------------------------------------------------------------


def ashby(http: Http, company: str, token: str) -> list[Posting]:
    """Ashby posting API. Verified against ``cohere``.

    Ashby frequently lists a US primary location with Toronto tucked into
    ``secondaryLocations``, so both are carried into ``raw`` for the classifier.
    """
    data = http.json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    out = []
    for job in (data or {}).get("jobs", []):
        if job.get("isListed") is False:
            continue
        jid = _s(job.get("id"))
        secondary = [
            _s(loc.get("location")) for loc in job.get("secondaryLocations") or []
        ]
        out.append(
            Posting(
                company=company,
                title=_s(job.get("title")),
                location=_join(job.get("location"), *secondary),
                url=_s(job.get("jobUrl"))
                or _s(job.get("applyUrl"))
                or f"https://jobs.ashbyhq.com/{token}/{jid}",
                uid=f"ashby:{token}:{jid}",
                raw=job,
                posted_at=_date(job.get("publishedAt"), job.get("updatedAt")),
            )
        )
    return out


def greenhouse(http: Http, company: str, token: str) -> list[Posting]:
    """Greenhouse job board API. Verified against ``faire`` and ``tenstorrent``."""
    data = http.json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    out = []
    for job in (data or {}).get("jobs", []):
        jid = _s(job.get("id"))
        out.append(
            Posting(
                company=company,
                title=_s(job.get("title")),
                location=_join(job.get("location"), job.get("offices")),
                url=_s(job.get("absolute_url"))
                or f"https://boards.greenhouse.io/{token}/jobs/{jid}",
                uid=f"greenhouse:{token}:{jid}",
                raw=job,
                posted_at=_date(job.get("first_published"), job.get("updated_at")),
            )
        )
    return out


def lever(http: Http, company: str, token: str) -> list[Posting]:
    """Lever postings API. Verified against ``waabi`` and ``benchsci``."""
    data = http.json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for job in data or []:
        cats = job.get("categories") or {}
        jid = _s(job.get("id"))
        out.append(
            Posting(
                company=company,
                title=_s(job.get("text")),
                location=_join(
                    cats.get("location"),
                    job.get("workplaceType"),
                    cats.get("allLocations"),
                    cats.get("commitment"),
                ),
                url=_s(job.get("hostedUrl")) or _s(job.get("applyUrl")),
                uid=f"lever:{token}:{jid}",
                raw=job,
                posted_at=_date(job.get("createdAt"), job.get("updatedAt")),
            )
        )
    return out


def smartrecruiters(http: Http, company: str, token: str) -> list[Posting]:
    """SmartRecruiters public postings. Shape ``{"content": [...]}`` verified.

    The token is case-sensitive and is the company identifier, not the display
    name -- ``--sniff`` prints the right one.
    """
    out: list[Posting] = []
    offset = 0
    while True:
        data = http.json(
            "https://api.smartrecruiters.com/v1/companies/"
            f"{token}/postings?limit=100&offset={offset}"
        )
        items = (data or {}).get("content", []) or []
        for job in items:
            jid = _s(job.get("id"))
            loc = job.get("location") or {}
            out.append(
                Posting(
                    company=company,
                    title=_s(job.get("name")),
                    location=_join(
                        loc.get("city"),
                        loc.get("region"),
                        loc.get("country"),
                        "remote" if loc.get("remote") else "",
                    ),
                    url=f"https://jobs.smartrecruiters.com/{token}/{jid}",
                    uid=f"smartrecruiters:{token}:{jid}",
                    raw=job,
                    posted_at=_date(job.get("releasedDate"), job.get("createdOn")),
                )
            )
        total = (data or {}).get("totalFound", 0)
        offset += 100
        if not items or offset >= total or offset > 1000:
            break
    return out


def workable(http: Http, company: str, token: str) -> list[Posting]:
    """Workable widget account API. Shape ``{"jobs": [...]}`` verified."""
    data = http.json(
        f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    )
    out = []
    for job in (data or {}).get("jobs", []):
        jid = _s(job.get("shortcode")) or _s(job.get("id"))
        out.append(
            Posting(
                company=company,
                title=_s(job.get("title")),
                location=_join(
                    job.get("city"),
                    job.get("region"),
                    job.get("country"),
                    job.get("location"),
                    "remote" if job.get("telecommuting") else "",
                ),
                url=_s(job.get("url"))
                or _s(job.get("application_url"))
                or f"https://apply.workable.com/{token}/j/{jid}/",
                uid=f"workable:{token}:{jid}",
                raw=job,
                posted_at=_date(job.get("published_on"), job.get("created_at")),
            )
        )
    return out


def recruitee(http: Http, company: str, token: str) -> list[Posting]:
    """Recruitee offers API."""
    data = http.json(f"https://{token}.recruitee.com/api/offers/")
    out = []
    for job in (data or {}).get("offers", []):
        jid = _s(job.get("id"))
        out.append(
            Posting(
                company=company,
                title=_s(job.get("title")),
                location=_join(
                    job.get("location"),
                    job.get("city"),
                    job.get("country"),
                    job.get("remote") and "remote",
                ),
                url=_s(job.get("careers_url")) or _s(job.get("careers_apply_url")),
                uid=f"recruitee:{token}:{jid}",
                raw=job,
                posted_at=_date(job.get("published_at"), job.get("created_at")),
            )
        )
    return out


def teamtailor(http: Http, company: str, token: str) -> list[Posting]:
    """Teamtailor public jobs feed. Accepts either a bare list or ``{"jobs": []}``."""
    data = http.json(f"https://{token}.teamtailor.com/jobs.json")
    jobs = data if isinstance(data, list) else (data or {}).get("jobs", [])
    out = []
    for job in jobs or []:
        jid = _s(job.get("id")) or _s(job.get("careersite-job-id"))
        out.append(
            Posting(
                company=company,
                title=_s(job.get("title")) or _s(job.get("name")),
                location=_join(
                    job.get("location"), job.get("city"), job.get("country"),
                    job.get("remote-status"),
                ),
                url=_s(job.get("careersite-job-url")) or _s(job.get("url")),
                uid=f"teamtailor:{token}:{jid}",
                raw=job,
                posted_at=_date(job.get("created-at"), job.get("updated-at")),
            )
        )
    return out


def breezy(http: Http, company: str, token: str) -> list[Posting]:
    """Breezy HR public JSON. Shape verified: a bare list of positions."""
    data = http.json(f"https://{token}.breezy.hr/json")
    out = []
    for job in data or []:
        jid = _s(job.get("id")) or _s(job.get("friendly_id"))
        loc = job.get("location") or {}
        out.append(
            Posting(
                company=company,
                title=_s(job.get("name")),
                location=_join(
                    (loc.get("city") if isinstance(loc, dict) else loc),
                    (loc.get("country") if isinstance(loc, dict) else ""),
                    "remote" if (isinstance(loc, dict) and loc.get("is_remote")) else "",
                ),
                url=_s(job.get("url"))
                or f"https://{token}.breezy.hr/p/{_s(job.get('friendly_id'))}",
                uid=f"breezy:{token}:{jid}",
                raw=job,
                posted_at=_date(job.get("published_date"), job.get("creation_date")),
            )
        )
    return out


_PERSONIO_TAGS = {
    "id": "id",
    "name": "name",
    "office": "office",
    "department": "department",
    "employmentType": "employmentType",
}


def personio(http: Http, company: str, token: str) -> list[Posting]:
    """Personio XML feed. The only non-JSON ATS in the set."""
    text = http.text(f"https://{token}.jobs.personio.de/xml")
    if text is None:
        return []
    root = ET.fromstring(text)
    out = []
    for pos in root.iter("position"):
        fields = {tag: (pos.findtext(tag) or "").strip() for tag in _PERSONIO_TAGS}
        jid = fields.get("id") or ""
        out.append(
            Posting(
                company=company,
                title=fields.get("name", ""),
                location=_join(fields.get("office"), fields.get("employmentType")),
                url=f"https://{token}.jobs.personio.de/job/{jid}",
                uid=f"personio:{token}:{jid}",
                raw=fields,
            )
        )
    return out


# Workday search is keyword-driven, so one term never surfaces everything.
WORKDAY_TERMS = ["intern", "co-op", "student", "new grad"]
WORKDAY_PAGE = 20


def workday(http: Http, company: str, token: str) -> list[Posting]:
    """Workday CXS jobs endpoint. Verified against NVIDIA.

    ``token`` is the full ``.../wday/cxs/{tenant}/{site}/jobs`` URL, copied
    from the browser network tab -- there is no way to derive it.

    Runs every search term with offset pagination and merges on the provider's
    own requisition id (``bulletFields[0]``, e.g. ``JR2021277``), which is
    stable across runs. ``externalPath`` is the fallback.
    """
    base = token.rstrip("/")
    if not base.endswith("/jobs"):
        base = base + "/jobs"

    found: dict[str, Posting] = {}
    for term in WORKDAY_TERMS:
        offset = 0
        while offset <= 200:
            payload = {
                "appliedFacets": {},
                "limit": WORKDAY_PAGE,
                "offset": offset,
                "searchText": term,
            }
            resp = http.request(
                "POST",
                base,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            data = resp.json() if resp is not None else {}
            posts = data.get("jobPostings") or []
            for job in posts:
                bullets = job.get("bulletFields") or []
                jid = _s(bullets[0]) if bullets else _s(job.get("externalPath"))
                if not jid:
                    continue
                path = _s(job.get("externalPath"))
                found[jid] = Posting(
                    company=company,
                    title=_s(job.get("title")),
                    location=_join(job.get("locationsText")),
                    url=_workday_url(base, path),
                    uid=f"workday:{urlparse(base).netloc}:{jid}",
                    raw=job,
                    posted_at=_date(job.get("postedOn"), job.get("startDate")),
                )
            total = data.get("total", 0)
            offset += WORKDAY_PAGE
            if len(posts) < WORKDAY_PAGE or offset >= total:
                break

    _resolve_workday_locations(http, base, found.values())
    return list(found.values())


_WORKDAY_MULTI_RE = re.compile(r"^\d+\s+Locations?$", re.IGNORECASE)
WORKDAY_DETAIL_CAP = 40


def _resolve_workday_locations(http: Http, base: str, posts: Iterable[Posting]) -> None:
    """Replace "3 Locations" with the real list, for student-level roles.

    Workday's search summarises multi-site reqs as "N Locations", which names
    no city, so the classifier cannot place them. Real Toronto co-ops hide
    behind it (TD's Winter 2027 SWE co-op lists Toronto, Mississauga and
    London). One detail request per posting, capped per board.
    """
    from core import NEWGRAD_RE, STUDENT_RE

    detail_base = base[: -len("/jobs")]
    budget = WORKDAY_DETAIL_CAP
    for post in posts:
        if budget <= 0:
            break
        if not _WORKDAY_MULTI_RE.match(post.location or ""):
            continue
        if not (STUDENT_RE.search(post.title) or NEWGRAD_RE.search(post.title)):
            continue
        budget -= 1
        try:
            detail = http.json(detail_base + _s(post.raw.get("externalPath")))
        except Exception:  # noqa: BLE001 - keep the summary; classifier rejects it
            continue
        info = (detail or {}).get("jobPostingInfo") or {}
        places = _join(info.get("location"), *(info.get("additionalLocations") or []),
                       (info.get("country") or {}).get("descriptor"))
        if places:
            post.location = places


def _workday_url(cxs_url: str, external_path: str) -> str:
    """Turn a CXS API URL plus externalPath into the human careers-site URL."""
    m = re.match(r"(https://[^/]+)/wday/cxs/[^/]+/([^/]+)/jobs", cxs_url)
    if not m:
        return external_path
    return f"{m.group(1)}/en-US/{m.group(2)}{external_path}"


AMAZON_PAGE = 100


def _amazon_date(value: Any) -> int:
    """Amazon writes "September  9, 2026", which ISO parsing cannot read."""
    text = " ".join(_s(value).split())
    try:
        dt = datetime.strptime(text, "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return int(dt.timestamp())


def amazon(http: Http, company: str, token: str) -> list[Posting]:
    """amazon.jobs search JSON. ``token`` is an ISO-3 country code (``CAN``).

    Amazon runs its own careers site rather than a third-party ATS, but the
    site's search is backed by a public JSON endpoint. Like Workday, it is
    keyword-driven, so the same student terms are searched and merged.
    """
    found: dict[str, Posting] = {}
    for term in WORKDAY_TERMS:
        offset = 0
        while offset <= 500:
            data = http.json(
                "https://www.amazon.jobs/en/search.json",
                params={"base_query": term, "country": token,
                        "result_limit": AMAZON_PAGE, "offset": offset},
            ) or {}
            jobs = data.get("jobs") or []
            for job in jobs:
                jid = _s(job.get("id_icims")) or _s(job.get("id"))
                if not jid:
                    continue
                found[jid] = Posting(
                    company=company,
                    title=_s(job.get("title")),
                    location=_join(job.get("normalized_location"), job.get("location")),
                    url="https://www.amazon.jobs" + _s(job.get("job_path")),
                    uid=f"amazon:{jid}",
                    raw={k: job.get(k) for k in ("city", "state", "country_code",
                                                 "job_category", "job_schedule_type")},
                    posted_at=_amazon_date(job.get("posted_date")),
                )
            offset += AMAZON_PAGE
            if len(jobs) < AMAZON_PAGE or offset >= int(data.get("hits") or 0):
                break
    return list(found.values())


# The four platforms below are keyword-driven like Workday, so the student
# terms are searched and merged on the provider's own id. "new grad" is left
# out: these boards are large and the classifier caps new-grad roles anyway.
SEARCH_TERMS = ["intern", "co-op", "student"]
SEARCH_MAX_OFFSET = 200


def _split_token(token: str) -> tuple[str, str]:
    """``"a|b"`` -> ``("a", "b")``, for platforms needing two identifiers."""
    first, _, second = token.partition("|")
    return first.strip().rstrip("/"), second.strip()


def oracle(http: Http, company: str, token: str) -> list[Posting]:
    """Oracle Cloud Recruiting (Candidate Experience). Verified against Nokia.

    ``token`` is ``"https://{host}|{siteNumber}"``, both read straight off a
    posting URL: ``{host}/hcmUI/CandidateExperience/en/sites/CX_1/job/40261``.
    """
    host, site = _split_token(token)
    site = site or "CX_1"
    api = f"{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    found: dict[str, Posting] = {}
    for term in SEARCH_TERMS:
        offset = 0
        while offset <= SEARCH_MAX_OFFSET:
            finder = (f"findReqs;siteNumber={site},keyword={term},"
                      f"limit=25,offset={offset}")
            data = http.json(api, params={
                "onlyData": "true",
                "expand": "requisitionList.secondaryLocations",
                "finder": finder,
            }) or {}
            items = data.get("items") or [{}]
            reqs = items[0].get("requisitionList") or []
            for req in reqs:
                jid = _s(req.get("Id"))
                if not jid:
                    continue
                secondary = [_s(s.get("Name")) for s in req.get("secondaryLocations") or []]
                remote = "Remote" if "remote" in _s(req.get("WorkplaceType")).lower() else ""
                found[jid] = Posting(
                    company=company,
                    title=_s(req.get("Title")),
                    location=_join(req.get("PrimaryLocation"), *secondary, remote),
                    url=f"{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                    uid=f"oracle:{urlparse(host).netloc}:{jid}",
                    raw={"PrimaryLocationCountry": req.get("PrimaryLocationCountry")},
                    posted_at=_date(req.get("PostedDate")),
                )
            offset += 25
            if len(reqs) < 25 or offset >= int(items[0].get("TotalJobsCount") or 0):
                break
    return list(found.values())


def jibe(http: Http, company: str, token: str) -> list[Posting]:
    """iCIMS "Jibe" careers sites, whose posting URLs end ``?icims=1``.

    ``token`` is the careers site root, e.g. ``https://careers.amd.com``. Its
    ``/api/jobs`` search takes a location filter, so only Canada is fetched.
    Verified against AMD.

    The search filters on the request language: ``en-CA`` returns nothing,
    since postings are tagged ``en-us``.
    """
    base = token.rstrip("/")
    found: dict[str, Posting] = {}
    for term in SEARCH_TERMS:
        page = 1
        while (page - 1) * 100 <= SEARCH_MAX_OFFSET:
            data = http.json(f"{base}/api/jobs", params={
                "keywords": term, "location": "Canada", "page": page, "limit": 100,
            }, headers={"Accept-Language": "en-US,en;q=0.9"}) or {}
            jobs = data.get("jobs") or []
            for item in jobs:
                job = item.get("data") or {}
                jid = _s(job.get("req_id")) or _s(job.get("slug"))
                if not jid:
                    continue
                found[jid] = Posting(
                    company=company,
                    title=_s(job.get("title")),
                    location=_join(job.get("full_location"),
                                   _join(job.get("city"), job.get("state"), job.get("country"))),
                    url=_s(job.get("canonical_url")) or f"{base}/jobs/{jid}",
                    uid=f"jibe:{urlparse(base).netloc}:{jid}",
                    raw={"location_type": job.get("location_type")},
                    posted_at=_date(job.get("posted_date"), job.get("create_date")),
                )
            if len(jobs) < 100 or page * 100 >= int(data.get("totalCount") or 0):
                break
            page += 1
    return list(found.values())


def eightfold(http: Http, company: str, token: str) -> list[Posting]:
    """Eightfold careers sites through the PCSX search. Verified against Qualcomm.

    ``token`` is ``"{tenant}.eightfold.ai|{domain}"``, e.g.
    ``"qualcomm.eightfold.ai|qualcomm.com"``. The older
    ``/api/apply/v2/jobs`` endpoint answers 403 ("Not authorized for PCSX").
    """
    host, domain = _split_token(token)
    host = re.sub(r"^https?://", "", host)
    domain = domain or host.split(".")[0] + ".com"
    found: dict[str, Posting] = {}
    for term in SEARCH_TERMS:
        start = 0
        while start <= SEARCH_MAX_OFFSET:
            data = (http.json(f"https://{host}/api/pcsx/search", params={
                "domain": domain, "query": term, "location": "Canada", "start": start,
            }) or {}).get("data") or {}
            positions = data.get("positions") or []
            for job in positions:
                jid = _s(job.get("id"))
                if not jid:
                    continue
                remote = "Remote" if "remote" in _s(job.get("workLocationOption")).lower() else ""
                found[jid] = Posting(
                    company=company,
                    title=_s(job.get("name")),
                    location=_join(*(job.get("locations") or []), remote),
                    url=f"https://{host}{_s(job.get('positionUrl')) or '/careers/job/' + jid}",
                    uid=f"eightfold:{host}:{jid}",
                    raw={"department": job.get("department")},
                    posted_at=_date(job.get("postedTs"), job.get("creationTs")),
                )
            start += len(positions)
            if not positions or start >= int(data.get("count") or 0):
                break
    return list(found.values())


_ICIMS_CARD_RE = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.S)
_ICIMS_LINK_RE = re.compile(r'<a href="([^"]*/jobs/(\d+)/[^"]*)"[^>]*class="iCIMS_Anchor"'
                            r'[^>]*>.*?<h3[^>]*>(.*?)</h3>', re.S)
# Portals use two card templates: "Location" with the date in a <dl>, and
# "Job Locations" with the date in the card header.
_ICIMS_LOC_RE = re.compile(
    r'field-label">(?:Job )?Locations?</span>\s*<span[^>]*>(.*?)</span>', re.S)
_ICIMS_MORE_RE = re.compile(r'Additional Locations</span>.*?<dd[^>]*><span[^>]*>(.*?)</span>', re.S)
_ICIMS_DATE_RE = re.compile(
    r'Posted Date</(?:dt|span)>\s*(?:<dd[^>]*>)?\s*<span title="([^"]+)"', re.S)
_ICIMS_COUNTRIES = {"CA": "Canada", "US": "United States", "UK": "UK", "GB": "UK",
                    "IN": "India", "DE": "Germany", "FR": "France", "IE": "Ireland"}
ICIMS_MAX_PAGES = 10
ICIMS_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "Chrome/126.0.0.0 Safari/537.36")


def _icims_place(code: str) -> str:
    """``CA-ON-Ottawa`` -> ``Ottawa, ON, Canada``; ``CA-Remote`` -> ``Remote, Canada``.

    iCIMS writes locations as country-region-city codes, which the location
    gate cannot read as they stand.
    """
    parts = [p.strip() for p in code.strip().split("-", 2)]
    if len(parts) < 2 or parts[0].upper() not in _ICIMS_COUNTRIES:
        return code.strip()
    country = _ICIMS_COUNTRIES[parts[0].upper()]
    return ", ".join([*reversed(parts[1:]), country])


def _icims_date(value: str) -> int:
    try:
        dt = datetime.strptime(value.strip(), "%m/%d/%Y %I:%M %p")
    except ValueError:
        return 0
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _icims_cards(html: str) -> list[dict[str, Any]]:
    cards = []
    for block in _ICIMS_CARD_RE.findall(html):
        link = _ICIMS_LINK_RE.search(block)
        if not link:
            continue
        loc = _ICIMS_LOC_RE.search(block)
        more = _ICIMS_MORE_RE.search(block)
        places = [loc.group(1)] if loc else []
        if more:
            places += more.group(1).split("|")
        date = _ICIMS_DATE_RE.search(block)
        cards.append({
            "id": link.group(2),
            "url": re.sub(r"[?&]in_iframe=1", "", unescape(link.group(1))),
            "title": " ".join(unescape(re.sub(r"<[^>]+>", "", link.group(3))).split()),
            "location": _join(*(_icims_place(unescape(p)) for p in places if p.strip())),
            "posted_at": _icims_date(date.group(1)) if date else 0,
        })
    return cards


def icims(http: Http, company: str, token: str) -> list[Posting]:
    """Classic iCIMS portals (``careers-{x}.icims.com``). Verified against Kinaxis.

    There is no JSON, so the server-rendered search page is parsed: up to 50
    cards a page, each carrying title, location codes and a posted date.

    iCIMS answers 405 to any User-Agent containing "(KHTML, like Gecko)",
    which the shared client sends, so this adapter sends its own. The full
    listing is paged rather than searched per keyword: a whole portal is
    usually a few pages.
    """
    base = token.rstrip("/")
    host = urlparse(base).netloc
    found: dict[str, Posting] = {}
    for page in range(ICIMS_MAX_PAGES):
        html = http.text(f"{base}/jobs/search",
                         params={"ss": "1", "in_iframe": "1", "pr": page},
                         headers={"User-Agent": ICIMS_USER_AGENT}) or ""
        cards = _icims_cards(html)
        for card in cards:
            found[card["id"]] = Posting(
                company=company,
                title=card["title"],
                location=card["location"],
                url=card["url"],
                uid=f"icims:{host}:{card['id']}",
                raw={},
                posted_at=card["posted_at"],
            )
        if not cards or f"pr={page + 1}" not in html:
            break
    return list(found.values())


ADAPTERS: dict[str, Callable[..., list[Posting]]] = {
    "ashby": ashby,
    "greenhouse": greenhouse,
    "lever": lever,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "recruitee": recruitee,
    "teamtailor": teamtailor,
    "breezy": breezy,
    "personio": personio,
    "workday": workday,
    "amazon": amazon,
    "oracle": oracle,
    "jibe": jibe,
    "eightfold": eightfold,
    "icims": icims,
}


# --------------------------------------------------------------------------
# Layer 2: HTML careers-page fallback
# --------------------------------------------------------------------------

_ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def html_links(http: Http, company: str, url: str) -> list[Posting]:
    """Crude by design: every anchor on a careers page becomes a candidate.

    Filters to same-domain links or anything whose href/text mentions a job.
    It cannot miss a link appearing, which is the whole point -- the dedupe
    layer absorbs the noise.
    """
    text = http.text(url)
    if text is None:
        return []
    structured = _jsonld_postings(company, url, text)
    if structured:
        return structured
    host = urlparse(url).netloc
    out: list[Posting] = []
    seen: set[str] = set()

    for href, inner in _ANCHOR_RE.findall(text):
        label = _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", inner))).strip()
        if not label or len(label) > 200:
            continue
        absolute = urljoin(url, unescape(href.strip()))
        if not absolute.startswith("http"):
            continue
        same_domain = urlparse(absolute).netloc.endswith(host.split(":")[0][-15:])
        looks_like_job = re.search(
            r"job|career|position|opening|posting|apply|vacanc|req",
            absolute + " " + label,
            re.IGNORECASE,
        )
        if not (same_domain or looks_like_job):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(
            Posting(
                company=company,
                title=label,
                location="",
                url=absolute,
                uid=f"html:{company}:{absolute}",
                raw={"anchor_text": label, "page": url},
            )
        )
    return out


_JSONLD_RE = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _jsonld_nodes(node: Any, depth: int = 0) -> Iterable[dict]:
    """Every dict in a JSON-LD document, through lists and ``@graph``."""
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node:
            yield from _jsonld_nodes(item, depth + 1)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "item"):
            if key in node:
                yield from _jsonld_nodes(node[key], depth + 1)


def _jsonld_location(job: dict) -> str:
    places = job.get("jobLocation") or []
    parts: list[str] = []
    for place in places if isinstance(places, list) else [places]:
        addr = place.get("address") if isinstance(place, dict) else None
        if isinstance(addr, dict):
            parts.append(_join(addr.get("addressLocality"), addr.get("addressRegion"),
                               addr.get("addressCountry")))
        elif addr:
            parts.append(_s(addr))
    if _s(job.get("jobLocationType")).upper() == "TELECOMMUTE":
        parts.append("Remote")
        parts.extend(_s(r) for r in _as_list(job.get("applicantLocationRequirements")))
    return _join(*parts)


def _as_list(val: Any) -> list:
    if val is None:
        return []
    return val if isinstance(val, list) else [val]


def _jsonld_postings(company: str, page: str, text: str) -> list[Posting]:
    """schema.org ``JobPosting`` records embedded in a careers page.

    Google for Jobs indexes these, so plenty of careers sites that render
    their listings client-side still ship them in the HTML. When present they
    carry a real title, location and publish date, which a bare anchor never
    does -- so they replace the link-diff result rather than add to it.
    """
    out: list[Posting] = []
    seen: set[str] = set()
    for blob in _JSONLD_RE.findall(text):
        try:
            doc = json.loads(unescape(blob.strip()))
        except ValueError:
            continue
        for node in _jsonld_nodes(doc):
            kinds = _as_list(node.get("@type"))
            if "JobPosting" not in kinds:
                continue
            title = _s(node.get("title"))
            link = urljoin(page, _s(node.get("url")) or page)
            ident = node.get("identifier")
            jid = _s(ident.get("value") if isinstance(ident, dict) else ident)
            key = jid or f"{link}#{title}"
            if not title or key in seen:
                continue
            seen.add(key)
            org = node.get("hiringOrganization")
            out.append(
                Posting(
                    company=company,
                    title=title,
                    location=_jsonld_location(node),
                    url=link,
                    uid=f"html:{company}:{key}",
                    raw={"employmentType": _s(node.get("employmentType")),
                         "organization": _s(org), "page": page},
                    posted_at=_date(node.get("datePosted")),
                )
            )
    return out


# --------------------------------------------------------------------------
# Layer 3: community trackers
# --------------------------------------------------------------------------

RAW = "https://raw.githubusercontent.com"
BRANCHES = ("dev", "main", "master")

# These repos publish a structured listings file alongside the README. It is
# strictly higher recall than table scraping -- Simplify's README moved from
# markdown pipes to an HTML <table>, which a pipe parser reads as zero rows.
JSON_PATHS = (".github/scripts/listings.json", "listings.json")
README_PATHS = ("README.md", "readme.md")


def tracker(http: Http, name: str, repo: str) -> list[Posting]:
    """Read a community tracker repo, preferring its structured listings file.

    Branch names differ per repo (``dev`` vs ``main``), so every candidate is
    tried rather than hardcoding one and silently returning nothing.
    """
    for branch in BRANCHES:
        for path in JSON_PATHS:
            url = f"{RAW}/{repo}/{branch}/{path}"
            try:
                data = http.json(url, etag_key=f"tracker:{repo}:{path}")
            except requests.HTTPError:
                continue
            if data is None:  # 304 Not Modified: nothing new since last run
                return []
            if isinstance(data, list) and data:
                return _tracker_from_json(name, repo, data)

    last_error: Optional[Exception] = None
    for branch in BRANCHES:
        for path in README_PATHS:
            url = f"{RAW}/{repo}/{branch}/{path}"
            try:
                text = http.text(url, etag_key=f"tracker:{repo}:{path}")
            except requests.HTTPError as exc:
                last_error = exc
                continue
            if text is None:
                return []
            rows = parse_tracker_readme(text)
            if rows:
                return _tracker_from_rows(name, repo, rows)

    raise last_error or RuntimeError(f"no readable listing in {repo}")


# Trackers fill the season field with these when no term is known. Appended
# to the title verbatim they produce rows like "Data Engineer Co-op, N/A".
_SEASON_PLACEHOLDERS = {"n/a", "na", "tbd", "tba", "-", "none", "unknown", "null"}


def _season(*parts: Any) -> str:
    """Join the season fields of a tracker item, dropping placeholders."""
    terms: list[Any] = []
    for part in parts:
        terms.extend(part if isinstance(part, (list, tuple)) else [part])
    return _join(*(t for t in terms if _s(t).lower() not in _SEASON_PLACEHOLDERS))


def _tracker_from_json(name: str, repo: str, data: list[dict]) -> list[Posting]:
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("active") is False or item.get("is_visible") is False:
            continue
        jid = _s(item.get("id"))
        url = _s(item.get("url"))
        if not url:
            continue
        season = _season(item.get("season"), item.get("terms"))
        out.append(
            Posting(
                company=_s(item.get("company_name")),
                title=_join(item.get("title"), season) if season else _s(item.get("title")),
                location=_s(item.get("locations")),
                url=url,
                uid=f"tracker:{repo}:{jid or url}",
                source=name,
                raw=item,
                posted_at=_date(item.get("date_posted"), item.get("date_updated")),
            )
        )
    return out


def _tracker_from_rows(name: str, repo: str, rows: list[dict]) -> list[Posting]:
    out = []
    for row in rows:
        url = row.get("url") or ""
        if not url:
            continue
        out.append(
            Posting(
                company=row.get("company", ""),
                title=row.get("title", ""),
                location=row.get("location", ""),
                url=url,
                uid=f"tracker:{repo}:{row.get('id') or url}",
                source=name,
                raw=row,
                posted_at=int(row.get("posted_at") or 0),
            )
        )
    return out


_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
_HREF_RE = re.compile(r"href\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_ID_COMMENT_RE = re.compile(r"<!--\s*id:([^\s>-]+)\s*-->")
_HTML_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_CONTINUATION = ("↳", "->", "&#8627;", "↳")

# Trackers name these columns inconsistently; match on substrings.
_COL_ALIASES = {
    "company": ("company", "employer", "organization"),
    "title": ("role", "position", "title", "job"),
    "location": ("location", "city", "where"),
    # speedyapply calls its apply-button column "Posting".
    "url": ("apply", "application", "link", "url", "posting"),
    "date": ("date", "posted", "age", "added", "when"),
    # Trackers bury the work term here ("Intern - 4mo - Fall 2026"), which is
    # the only place the cycle appears for some rows.
    "details": ("detail", "term", "duration", "type", "season"),
}

# Tracker tables write dates as "Aug 21", "1d", "3 days ago", "2026-08-21".
_REL_AGE_RE = re.compile(r"^(\d+)\s*([dhwmo]|day|hour|week|mo)", re.IGNORECASE)
_MON_DAY_RE = re.compile(
    r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})",
    re.IGNORECASE,
)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]


def _table_date(cell: str) -> int:
    """Parse the date column of a tracker table into Unix seconds.

    Returns 0 when unparseable, which the freshness filter treats as unknown
    rather than stale.
    """
    text = (cell or "").strip()
    if not text:
        return 0

    iso = _one_date(text)
    if iso:
        return iso

    m = _REL_AGE_RE.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        scale = {"h": 3600, "hour": 3600, "d": 86400, "day": 86400,
                 "w": 604800, "week": 604800, "m": 2592000, "mo": 2592000}
        return int(time.time()) - n * scale.get(unit, 86400)

    m = _MON_DAY_RE.match(text)
    if m:
        month = _MONTHS.index(m.group(1).lower()) + 1
        day = int(m.group(2))
        now = datetime.now(timezone.utc)
        year = now.year
        try:
            when = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return 0
        # A date more than a month ahead is last year's, not next year's.
        if (when - now).days > 31:
            when = when.replace(year=year - 1)
        return int(when.timestamp())
    return 0


def parse_tracker_readme(text: str) -> list[dict]:
    """Extract job rows from a tracker README.

    Handles both layouts these repos use in practice: markdown pipe tables
    (``negarprh``, ``vanshb03``) and HTML ``<table>`` blocks
    (``SimplifyJobs``). Column order is resolved from the header rather than
    assumed, since the repos disagree on it, and ``↳`` continuation rows
    inherit the company above them.
    """
    rows: list[dict] = []
    rows.extend(_parse_pipe_tables(text))
    rows.extend(_parse_html_tables(text))
    return rows


def _cell_text(cell: str) -> str:
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", cell))).strip(" |*")


# Apply buttons are often a badge image wrapped in a link --
# "[![Apply](https://img.shields.io/...)](https://real.posting)" -- and the
# first URL in the cell is then the badge, not the job.
_IMAGE_URL_RE = re.compile(
    r"img\.shields\.io|i\.imgur\.com|\.(?:png|svg|jpe?g|gif|webp)(?:[?#]|$)",
    re.IGNORECASE,
)


def _cell_url(cell: str) -> str:
    candidates = [
        *_HREF_RE.findall(cell),
        *_MD_LINK_RE.findall(cell),
        *(u.rstrip(">)") for u in re.findall(r"https?://[^\s)\]\"'<>]+", cell)),
    ]
    for url in candidates:
        url = unescape(url)
        if not _IMAGE_URL_RE.search(url):
            return url
    return ""


def _map_columns(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    lowered = [h.lower() for h in header]
    for field, aliases in _COL_ALIASES.items():
        for idx, head in enumerate(lowered):
            if idx in mapping.values():
                continue
            if any(a in head for a in aliases):
                mapping[field] = idx
                break
    return mapping


def _rows_from_cells(cell_rows: list[list[str]]) -> list[dict]:
    """Turn raw cell lists into job dicts, resolving columns from the header."""
    if len(cell_rows) < 2:
        return []
    mapping = _map_columns([_cell_text(c) for c in cell_rows[0]])
    if "title" not in mapping or "url" not in mapping:
        return []

    out: list[dict] = []
    last_company = ""
    for cells in cell_rows[1:]:
        if len(cells) <= max(mapping.values()):
            continue
        joined = " ".join(cells)
        if set(_cell_text(joined)) <= set("-: "):
            continue  # markdown separator row

        company = _cell_text(cells[mapping["company"]]) if "company" in mapping else ""
        if not company or company in _CONTINUATION or company.startswith(_CONTINUATION):
            company = last_company
        else:
            last_company = company

        title = _cell_text(cells[mapping["title"]])
        url = _cell_url(cells[mapping["url"]])
        if not title or not url:
            continue

        jid_match = _ID_COMMENT_RE.search(joined)
        out.append(
            {
                "company": company,
                "title": title,
                "location": (
                    _cell_text(cells[mapping["location"]])
                    if "location" in mapping
                    else ""
                ),
                "url": url,
                "id": jid_match.group(1) if jid_match else "",
                "posted_at": (
                    _table_date(_cell_text(cells[mapping["date"]]))
                    if "date" in mapping
                    else 0
                ),
                "details": (
                    _cell_text(cells[mapping["details"]])
                    if "details" in mapping
                    else ""
                ),
            }
        )
    return out


def _parse_pipe_tables(text: str) -> list[dict]:
    out: list[dict] = []
    block: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 3:
            cells = stripped.strip("|").split("|")
            block.append(cells)
        elif block:
            out.extend(_rows_from_cells(block))
            block = []
    if block:
        out.extend(_rows_from_cells(block))
    return out


def _parse_html_tables(text: str) -> list[dict]:
    out: list[dict] = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", text, re.I | re.S):
        cell_rows = [
            _HTML_CELL_RE.findall(tr) for tr in _HTML_ROW_RE.findall(table)
        ]
        cell_rows = [r for r in cell_rows if r]
        out.extend(_rows_from_cells(cell_rows))
    return out


# --------------------------------------------------------------------------
# ATS sniffer
# --------------------------------------------------------------------------

_SNIFF_PATTERNS: list[tuple[str, str]] = [
    ("ashby", r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([A-Za-z0-9_.\-]+)"),
    ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_\-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_\-]+)"),
    ("lever", r"(?:jobs\.lever\.co|api\.lever\.co/v0/postings)/([A-Za-z0-9_\-]+)"),
    ("smartrecruiters", r"(?:jobs|careers|api)\.smartrecruiters\.com/(?:v1/companies/)?([A-Za-z0-9_\-]+)"),
    ("workable", r"apply\.workable\.com/(?:api/v1/widget/accounts/)?([A-Za-z0-9_\-]+)"),
    ("recruitee", r"([A-Za-z0-9_\-]+)\.recruitee\.com"),
    ("teamtailor", r"([A-Za-z0-9_\-]+)\.teamtailor\.com"),
    ("breezy", r"([A-Za-z0-9_\-]+)\.breezy\.hr"),
    ("personio", r"([A-Za-z0-9_\-]+)\.jobs\.personio\.(?:de|com)"),
    ("workday", r"([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z\-]+/)?([A-Za-z0-9_\-]+)"),
    ("oracle", r"([a-z0-9\-]+\.fa(?:\.[a-z0-9\-]+)*\.oraclecloud\.com)"
               r"/hcmUI/CandidateExperience/[A-Za-z\-]+/sites/([A-Za-z0-9_]+)"),
    ("eightfold", r"([a-z0-9\-]+)\.eightfold\.ai"),
    # Jibe sites load their app from jibecdn; the board is the page's own host.
    ("jibe", r"(?:app|assets)\.jibecdn\.com"),
    ("icims", r"([A-Za-z0-9_\-]+)\.icims\.com"),
    ("successfactors", r"([A-Za-z0-9_\-]+)\.(?:successfactors|sapsf)\.(?:com|eu)"),
]

_NO_API = {
    "successfactors": "SuccessFactors",
}

# Words that show up in these URL slots but are never a real board token.
_TOKEN_NOISE = {
    "www", "jobs", "job", "careers", "career", "api", "app", "apply", "embed",
    "static", "assets", "cdn", "js", "css", "images", "img", "en", "en-us",
    "search", "index", "home", "about", "login", "signup", "help", "support",
}


def _sniff_token(platform: str, groups: tuple, page_url: str) -> Optional[str]:
    """The adapter token a sniff-pattern match names, or None for noise."""
    if platform == "workday":
        tenant, wd, site = groups
        return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    if platform == "oracle":
        return f"https://{groups[0].lower()}|{groups[1]}"
    if platform == "jibe":
        parsed = urlparse(page_url)
        return f"{parsed.scheme or 'https'}://{parsed.netloc}" if parsed.netloc else None
    token = groups[0]
    if token.lower() in _TOKEN_NOISE or len(token) < 2:
        return None
    if platform == "eightfold":
        return f"{token.lower()}.eightfold.ai|{token.lower()}.com"
    if platform == "icims":
        return f"https://{token.lower()}.icims.com"
    return token


def _sniff_hits(url: str, html: str) -> list[tuple[str, str]]:
    """Every ``(platform, token)`` signature in a page and its URL.

    The URL is scanned as well as the body: a Workday or Ashby careers page is
    a JS shell whose HTML never mentions the platform, but whose URL does.
    """
    hits: list[tuple[str, str]] = []
    haystack = f"{url}\n{html}"
    for platform, pattern in _SNIFF_PATTERNS:
        for match in re.finditer(pattern, haystack, re.IGNORECASE):
            token = _sniff_token(platform, match.groups(), url)
            if token and (platform, token) not in hits:
                hits.append((platform, token))
    return hits


def sniff_boards(http: Http, url: str) -> list[tuple[str, str]]:
    """Scrapable boards behind ``url``: sniff hits on platforms with an adapter."""
    html = http.text(url) or ""
    return [(p, t) for p, t in _sniff_hits(url, html) if p in ADAPTERS]


def sniff(http: Http, url: str) -> list[str]:
    """Detect the ATS behind a careers page and return config lines to paste.

    This is the fast path for adding companies: point it at a careers URL and
    it prints a line ready for ``companies.py``.
    """
    lines: list[str] = []
    try:
        html = http.text(url) or ""
    except Exception as exc:  # noqa: BLE001 - report, do not crash the CLI
        return [f"# could not fetch {url}: {exc}"]

    name_guess = _guess_name(url, html)
    hits = _sniff_hits(url, html)

    if not hits:
        lines.append(f"# no ATS signature found on {url}")
        lines.append("# fall back to the HTML layer:")
        lines.append(f'    {{"name": "{name_guess}", "platform": "html", "token": "{url}", "ai_native": False}},')
        return lines

    for platform, token in hits:
        if platform in _NO_API:
            lines.append(
                f"# {_NO_API[platform]} detected (token '{token}') -- no clean API, "
                "use the HTML fallback:"
            )
            lines.append(
                f'    {{"name": "{name_guess}", "platform": "html", "token": "{url}", "ai_native": False}},'
            )
            continue
        if platform == "workday":
            lines.append(
                "# Workday: confirm this cxs URL in the browser network tab "
                "(filter for '/jobs')."
            )
        lines.append(
            f'    {{"name": "{name_guess}", "platform": "{platform}", '
            f'"token": "{token}", "ai_native": False}},'
        )
    return lines


# Platforms whose board token is usually just the company slug, so it can be
# guessed. Workday, iCIMS and SuccessFactors cannot be.
PROBE_PLATFORMS = ("ashby", "greenhouse", "lever", "smartrecruiters", "workable")


def probe(http: Http, slug: str) -> list[str]:
    """Try ``slug`` as a board token on every guessable ATS.

    ``sniff`` needs the careers page to name its ATS, which a JS-rendered page
    (Clio, Xanadu) never does and a Cloudflare-walled one (Ada) never serves.
    The ATS APIs themselves are usually still reachable, so guess instead.
    Only boards with at least one posting count: SmartRecruiters answers 200
    with an empty list for any slug at all.
    """
    name = slug.replace("-", " ").replace("_", " ").title()
    lines: list[str] = []
    for platform in PROBE_PLATFORMS:
        try:
            found = ADAPTERS[platform](http, name, slug)
        except Exception as exc:  # noqa: BLE001 - a miss is the common case
            lines.append(f"# {platform:<16} miss  ({type(exc).__name__})")
            continue
        if not found:
            lines.append(f"# {platform:<16} miss  (0 postings)")
            continue
        lines.append(f"# {platform:<16} HIT   {len(found)} postings")
        lines.append(
            f'    {{"name": "{name}", "platform": "{platform}", '
            f'"token": "{slug}", "ai_native": False}},'
        )
    return lines


# --------------------------------------------------------------------------
# Board discovery
#
# Tracker rows link straight to the employer's own board. Every Canadian row
# therefore names a board worth scraping directly, which surfaces roles the
# trackers never list.
# --------------------------------------------------------------------------

# Posting URLs, not careers-page URLs, so these are stricter than the sniffer:
# each requires the path shape of an actual job link.
_BOARD_URL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.\-]+)/[0-9a-f\-]{8,}", re.I)),
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_\-]+)/jobs/\d+", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_app\?(?:[^#]*&)?for=([A-Za-z0-9_\-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_\-]+)/[0-9a-f\-]{8,}", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([A-Za-z0-9_\-]+)/\d+", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_\-]+)/j/", re.I)),
]

# tenant.wdN.myworkdayjobs.com/[en-US/]Site/job/... -- the "/job/" anchor is
# what separates the site slug from a locale prefix.
_WORKDAY_POSTING_RE = re.compile(
    r"https?://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"([A-Za-z0-9_\-]+)/job/"
)
# wdN.myworkdaysite.com/[en-US/]recruiting/tenant/Site/job/... is the same
# tenant, and its search answers on the tenant.wdN.myworkdayjobs.com host.
_WORKDAY_SITE_RE = re.compile(
    r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/"
    r"([a-z0-9\-]+)/([A-Za-z0-9_\-]+)/job/"
)
# Platforms whose token is more than a slug, as (platform, pattern, builder).
_COMPOSITE_POSTING_RES: list[tuple[str, re.Pattern, Callable[..., str]]] = [
    ("oracle", re.compile(
        r"https?://([a-z0-9\-]+\.fa(?:\.[a-z0-9\-]+)*\.oraclecloud\.com)"
        r"/hcmUI/CandidateExperience/[A-Za-z\-]+/sites/([A-Za-z0-9_]+)/job/", re.I),
     lambda host, site: f"https://{host.lower()}|{site}"),
    ("eightfold", re.compile(r"https?://([a-z0-9\-]+)\.eightfold\.ai/careers/job/\d+", re.I),
     lambda t: f"{t.lower()}.eightfold.ai|{t.lower()}.com"),
    # Jibe posting URLs end "?icims=1" (careers.amd.com/jobs/91308?icims=1).
    ("jibe", re.compile(r"https?://([a-z0-9.\-]+)/(?:[a-z\-]+/)?jobs/\d+\?(?:[^#]*&)?icims=1", re.I),
     lambda host: f"https://{host.lower()}"),
    ("icims", re.compile(r"https?://([a-z0-9\-]+\.icims\.com)/jobs/\d+", re.I),
     lambda host: f"https://{host.lower()}"),
]


def board_from_url(url: str) -> Optional[tuple[str, str]]:
    """The ``(platform, token)`` of the board a posting URL belongs to."""
    url = url or ""
    m = _WORKDAY_POSTING_RE.search(url)
    if m:
        tenant, wd, site = m.groups()
        return "workday", f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    m = _WORKDAY_SITE_RE.search(url)
    if m:
        wd, tenant, site = m.groups()
        return "workday", f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    for platform, pattern, build in _COMPOSITE_POSTING_RES:
        m = pattern.search(url)
        if m:
            return platform, build(*m.groups())
    for platform, pattern in _BOARD_URL_PATTERNS:
        m = pattern.search(url)
        if m and m.group(1).lower() not in _TOKEN_NOISE:
            return platform, m.group(1)
    return None


def board_key(platform: str, token: str) -> tuple[str, str]:
    """Case-insensitive identity of a board, for comparing against config."""
    return platform, token.rstrip("/").lower()


# Aggregators, redirectors and search pages: linked from trackers constantly,
# never an employer's own board.
SNIFF_DENY_HOSTS = (
    "zapply.jobs", "simplify.jobs", "google.com", "linkedin.com", "indeed.com",
    "indeed.ca", "glassdoor.com", "glassdoor.ca", "github.com", "careerpuck.com",
    "wellfound.com", "workatastartup.com", "joinhandshake.com", "amazon.jobs",
    "bit.ly", "forms.gle", "notion.site", "lnkd.in",
)


def _bare_host(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def unplaced_hosts(postings: Iterable[Posting], skip: set[str]) -> list[dict]:
    """Hosts of Canadian postings that no board pattern recognises.

    Each is a careers site that may run a supported ATS behind a custom
    domain (``jobs.l3harris.com``), which only fetching the page can tell.
    Returns ``{"host", "url", "name", "hits"}`` per host, most linked first.
    """
    from collections import Counter

    from core import in_canada

    hosts: dict[str, dict] = {}
    for post in postings:
        host = _bare_host(post.url)
        if not host or host in skip or board_from_url(post.url):
            continue
        if any(host == d or host.endswith("." + d) for d in SNIFF_DENY_HOSTS):
            continue
        if not in_canada(post):
            continue
        entry = hosts.setdefault(host, {"host": host, "url": post.url,
                                        "names": Counter(), "hits": 0})
        entry["hits"] += 1
        if post.company:
            entry["names"][post.company] += 1

    out = []
    for entry in hosts.values():
        names = entry.pop("names")
        entry["name"] = names.most_common(1)[0][0] if names else entry["host"]
        out.append(entry)
    return sorted(out, key=lambda h: -h["hits"])


def discover_boards(
    postings: Iterable[Posting], known: set[tuple[str, str]]
) -> list[dict]:
    """Boards named by Canadian postings that are not already configured.

    Returns ``{"platform", "token", "name", "hits"}`` per board, where
    ``hits`` is how many Canadian postings pointed at it. The name is the one
    the postings used most, so a role later scraped from the board itself
    fingerprints the same as its tracker copy.
    """
    from collections import Counter

    from core import in_canada

    boards: dict[tuple[str, str], dict] = {}
    for post in postings:
        found = board_from_url(post.url)
        if not found or not in_canada(post):
            continue
        key = board_key(*found)
        if key in known:
            continue
        board = boards.setdefault(
            key, {"platform": found[0], "token": found[1], "names": Counter(), "hits": 0}
        )
        board["hits"] += 1
        if post.company:
            board["names"][post.company] += 1

    out = []
    for board in boards.values():
        names = board.pop("names")
        board["name"] = names.most_common(1)[0][0] if names else board["token"]
        out.append(board)
    return sorted(out, key=lambda b: -b["hits"])


# Page titles are frequently just "Careers", which makes a useless company
# name; fall back to the domain in that case.
_GENERIC_TITLE_RE = re.compile(
    r"^(?:careers?|jobs?|home|open\s+(?:roles|positions)|work\s+with\s+us|"
    r"join\s+us|opportunities|hiring|about|team|welcome)$",
    re.IGNORECASE,
)


def _guess_name(url: str, html: str) -> str:
    """Best-effort company display name for the printed config line."""
    host = urlparse(url).netloc.replace("www.", "")
    fallback = host.split(".")[0].replace("-", " ").title()

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = _WS_RE.sub(" ", unescape(_TAG_RE.sub("", m.group(1)))).strip()
        for part in re.split(r"[|\-–—:]", title):
            part = part.strip()
            if 1 < len(part) < 40 and not _GENERIC_TITLE_RE.match(part):
                return part
    return fallback
