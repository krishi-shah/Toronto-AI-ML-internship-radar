"""Source adapters: ATS JSON APIs, HTML careers-page fallback, and community
trackers. Plus the ``--sniff`` helper that turns a careers URL into a config
line.

Every adapter takes a company display name and a platform token, and returns a
list of :class:`core.Posting`. Adapters raise on failure; the orchestrator in
``radar.py`` records the exception against the source and keeps going, so one
dead board never fails a run.
"""

from __future__ import annotations

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
    return list(found.values())


def _workday_url(cxs_url: str, external_path: str) -> str:
    """Turn a CXS API URL plus externalPath into the human careers-site URL."""
    m = re.match(r"(https://[^/]+)/wday/cxs/[^/]+/([^/]+)/jobs", cxs_url)
    if not m:
        return external_path
    return f"{m.group(1)}/en-US/{m.group(2)}{external_path}"


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
        season = _join(item.get("season"), item.get("terms"))
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
    "url": ("apply", "application", "link", "url"),
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


def _cell_url(cell: str) -> str:
    m = _HREF_RE.search(cell)
    if m:
        return unescape(m.group(1))
    m = _MD_LINK_RE.search(cell)
    if m:
        return unescape(m.group(1))
    m = re.search(r"https?://\S+", cell)
    return unescape(m.group(0).rstrip(">)")) if m else ""


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
    ("icims", r"([A-Za-z0-9_\-]+)\.icims\.com"),
    ("successfactors", r"([A-Za-z0-9_\-]+)\.(?:successfactors|sapsf)\.(?:com|eu)"),
]

_NO_API = {
    "icims": "iCIMS",
    "successfactors": "SuccessFactors",
}

# Words that show up in these URL slots but are never a real board token.
_TOKEN_NOISE = {
    "www", "jobs", "job", "careers", "career", "api", "app", "apply", "embed",
    "static", "assets", "cdn", "js", "css", "images", "img", "en", "en-us",
    "search", "index", "home", "about", "login", "signup", "help", "support",
}


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
    hits: list[tuple[str, str]] = []

    # Scan the URL as well as the body. A Workday or Ashby careers page is a
    # JS shell whose HTML never mentions the platform, but whose own URL does.
    haystack = f"{url}\n{html}"

    for platform, pattern in _SNIFF_PATTERNS:
        for match in re.finditer(pattern, haystack, re.IGNORECASE):
            groups = match.groups()
            if platform == "workday":
                tenant, wd, site = groups[0], groups[1], groups[2]
                token = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            else:
                token = groups[0]
                if token.lower() in _TOKEN_NOISE or len(token) < 2:
                    continue
            if (platform, token) not in hits:
                hits.append((platform, token))

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
