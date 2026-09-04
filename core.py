"""Classification, deduplication and SQLite state for the internship radar.

Three responsibilities, kept together because they are the parts a bug in
silently costs a job:

* :func:`classify` decides whether a posting pings instantly, waits for the
  5pm digest, or gets dropped.
* :func:`fingerprint` collapses the same role arriving from several sources
  into one notification.
* :class:`Store` persists what has been seen and how each source is doing.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Toronto time
# --------------------------------------------------------------------------

try:  # zoneinfo needs the tzdata package on Windows; fall back if absent.
    from zoneinfo import ZoneInfo

    _TORONTO: Any = ZoneInfo("America/Toronto")
except Exception:  # pragma: no cover - platform dependent
    _TORONTO = None


def toronto_now() -> datetime:
    """Return the current time in Toronto, falling back to a fixed EST offset."""
    if _TORONTO is not None:
        return datetime.now(_TORONTO)
    return datetime.now(timezone.utc) - timedelta(hours=5)


# --------------------------------------------------------------------------
# Normalised posting record
# --------------------------------------------------------------------------


@dataclass
class Posting:
    """One job posting, normalised across every source adapter."""

    company: str
    title: str
    location: str
    url: str
    uid: str
    source: str = ""
    raw: dict = field(default_factory=dict)
    ai_native: bool = False
    # Unix seconds the provider says this went live. 0 when the source does
    # not expose a date, in which case discovery time stands in for it.
    posted_at: int = 0

    def fingerprint(self) -> str:
        return fingerprint(self.company, self.title)


# --------------------------------------------------------------------------
# Token vocabularies
#
# Every list below widens what we catch. Recall beats precision here: a false
# positive costs five seconds, a false negative costs a job.
# --------------------------------------------------------------------------

_AI_TERMS = [
    r"machine[\s\-]?learning",
    r"\bml\b",
    r"\bmle\b",
    r"\bmlops\b",
    r"\bai\b",
    r"\ba\.i\.",
    r"artificial[\s\-]?intelligence",
    r"deep[\s\-]?learning",
    r"\bnlp\b",
    r"natural[\s\-]?language",
    r"computer[\s\-]?vision",
    r"\bcv\b(?![\s\-]?(?:file|upload))",
    r"\bllm[s]?\b",
    r"large[\s\-]?language[\s\-]?model",
    r"foundation[\s\-]?model",
    r"gen[\s\-]?ai",
    r"generative",
    r"neural",
    r"transformer",
    r"reinforcement[\s\-]?learning",
    r"\brlhf\b",
    r"data[\s\-]?scien",
    r"data[\s\-]?engineer",
    r"applied[\s\-]?scien",
    r"research[\s\-]?scien",
    r"research[\s\-]?engineer",
    r"\bperception\b",
    r"\bautonomy\b",
    r"autonomous",
    r"self[\s\-]?driving",
    r"\brobotic",
    r"\bspeech\b",
    r"\basr\b",
    r"recommend(?:er|ation)",
    r"\brecsys\b",
    r"quantitative[\s\-]?research",
    r"\bquantum[\s\-]?(?:machine|comput|software|algorithm)",
    r"\bcomputational\b",
    r"knowledge[\s\-]?graph",
    r"\bagentic\b",
    r"\bagent[s]?[\s\-]?(?:engineer|team|platform)",
]

_STUDENT_TERMS = [
    r"\bintern\b",
    r"\binterns\b",
    r"\binternship",
    r"\bco[\s\-]?op\b",
    r"\bcoop\b",
    r"cooperative[\s\-]?education",
    r"\bstudent\b",
    r"\bplacement\b",
    r"work[\s\-]?term",
    r"new[\s\-]?grad",
    r"new[\s\-]?graduate",
    r"recent[\s\-]?graduate",
    r"university[\s\-]?graduate",
    r"campus[\s\-]?hire",
    r"\bon[\s\-]?campus\b",
    r"\bundergrad",
    r"early[\s\-]?career",
    r"early[\s\-]?talent",
    r"emerging[\s\-]?talent",
    r"entry[\s\-]?level",
    r"\bapprentice",
    r"\btrainee\b",
    r"rotational",
    r"\bgraduate[\s\-]?program",
    r"\bw2[5-9]\b",
    r"\bs2[5-9]\b",
    r"\bf2[5-9]\b",
]

# Explicit rejects. Deliberately short: anything ambiguous belongs in loose.
_REJECT_TERMS = [
    r"\bunpaid\b",
    r"\bvolunteer",
    r"\bhigh[\s\-]?school\b",
    r"\bsecondary[\s\-]?school\b",
    r"\bphd[\s\-]?only\b",
    r"ph\.?d\.?[\s\-]?(?:students?[\s\-]?)?only",
    r"(?:must|required)[\s\-]?(?:to[\s\-]?)?(?:be[\s\-]?)?(?:a[\s\-]?)?ph\.?d",
    r"ph\.?d\.?[\s\-]?(?:candidates?|students?)[\s\-]?(?:are[\s\-]?)?required",
    r"\bdoctoral[\s\-]?candidates?[\s\-]?only\b",
]

_CANADA_TERMS = [
    r"\bcanada\b",
    r"\bcanadian\b",
    r"\btoronto\b",
    r"\bontario\b",
    r"\bottawa\b",
    r"\bwaterloo\b",
    r"\bkitchener\b",
    r"\bmississauga\b",
    r"\bbrampton\b",
    r"\bmarkham\b",
    r"\bvaughan\b",
    r"\bhamilton\b",
    r"\blondon,?\s*(?:on|ontario)\b",
    r"\bvancouver\b",
    r"\bburnaby\b",
    r"\bvictoria,?\s*(?:bc|british)\b",
    r"british[\s\-]?columbia",
    r"\bmontr[eé]al\b",
    r"\bqu[eé]bec\b",
    r"\bcalgary\b",
    r"\bedmonton\b",
    r"\balberta\b",
    r"\bwinnipeg\b",
    r"\bmanitoba\b",
    r"\bhalifax\b",
    r"nova[\s\-]?scotia",
    r"\bsaskatoon\b",
    r"\bregina\b",
    r"saskatchewan",
    r"new[\s\-]?brunswick",
    r"newfoundland",
    r"\bguelph\b",
    r"\bkingston,?\s*(?:on|ontario)\b",
    r"\bwindsor,?\s*(?:on|ontario)\b",
    r"\bygk\b",
    r",\s*(?:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)\b",
    r"\b(?:ON|QC|BC|AB)\s*,\s*Canada\b",
]

_REMOTE_TERMS = [
    r"\bremote\b",
    r"\banywhere\b",
    r"work[\s\-]?from[\s\-]?home\b",
    r"\bdistributed\b",
    r"\bvirtual\b",
    r"fully[\s\-]?remote",
    r"remote[\s\-]?(?:first|friendly)",
]

# --------------------------------------------------------------------------
# Cycle detection
#
# A cycle label is resolved to the (year, month) that cycle begins, so it can
# be compared against the cycle being targeted. Earlier cycles are rejected
# outright: recruiting for Fall 2026 is over, and those postings are pure
# noise. Later cycles are demoted to loose, never dropped.
# --------------------------------------------------------------------------

# Month each season's work term starts.
_SEASON_MONTH = {
    "winter": 1, "w": 1,
    "spring": 4,
    "summer": 5, "s": 5,
    "fall": 9, "autumn": 9, "f": 9,
}

# "Winter 2027", "Fall '26", "Summer 2027"
_CYCLE_LONG_RE = re.compile(
    r"\b(winter|spring|summer|fall|autumn)\b[\s\-,]*'?(\d{4}|\d{2})\b",
    re.IGNORECASE,
)
# "W27", "S2027", "F26" -- but not a bare word starting with those letters.
_CYCLE_SHORT_RE = re.compile(r"\b([wsf])'?(\d{2}|\d{4})\b", re.IGNORECASE)


def _norm_year(raw: str) -> int:
    year = int(raw)
    return year + 2000 if year < 100 else year


def parse_cycles(text: str) -> list[tuple[int, int]]:
    """Every cycle mentioned in ``text``, as sorted ``(year, month)`` pairs.

    A posting may legitimately name several ("Summer 2026, Winter 2027, Fall
    2027"), so all of them are returned and the caller keeps the best.
    """
    found: set[tuple[int, int]] = set()
    for season, year in _CYCLE_LONG_RE.findall(text or ""):
        found.add((_norm_year(year), _SEASON_MONTH[season.lower()]))
    for letter, year in _CYCLE_SHORT_RE.findall(text or ""):
        found.add((_norm_year(year), _SEASON_MONTH[letter.lower()]))
    return sorted(found)


def _compile(terms: Iterable[str]) -> re.Pattern:
    return re.compile("|".join(terms), re.IGNORECASE)


AI_RE = _compile(_AI_TERMS)
STUDENT_RE = _compile(_STUDENT_TERMS)
REJECT_RE = _compile(_REJECT_TERMS)
CANADA_RE = _compile(_CANADA_TERMS)
REMOTE_RE = _compile(_REMOTE_TERMS)

# Countries that positively rule a posting out, but only when no Canadian or
# remote signal appears anywhere alongside them.
_FOREIGN_RE = re.compile(
    r"\b(?:united\s+states|usa|u\.s\.a?\.|india|united\s+kingdom|england|ireland|"
    r"germany|france|spain|poland|romania|netherlands|sweden|norway|denmark|"
    r"switzerland|israel|singapore|japan|china|korea|australia|new\s+zealand|"
    r"brazil|mexico|argentina|philippines|vietnam|taiwan|hong\s+kong|uae|"
    r"dubai|egypt|nigeria|kenya|south\s+africa|portugal|italy|austria|belgium|"
    r"czech|hungary|greece|turkey|finland|estonia|lithuania|ukraine|"
    r"costa\s+rica|colombia|chile|peru|malaysia|indonesia|thailand|pakistan|"
    r"bangladesh|sri\s+lanka)\b"
    r"|,\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|MA|MD|ME|"
    r"MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
    r"VA|VT|WA|WI|WV|WY|DC)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

STRICT = "strict"
LOOSE = "loose"
REJECTED = None


@dataclass
class Verdict:
    """Why a posting landed where it did. Surfaced by ``--check`` for tuning."""

    tier: Optional[str]
    reason: str
    ai: bool = False
    student: bool = False
    canada: bool = False
    location_known: bool = True
    cycle: str = "unknown"

    def __bool__(self) -> bool:
        return self.tier is not None


def _location_blob(posting: Posting) -> str:
    """Every field that could carry a location signal, joined into one string.

    Ashby puts Toronto in ``secondaryLocations`` while the primary location is
    New York, so checking only the headline location misses real Toronto roles.
    """
    parts = [posting.location or ""]
    raw = posting.raw or {}

    for key in ("location", "locations", "locationsText", "offices", "country",
                "city", "region", "allLocations", "secondaryLocations",
                "workplaceType", "isRemote", "remote", "employmentType"):
        val = raw.get(key)
        parts.extend(_flatten(val))

    return " | ".join(p for p in parts if p)


def _flatten(val: Any, depth: int = 0) -> list[str]:
    """Pull every string out of an arbitrarily nested JSON fragment."""
    if depth > 4 or val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, bool):
        return ["remote"] if val else []
    if isinstance(val, (int, float)):
        return []
    if isinstance(val, dict):
        out: list[str] = []
        for v in val.values():
            out.extend(_flatten(v, depth + 1))
        return out
    if isinstance(val, (list, tuple, set)):
        out = []
        for v in val:
            out.extend(_flatten(v, depth + 1))
        return out
    return []


def classify(posting: Posting) -> Verdict:
    """Route a posting to the instant tier, the 5pm digest, or the bin.

    Strict (instant Telegram ping) requires all of:
      * an AI/ML signal in the title, or a company flagged ``ai_native``
      * a student-level signal
      * a positive Canada or remote location signal
      * no cycle token pointing exclusively at a season other than Winter 2027

    Anything student-level that is Canadian or has no readable location falls
    through to loose. Only unpaid, volunteer, high-school and PhD-only
    postings are dropped outright.
    """
    title = posting.title or ""
    loc_blob = _location_blob(posting)
    haystack = f"{title} | {posting.company} | {loc_blob}"

    if REJECT_RE.search(haystack):
        return Verdict(REJECTED, "reject term")

    student = bool(STUDENT_RE.search(title))
    if not student:
        # Some boards keep the level out of the title entirely.
        student = bool(STUDENT_RE.search(loc_blob)) or bool(
            STUDENT_RE.search(str(posting.raw.get("employmentType", "")))
        )
    if not student:
        return Verdict(REJECTED, "not student level", student=False)

    canada = bool(CANADA_RE.search(loc_blob)) or bool(REMOTE_RE.search(loc_blob))
    location_known = bool(loc_blob.strip())

    if not canada:
        if location_known and _FOREIGN_RE.search(loc_blob):
            return Verdict(
                REJECTED, "located outside Canada", student=True,
                location_known=True,
            )
        # No readable location: never bin it, let the digest decide.
        return Verdict(
            LOOSE, "location unknown", student=True, location_known=location_known,
        )

    cycle = _cycle_verdict(f"{haystack} | {_cycle_blob(posting)}")
    if cycle == "past":
        return Verdict(
            REJECTED, "cycle already passed", student=True, canada=True, cycle=cycle
        )

    ai = bool(AI_RE.search(title)) or posting.ai_native

    if not ai:
        return Verdict(LOOSE, "no AI signal", student=True, canada=True, cycle=cycle)
    if cycle == "later":
        return Verdict(
            LOOSE, "later cycle", ai=True, student=True, canada=True, cycle=cycle
        )

    return Verdict(STRICT, "ai + student + canada", ai=True, student=True,
                   canada=True, cycle=cycle)


# Fields that carry a work term when the title does not. Community trackers
# in particular put it in a "Details" column ("Intern - 4mo - Fall 2026"),
# which is the only cycle signal those rows have.
_CYCLE_FIELDS = (
    "details", "terms", "term", "season", "employmentType", "commitment",
    "workType", "duration", "category",
)


def _cycle_blob(posting: Posting) -> str:
    """Text outside the title that may name a work term."""
    raw = posting.raw or {}
    parts: list[str] = []
    for key in _CYCLE_FIELDS:
        parts.extend(_flatten(raw.get(key)))
    return " | ".join(p for p in parts if p)


def _cycle_verdict(text: str) -> str:
    """Classify a posting's cycle labels against the targeted cycle.

    Returns ``target`` (the cycle being hunted), ``later`` (a future cycle,
    demoted to loose), ``past`` (recruiting is over, rejected), or ``unknown``
    when no cycle is named at all -- which stays eligible for strict, because
    plenty of real postings never name a term.

    A posting naming several cycles keeps the best one: "Summer 2026, Winter
    2027, Fall 2027" is a live Winter 2027 opportunity, not a dead 2026 one.
    """
    cycles = parse_cycles(text)
    if not cycles:
        return "unknown"

    target = _target_cycle()
    if any(c == target for c in cycles):
        return "target"
    if any(c > target for c in cycles):
        return "later"
    return "past"


def _target_cycle() -> tuple[int, int]:
    """The cycle being targeted, from config, defaulting to Winter 2027."""
    try:
        import companies

        year, month = companies.TARGET_CYCLE
        return int(year), int(month)
    except Exception:  # noqa: BLE001 - config is optional for library use
        return (2027, 1)


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|"
    r"plc|gmbh|ag|sa|bv|nv|pty|labs?|technologies|technology|holdings|group|"
    r"solutions|systems|canada|usa|international|global)\b\.?",
    re.IGNORECASE,
)

# Noise that varies between sources for the same role and must not change the
# fingerprint: cycle labels, req numbers, emoji, sponsorship markers.
_TITLE_NOISE_RE = re.compile(
    r"\b(?:winter|summer|fall|autumn|spring)\b[\s\-,]*(?:20)?\d{2,4}"
    r"|\b[wsf](?:20)?2\d\b"
    r"|\b20\d{2}\b"
    r"|\b(?:req|job|jr|r)[\s\-#]?\d{4,}\b"
    r"|\b\d{5,}\b"
    r"|\b(?:remote|hybrid|onsite|on[\s\-]site)\b"
    r"|\b(?:\d{1,2}\s*(?:month|mo|week|wk)s?)\b"
    r"|\b(?:canada|usa|united states|toronto|vancouver|montreal|ottawa|waterloo)\b",
    re.IGNORECASE,
)

_COOP_RE = re.compile(r"\bco[\s\-]?op(?:erative)?(?:\s+education)?\b", re.IGNORECASE)
_INTERN_RE = re.compile(r"\binternship\b", re.IGNORECASE)
_NEWGRAD_RE = re.compile(r"\bnew\s*grad(?:uate)?\b", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_company(company: str) -> str:
    """Reduce a company name to a comparable key.

    ``Cohere Inc.``, ``cohere``, and ``Cohere Technologies`` all collapse
    together so a role seen on both Ashby and a tracker fingerprints once.
    """
    s = (company or "").lower()
    s = s.replace("&", " and ")
    s = _LEGAL_SUFFIX_RE.sub(" ", s)
    s = _NON_ALNUM_RE.sub("", s)
    return s


def normalize_title(title: str) -> str:
    """Reduce a job title to a comparable key.

    Strips cycle labels, req ids, location suffixes and emoji, and folds the
    co-op / internship / new-grad spelling variants onto one token each.
    """
    s = (title or "").lower()
    s = _COOP_RE.sub(" coop ", s)
    s = _INTERN_RE.sub(" intern ", s)
    s = _NEWGRAD_RE.sub(" newgrad ", s)
    s = _TITLE_NOISE_RE.sub(" ", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    # Order-insensitive so "Intern, ML Engineer" == "ML Engineer Intern".
    tokens = sorted(t for t in s.split() if t)
    return " ".join(tokens)


def fingerprint(company: str, title: str) -> str:
    """Stable cross-source identity for a role: normalised company + title."""
    key = f"{normalize_company(company)}::{normalize_title(title)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# SQLite state
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid          TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    company      TEXT NOT NULL,
    title        TEXT NOT NULL,
    location     TEXT,
    url          TEXT,
    source       TEXT,
    tier         TEXT,
    reason       TEXT,
    first_seen   INTEGER NOT NULL,
    posted_at    INTEGER NOT NULL DEFAULT 0,
    seeded       INTEGER NOT NULL DEFAULT 0,
    notified     INTEGER NOT NULL DEFAULT 0,
    digested     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(tier, notified, digested);

CREATE TABLE IF NOT EXISTS health (
    source       TEXT PRIMARY KEY,
    last_success INTEGER,
    last_error   TEXT,
    last_error_at INTEGER,
    job_count    INTEGER DEFAULT 0,
    ok           INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS links (
    source TEXT NOT NULL,
    url    TEXT NOT NULL,
    seen   INTEGER NOT NULL,
    PRIMARY KEY (source, url)
);

CREATE TABLE IF NOT EXISTS cache (
    key   TEXT PRIMARY KEY,
    etag  TEXT,
    at    INTEGER
);
"""


class Store:
    """SQLite-backed state: seen postings, per-source health, HTTP etags."""

    def __init__(self, path: str = "radar.db") -> None:
        self.path = path
        # Worker threads read and write the etag cache during a fetch, so the
        # connection is shared across threads and every access takes the lock.
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS silently does nothing on an existing table,
        so new columns need an explicit ALTER.
        """
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        if "posted_at" not in have:
            self.conn.execute(
                "ALTER TABLE jobs ADD COLUMN posted_at INTEGER NOT NULL DEFAULT 0"
            )
        if "seeded" not in have:
            self.conn.execute(
                "ALTER TABLE jobs ADD COLUMN seeded INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self.conn.close()

    # -- postings ---------------------------------------------------------

    def seen_uid(self, uid: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM jobs WHERE uid = ?", (uid,))
        return cur.fetchone() is not None

    def seen_fingerprint(self, fp: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM jobs WHERE fingerprint = ?", (fp,))
        return cur.fetchone() is not None

    def fingerprint_delivered(self, fp: str, tier: Optional[str]) -> bool:
        """Has this role already been delivered at ``tier`` or better?

        Fingerprints deliberately ignore cycle labels, so "ML Intern, Fall
        2026" and "ML Intern (Winter 2027)" collapse together. That is right
        for dedupe but wrong for suppression: an off-cycle posting lands in
        loose, and if it merely being *seen* silenced the fingerprint, the
        real Winter 2027 posting arriving later would never ping.

        So a strict candidate is only suppressed by a strict hit that was
        actually notified. A loose candidate is suppressed by any sighting.
        """
        if tier == STRICT:
            cur = self.conn.execute(
                "SELECT 1 FROM jobs WHERE fingerprint = ? AND tier = ?"
                " AND notified = 1",
                (fp, STRICT),
            )
        else:
            cur = self.conn.execute(
                "SELECT 1 FROM jobs WHERE fingerprint = ?", (fp,)
            )
        return cur.fetchone() is not None

    def record(
        self,
        posting: Posting,
        tier: Optional[str],
        reason: str,
        notified: bool = False,
        digested: bool = False,
        seeded: bool = False,
    ) -> None:
        """Insert a posting. Existing uids are left untouched so the
        ``first_seen`` timestamp and notified flags never reset."""
        self.conn.execute(
            "INSERT OR IGNORE INTO jobs (uid, fingerprint, company, title, location,"
            " url, source, tier, reason, first_seen, posted_at, seeded,"
            " notified, digested)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                posting.uid,
                posting.fingerprint(),
                posting.company,
                posting.title,
                posting.location,
                posting.url,
                posting.source,
                tier,
                reason,
                int(time.time()),
                int(posting.posted_at or 0),
                int(seeded),
                int(notified),
                int(digested),
            ),
        )

    def mark_notified(self, uid: str) -> None:
        self.conn.execute("UPDATE jobs SET notified = 1 WHERE uid = ?", (uid,))

    def pending_digest(self) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM jobs WHERE tier = ? AND digested = 0 ORDER BY company, title",
            (LOOSE,),
        )
        return cur.fetchall()

    def mark_digested(self, uids: Iterable[str]) -> None:
        self.conn.executemany(
            "UPDATE jobs SET digested = 1 WHERE uid = ?", ((u,) for u in uids)
        )

    def recent_jobs(
        self, tier: str, limit: int = 60, max_age_hours: Optional[int] = None
    ) -> list[sqlite3.Row]:
        """Postings in one tier, freshest first.

        Ordered and filtered by when the *provider* published the role, falling
        back to when we discovered it for sources that expose no date. A role
        that has been live for days already has hundreds of applicants, so
        ``max_age_hours`` keeps the list to things still worth racing for.
        """
        age_expr = "COALESCE(NULLIF(posted_at, 0), first_seen)"
        params: list = [tier]
        # A posting with no provider date that was recorded during the seed has
        # an unknown age -- first_seen is just when seeding ran, so it would
        # masquerade as brand new forever. Undated rows only count as fresh
        # when the radar genuinely discovered them after seeding.
        clause = " AND NOT (posted_at = 0 AND seeded = 1)"
        if max_age_hours:
            clause += f" AND {age_expr} >= ?"
            params.append(int(time.time()) - max_age_hours * 3600)
        params.append(limit)
        cur = self.conn.execute(
            f"SELECT *, {age_expr} AS shown_at FROM jobs WHERE tier = ?{clause}"
            f" ORDER BY shown_at DESC LIMIT ?",
            params,
        )
        return cur.fetchall()

    def counts(self) -> dict:
        cur = self.conn.execute(
            "SELECT tier, COUNT(*) c FROM jobs GROUP BY tier"
        )
        return {r["tier"] or "rejected": r["c"] for r in cur.fetchall()}

    # -- health -----------------------------------------------------------

    def health_ok(self, source: str, count: int) -> None:
        """Record a successful fetch.

        ``job_count`` is the last *non-zero* count, not the last count. A
        source whose content is unchanged answers 304 and yields no postings,
        which is perfectly healthy -- overwriting the count with 0 would make
        every cached tracker read as broken on the dashboard.
        """
        self.conn.execute(
            "INSERT INTO health (source, last_success, job_count, ok)"
            " VALUES (?,?,?,1)"
            " ON CONFLICT(source) DO UPDATE SET last_success=excluded.last_success,"
            " job_count=CASE WHEN excluded.job_count > 0 THEN excluded.job_count"
            "                ELSE health.job_count END,"
            " ok=1",
            (source, int(time.time()), count),
        )

    def health_fail(self, source: str, error: str) -> None:
        self.conn.execute(
            "INSERT INTO health (source, last_error, last_error_at, job_count, ok)"
            " VALUES (?,?,?,0,0)"
            " ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error,"
            " last_error_at=excluded.last_error_at, ok=0",
            (source, error[:500], int(time.time())),
        )

    def health_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM health ORDER BY ok, source"
        ).fetchall()

    # -- HTML fallback link state ----------------------------------------

    def new_links(self, source: str, urls: Iterable[str]) -> list[str]:
        """Return links never seen for this source, and remember them."""
        urls = list(dict.fromkeys(urls))
        if not urls:
            return []
        known = {
            r["url"]
            for r in self.conn.execute(
                "SELECT url FROM links WHERE source = ?", (source,)
            )
        }
        fresh = [u for u in urls if u not in known]
        now = int(time.time())
        self.conn.executemany(
            "INSERT OR IGNORE INTO links (source, url, seen) VALUES (?,?,?)",
            ((source, u, now) for u in urls),
        )
        return fresh

    # -- conditional-request cache ---------------------------------------

    def get_etag(self, key: str) -> Optional[str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT etag FROM cache WHERE key = ?", (key,)
            ).fetchone()
        return row["etag"] if row else None

    def set_etag(self, key: str, etag: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO cache (key, etag, at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET etag=excluded.etag, at=excluded.at",
                (key, etag, int(time.time())),
            )
            self.conn.commit()

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()
