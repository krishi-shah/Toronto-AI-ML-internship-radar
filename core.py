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
from urllib.parse import urlsplit

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
    r"\bon[\s\-]?campus\b",
    r"\bundergrad",
    r"\bapprentice",
    r"\btrainee\b",
    r"rotational",
    r"\bw2[5-9]\b",
    r"\bs2[5-9]\b",
    r"\bf2[5-9]\b",
]

# Full-time early-career roles. Still student-level for the loose tier, but
# never strict on their own: the radar hunts internships and co-ops, and a
# "New Grad ML Engineer" posting is a different race.
_NEWGRAD_TERMS = [
    r"new[\s\-]?grad",
    r"new[\s\-]?graduate",
    r"recent[\s\-]?graduate",
    r"university[\s\-]?graduate",
    r"campus[\s\-]?hire",
    r"early[\s\-]?career",
    r"early[\s\-]?talent",
    r"emerging[\s\-]?talent",
    r"entry[\s\-]?level",
    r"\bgraduate[\s\-]?program",
]

# Disciplines with no AI/ML angle. A student role in one of these is kept, but
# filed under "Other fields" instead of crowding the loose tier. Only consulted
# when the title carries no AI signal, so "ML Firmware Intern" is unaffected.
_OFF_FIELD_TERMS = [
    r"\banalog\b",
    r"mixed[\s\-]?signal",
    r"\bams\b",
    r"\bpcba?\b",
    r"\basic\b",
    r"\blayout\b",
    r"\bfirmware\b",
    r"signal[\s\-]?integrity",
    r"\bvalidation[\s\-]?engineer",
    r"hardware[\s\-]?(?:design|engineer)",
    r"\bic[\s\-]?design",
    r"\belectrical\b",
    r"\bmechanical\b",
    r"\bcivil\b",
    r"capital[\s\-]?markets",
    r"\baccounting\b",
    r"\baudit",
    r"\bmarketing\b",
    r"\bsales\b",
    r"human[\s\-]?resources",
    r"\bhr\b",
    r"talent[\s\-]?acquisition",
    r"financial[\s\-]?advisor",
    r"banking[\s\-]?advisor",
    r"client[\s\-]?advisor",
    r"relationship[\s\-]?manager",
    r"public[\s\-]?relations",
    r"\bhse\b",
    r"\btreasury\b",
    r"\bfp&a\b",
    r"\bunderwriting\b",
]

# Loose is for tech-relevant student roles. A student role with no AI signal
# needs one of these in its title to reach loose; without one it is filed
# under "Other fields". Off-field terms are checked first, so "Electrical
# Engineering Intern" still lands in other despite "engineering".
_TECH_TERMS = [
    r"\bsoftware\b",
    r"\bdevelop(?:er|ment)\b",
    r"\bengineer(?:ing|s)?\b",
    r"\bprogramm(?:er|ing)\b",
    r"\bcod(?:er|ing)\b",
    r"\bdata\b",
    r"\banalytics?\b",
    r"\bquant",
    r"\bresearch",
    r"\br\s*&(?:amp;)?\s*d\b",
    r"\bapplications?\b",
    r"\bcloud\b",
    r"\b(?:app|dev|devsec|sec|cloud|data|net|git|ml|ai)ops\b",
    r"\bsre\b",
    r"\binfrastructure\b",
    r"\bnetwork",
    r"\bsystems?\b",
    r"\bplatform\b",
    r"\bsecurity\b",
    r"\bcyber",
    r"\bqa\b",
    r"\btest",
    r"\bautomation\b",
    r"\bembedded\b",
    r"\brobotic",
    r"\bcomputer",
    r"\btech\b",
    r"\btechnolog",
    r"\btechnical\b",
    r"\bdigital\b",
    r"\bweb\b",
    r"\bmobile\b",
    r"\bdatabase",
    r"\bbi\b",
    r"\bproduct\b",
    r"\bux\b",
    r"\bui\b",
    r"\bgame",
    r"\belectronics\b",
    r"\btelecom",
    r"\bgis\b",
    r"geospatial",
    r"\bsimulation",
    r"\binformation\b",
]

# Retail floor and shift work that Workday tenants file as "Student". Checked
# against the title alone, so a company name can never trigger it.
_RETAIL_TERMS = [
    r"part[\s\-]?time",
    r"\bfranchise\b",
    r"greenlight[\s\-]?requisition",
    r"\bstore\b",
    r"\bcashier",
    r"\bcrew\b",
    r"pharmacy[\s\-]?student",
    r"\bcarvery\b",
    r"\bpizza\b",
    r"\bbakery\b",
    r"\bdeli\b",
    r"\bseafood\b",
    r"\bmeat[\s\-]?(?:cutter|clerk)",
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

# Ontario is the whole market. The radar exists for roles reachable from
# Toronto, so a Vancouver or Montreal posting is noise however good it looks.
#
# The province abbreviation is matched case-sensitively and needs the
# lookahead: with IGNORECASE, a bare ",\s*ON\b" happily matches the "on" in
# "Hybrid, on-site" -- and "London, on site" would read as London, Ontario.
_ONTARIO_TERMS = [
    # Ontario, California and Ontario, Oregon are US cities. ", CA" is not
    # guarded: Workday writes "Brampton, Ontario, CA" for Canada.
    r"\bontario\b(?!\s*,\s*(?:california|oregon)\b)",
    r"\btoronto\b",
    r"\bgta\b",
    r"greater[\s\-]?toronto",
    r"\bkanata\b",
    r"\bnepean\b",
    r"\bkitchener\b",
    r"\bguelph\b",
    r"\bmississauga\b",
    r"\bbrampton\b",
    r"\bvaughan\b",
    r"\bnorth[\s\-]?york\b",
    r"\betobicoke\b",
    r"\boshawa\b",
    r"\bajax\b",
    r"\bbarrie\b",
    r"\bwelland\b",
    r"\bst\.?\s*catharines\b",
    r"\bbrantford\b",
    r"\bsarnia\b",
    r"\bthunder[\s\-]?bay\b",
    r"\bcambridge,?\s*(?:(?-i:ON)(?![\w-])|ontario\b)",
    r"\bburlington,?\s*(?:(?-i:ON)(?![\w-])|ontario\b)",
    r"\bmilton,?\s*(?:(?-i:ON)(?![\w-])|ontario\b)",
    r"\blondon,?\s*(?:(?-i:ON)(?![\w-])|ontario\b)",
    r"\bkingston,?\s*(?:(?-i:ON)(?![\w-])|ontario\b)",
    r"\bwindsor,?\s*(?:(?-i:ON)(?![\w-])|ontario\b)",
    r"\bygk\b",
    r",\s*(?-i:ON)(?![\w-])",
    r",\s*ONT(?![\w-])",  # Oracle Cloud Recruiting: "Toronto, ONT, Canada"
    r"\b(?-i:ON)\s*,\s*Canada\b",
]

# Ontario city names that exist elsewhere too: Waterloo IA, Hamilton OH,
# Peterborough UK, Scarborough ME, Ottawa IL. Bare, they are almost always the
# Ontario one, but a qualifier after the comma has the final say -- otherwise
# John Deere's "Waterloo, IA" reads as Ontario and outranks the ", IA" code.
#
# The guard reads: not followed by a comma and then somewhere that is not
# Ontario or Canada. "Waterloo, ON" and "Waterloo, Ontario" survive it;
# "Waterloo, IA" and "Waterloo, Iowa" do not.
_ELSEWHERE = (
    r"(?!\s*,\s*(?!ON(?![\w-]))(?:[A-Za-z]{2}(?![\w-])|iowa|ohio|illinois|"
    r"maine|massachusetts|michigan|minnesota|missouri|kansas|indiana|"
    r"wisconsin|georgia|colorado|connecticut|vermont|virginia|new\s+jersey|"
    r"new\s+york|england|scotland|wales|australia|belgium))"
)

_AMBIGUOUS_ONTARIO_CITIES = [
    r"\bwaterloo\b",
    r"\bhamilton\b",
    r"\bottawa\b",
    r"\bmarkham\b",
    r"\boakville\b",
    r"\bwoodbridge\b",
    r"\brichmond[\s\-]?hill\b",
    r"\bscarborough\b",
    r"\bwhitby\b",
    r"\bpickering\b",
    r"\bnewmarket\b",
    r"\bpeterborough\b",
    r"\bsudbury\b",
    r"\bniagara\b",
]

# A street address is not the city: RTX's "1 Hamilton Rd" is in Connecticut.
_NOT_STREET = r"(?!\s+(?:rd|road|st|street|ave|avenue|dr|drive|blvd|way|lane|ln)\b)"

_ONTARIO_TERMS += [
    city + _NOT_STREET + _ELSEWHERE for city in _AMBIGUOUS_ONTARIO_CITIES
]

# The commute area: the GTA plus Hamilton, Waterloo Region and Guelph. The
# radar is for roles reachable from Toronto day to day, so an Ontario posting
# that names only places outside this (Ottawa, London, Kingston, ...) is
# rejected. A posting naming any commutable place stays, so "Ottawa; Toronto"
# is a Toronto role.
_COMMUTE_TERMS = [
    r"\btoronto\b",
    r"\bgta\b",
    r"greater[\s\-]?toronto",
    r"\bmississauga\b",
    r"\bbrampton\b",
    r"\bvaughan\b",
    r"\bnorth[\s\-]?york\b",
    r"\betobicoke\b",
    r"\bthornhill\b",
    r"\boshawa\b",
    r"\bajax\b",
    r"\bkitchener\b",
    r"\bguelph\b",
    r"\bygk\b",
] + [
    city + _NOT_STREET + _ELSEWHERE
    for city in (
        r"\bwaterloo\b", r"\bhamilton\b", r"\bmarkham\b", r"\boakville\b",
        r"\bwoodbridge\b", r"\brichmond[\s\-]?hill\b", r"\bscarborough\b",
        r"\bwhitby\b", r"\bpickering\b", r"\bnewmarket\b", r"\baurora\b",
        r"\bconcord\b", r"\bburlington\b", r"\bmilton\b", r"\bcambridge\b",
    )
]

# Ontario, but too far to commute from Toronto.
_FAR_ONTARIO_TERMS = [
    r"\bkanata\b",
    r"\bnepean\b",
    r"\bbarrie\b",
    r"\bbrantford\b",
    r"\bwelland\b",
    r"\bst\.?\s*catharines\b",
    r"\bsarnia\b",
    r"\bthunder[\s\-]?bay\b",
    r"\bbelleville\b",
    r"\bnorth[\s\-]?bay\b",
    r"\bsault\s+ste\.?\s*marie\b",
    # "ONT" is Oracle Cloud Recruiting's spelling of the province.
    r"\blondon,?\s*(?:(?-i:ONT?)(?![\w-])|ontario\b)",
    r"\bkingston,?\s*(?:(?-i:ONT?)(?![\w-])|ontario\b)",
    r"\bwindsor,?\s*(?:(?-i:ONT?)(?![\w-])|ontario\b)",
] + [
    city + _NOT_STREET + _ELSEWHERE
    for city in (r"\bottawa\b", r"\bpeterborough\b", r"\bsudbury\b", r"\bniagara\b")
]

# Canadian, but not Ontario. Naming one of these rules a posting out unless
# Ontario is named alongside it -- a role listed for both Toronto and
# Vancouver is still a Toronto role.
_NON_ONTARIO_CA_TERMS = [
    r"\bvancouver\b",
    r"\bburnaby\b",
    r"\bkelowna\b",
    r"\bvictoria,?\s*(?:bc|british)\b",
    r"british[\s\-]?columbia",
    r"\bmontr[eé]al\b",
    r"\bqu[eé]bec\b",
    r"\blaval\b",
    r"\bgatineau\b",
    r"\bsherbrooke\b",
    r"\blongueuil\b",
    r"saint[\s\-]?hubert",
    r"\bcalgary\b",
    r"\bedmonton\b",
    r"\balberta\b",
    r"\bacheson\b",
    r"\bwinnipeg\b",
    r"\bmanitoba\b",
    r"\bhalifax\b",
    r"nova[\s\-]?scotia",
    r"\bmoncton\b",
    r"\bfredericton\b",
    r"\bsaskatoon\b",
    r"\bregina\b",
    r"saskatchewan",
    r"new[\s\-]?brunswick",
    r"newfoundland",
    r"prince[\s\-]?edward[\s\-]?island",
    r"\byukon\b",
    r"\bnunavut\b",
    r"northwest[\s\-]?territories",
    # Case-sensitive like ", ON": lowercase prose is not a province code.
    r",\s*(?-i:QC|BC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)(?![\w-])",
]

# Country-level only. This exists so "Remote in Canada" -- which names no
# province at all -- stays in scope, since it is workable from Toronto.
_CANADA_TERMS = [
    r"\bcanada\b",
    r"\bcanadian\b",
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
# "2027 Winter" -- RBC and others put the year first.
_CYCLE_YEAR_FIRST_RE = re.compile(
    r"\b(20\d{2})[\s\-–]*(winter|spring|summer|fall|autumn)\b", re.IGNORECASE
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
    # Only text the season-first form did not claim, or "Summer 2026, Winter
    # 2027" would also read as "2026 Winter".
    for year, season in _CYCLE_YEAR_FIRST_RE.findall(_CYCLE_LONG_RE.sub(" ", text or "")):
        found.add((_norm_year(year), _SEASON_MONTH[season.lower()]))
    for letter, year in _CYCLE_SHORT_RE.findall(text or ""):
        found.add((_norm_year(year), _SEASON_MONTH[letter.lower()]))
    return sorted(found)


def _compile(terms: Iterable[str]) -> re.Pattern:
    return re.compile("|".join(terms), re.IGNORECASE)


AI_RE = _compile(_AI_TERMS)
STUDENT_RE = _compile(_STUDENT_TERMS)
NEWGRAD_RE = _compile(_NEWGRAD_TERMS)
OFF_FIELD_RE = _compile(_OFF_FIELD_TERMS)
# "IT" is matched case-sensitively so the pronoun "it" never counts as tech.
TECH_RE = re.compile("|".join(_TECH_TERMS), re.IGNORECASE)
TECH_IT_RE = re.compile(r"\bIT\b")
RETAIL_RE = _compile(_RETAIL_TERMS)
REJECT_RE = _compile(_REJECT_TERMS)
ONTARIO_RE = _compile(_ONTARIO_TERMS)
COMMUTE_RE = _compile(_COMMUTE_TERMS)
FAR_ONTARIO_RE = _compile(_FAR_ONTARIO_TERMS)
NON_ONTARIO_CA_RE = _compile(_NON_ONTARIO_CA_TERMS)
CANADA_RE = _compile(_CANADA_TERMS)
REMOTE_RE = _compile(_REMOTE_TERMS)

# Places that positively rule a posting out, unless Ontario is named alongside.
#
# The spelled-out US states and the city names below are the fix for a real
# leak: this pattern knew "united kingdom" but not "UK", and ", NY" but not
# "Texas", so "Barclays - London, UK" and "Hitachi - Texas" were read as
# having no readable location and kept in the loose tier.
_FOREIGN_RE = re.compile(
    r"\b(?:u\.?k\.?|scotland|wales|northern\s+ireland)\b"
    r"|\b(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
    r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new\s+hampshire|"
    r"new\s+jersey|new\s+mexico|new\s+york|north\s+carolina|north\s+dakota|"
    r"ohio|oklahoma|oregon|pennsylvania|rhode\s+island|south\s+carolina|"
    r"south\s+dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"west\s+virginia|wisconsin|wyoming)\b"
    r"|\b(?:san\s+francisco|san\s+jose|san\s+diego|palo\s+alto|mountain\s+view|"
    r"sunnyvale|cupertino|bellevue|redmond|los\s+angeles|seattle|austin|"
    r"boston|chicago|denver|atlanta|dallas|houston|phoenix|miami|"
    r"philadelphia|pittsburgh|detroit|minneapolis|cambridge,\s*ma)\b"
    r"|\b(?:united\s+states|usa|u\.s\.a?\.|india|united\s+kingdom|england|ireland|"
    r"germany|france|spain|poland|romania|netherlands|sweden|norway|denmark|"
    r"switzerland|israel|singapore|japan|china|korea|australia|new\s+zealand|"
    r"brazil|mexico|argentina|philippines|vietnam|taiwan|hong\s+kong|uae|"
    r"dubai|egypt|nigeria|kenya|south\s+africa|portugal|italy|austria|belgium|"
    r"czech|hungary|greece|turkey|finland|estonia|lithuania|ukraine|"
    r"costa\s+rica|colombia|chile|peru|malaysia|indonesia|thailand|pakistan|"
    r"bangladesh|sri\s+lanka)\b"
    # State codes are case-sensitive: under IGNORECASE, ", or remote" read as
    # Oregon and ", in the US" as Indiana.
    r"|,\s*(?-i:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|MA|MD|ME|"
    r"MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
    r"VA|VT|WA|WI|WV|WY|DC)\b"
    # Workday site codes: "US-CT-WINDSOR LOCKS-B2".
    r"|\bUS-[A-Z]{2}-",
    re.IGNORECASE,
)

# Tracker repos publish bare city abbreviations as the entire location field
# ("SF", "NYC", "LA"). Matched case-sensitively on purpose: a case-insensitive
# "\bla\b" would fire on French-language location text.
_FOREIGN_ABBR_RE = re.compile(r"\b(?:SF|LA|NYC|DC)\b")


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

STRICT = "strict"
LOOSE = "loose"
OTHER = "other"
REJECTED = None


@dataclass
class Verdict:
    """Why a posting landed where it did. Surfaced by ``--check`` for tuning."""

    tier: Optional[str]
    reason: str
    ai: bool = False
    student: bool = False
    ontario: bool = False
    location_known: bool = True
    cycle: str = "unknown"
    # The ML terms a description matched, when that is what made it AI.
    ai_terms: list[str] = field(default_factory=list)

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


def _location_scope(loc_blob: str) -> str:
    """Where a location string puts a posting.

    ``ontario``   the commute area named, Ontario with no city, or remote
                  within Canada.
    ``far``       Ontario, but only places outside the commute area.
    ``canada``    Canada named with no province, nothing foreign alongside.
    ``elsewhere`` another province or another country.
    ``unknown``   nothing recognisable at all.
    """
    ontario = bool(ONTARIO_RE.search(loc_blob))
    elsewhere_in_canada = bool(NON_ONTARIO_CA_RE.search(loc_blob))
    remote = bool(REMOTE_RE.search(loc_blob))
    canada = bool(CANADA_RE.search(loc_blob))
    foreign = bool(_FOREIGN_RE.search(loc_blob)) or bool(
        _FOREIGN_ABBR_RE.search(loc_blob)
    )

    # Ontario named anywhere wins, checked first on purpose: Ashby routinely
    # lists a US primary location with Toronto in secondaryLocations, and a
    # req open in both Toronto and Vancouver is still a Toronto req.
    #
    # "Remote in Canada" names no province at all, so it is taken on trust --
    # it is workable from Toronto. Bare "Remote" is not: it says nothing about
    # the country.
    if ontario or (remote and canada and not elsewhere_in_canada):
        # A province-only location ("Ontario, Canada", ", ON") is kept: it is
        # almost always the Toronto head office. Remote work is workable from
        # Toronto wherever the team sits.
        if (FAR_ONTARIO_RE.search(loc_blob) and not COMMUTE_RE.search(loc_blob)
                and not remote):
            return "far"
        return "ontario"
    # "Canada" with no province is in Canada but not confirmably Ontario, so
    # it may reach loose and never strict.
    if canada and not elsewhere_in_canada and not foreign:
        return "canada"
    if elsewhere_in_canada or foreign:
        return "elsewhere"
    return "unknown"


def names_a_place(location: str) -> bool:
    """Does this location string say anything the classifier can place?"""
    return _location_scope(location or "") != "unknown"


def in_canada(posting: Posting) -> bool:
    """Would the classifier's location gate let this posting through?"""
    return _location_scope(_location_blob(posting)) in ("ontario", "canada")


def classify(posting: Posting, description: Optional[str] = None) -> Verdict:
    """Route a posting to the instant tier, the 5pm digest, or the bin.

    Strict requires all of:
      * an AI/ML signal in the title, or a company flagged ``ai_native``, or
        -- for a tech title -- a ``description`` naming enough ML terms
      * an internship-level signal (new-grad-only roles cap at loose)
      * a location in the commute area (the GTA, Hamilton, Waterloo Region,
        Guelph), Ontario with no city named, or remote within Canada
      * no cycle token pointing exclusively at a season other than Winter 2027

    Anything else student-level in the commute area (or in Canada with no
    province) falls through to loose only when the title is tech-relevant. A
    title naming a discipline with no AI angle (analog design, capital
    markets, branch advisor, ...) or no tech term at all goes to ``other``.
    Dropped outright: unpaid, volunteer, high-school and PhD-only postings,
    retail and shift work, past cycles, Ontario outside the commute area
    (Ottawa, London, Kingston, ...), and the rest of Canada.
    """
    title = posting.title or ""
    loc_blob = _location_blob(posting)
    haystack = f"{title} | {posting.company} | {loc_blob}"

    if REJECT_RE.search(haystack):
        return Verdict(REJECTED, "reject term")

    # Some boards keep the level out of the title entirely.
    level_text = f"{title} | {loc_blob} | {(posting.raw or {}).get('employmentType', '')}"
    internship = bool(STUDENT_RE.search(level_text))
    new_grad = not internship and bool(NEWGRAD_RE.search(level_text))
    if not (internship or new_grad):
        return Verdict(REJECTED, "not student level", student=False)

    scope = _location_scope(loc_blob)
    in_scope = scope == "ontario"
    canada_only = scope == "canada"
    location_known = bool(loc_blob.strip())

    if not (in_scope or canada_only):
        if scope == "far":
            return Verdict(
                REJECTED, "outside commute area", student=True, location_known=True,
            )
        if scope == "elsewhere":
            return Verdict(
                REJECTED, "outside Ontario", student=True, location_known=True,
            )
        # A positive Canadian signal is required. Unreadable locations used to
        # be kept in case they were Toronto, and in practice they were almost
        # all foreign: Bengaluru, "London, GBR", "FL JAX 347", "Remote in US".
        return Verdict(
            REJECTED, "no Canadian location", student=True,
            location_known=location_known,
        )

    cycle = _cycle_verdict(f"{haystack} | {_cycle_blob(posting)}")
    if cycle == "past":
        return Verdict(
            REJECTED, "cycle already passed", student=True, ontario=True, cycle=cycle
        )

    ai = bool(AI_RE.search(title)) or posting.ai_native

    if not ai:
        if RETAIL_RE.search(title):
            return Verdict(
                REJECTED, "store or shift role", student=True, ontario=in_scope,
            )
        if OFF_FIELD_RE.search(title):
            return Verdict(
                OTHER, "off-field", student=True, ontario=in_scope, cycle=cycle,
            )
        if not (TECH_RE.search(title) or TECH_IT_RE.search(title)):
            return Verdict(
                OTHER, "not tech", student=True, ontario=in_scope, cycle=cycle,
            )

    # A tech title with no AI word can still be ML work: "Software Developer
    # Co-op" on a model-training team. The description settles it when given.
    ai_terms: list[str] = []
    if not ai and description:
        score, ai_terms = ai_description_score(description)
        if score >= _desc_min_terms():
            ai = True
        else:
            ai_terms = []

    if canada_only:
        return Verdict(
            LOOSE, "Canada, province not named", ai=ai, student=True, cycle=cycle,
        )

    if not ai:
        return Verdict(LOOSE, "no AI signal", student=True, ontario=True, cycle=cycle)
    if new_grad:
        return Verdict(
            LOOSE, "new grad, not internship", ai=True, student=True,
            ontario=True, cycle=cycle, ai_terms=ai_terms,
        )
    if cycle == "later":
        return Verdict(
            LOOSE, "later cycle", ai=True, student=True, ontario=True, cycle=cycle,
            ai_terms=ai_terms,
        )

    if ai_terms:
        return Verdict(STRICT, AI_IN_DESCRIPTION, ai=True, student=True,
                       ontario=True, cycle=cycle, ai_terms=ai_terms)
    return Verdict(STRICT, "ai + student + ontario", ai=True, student=True,
                   ontario=True, cycle=cycle)


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


_SEASON_NAME = {1: "Winter", 4: "Spring", 5: "Summer", 9: "Fall"}


def term_label(posting: Posting) -> str:
    """The work term(s) a posting names, for the feed: "Winter 2027".

    Cycles before the targeted one are left out when a current one is also
    named, so "Summer 2026, Winter 2027" shows as "Winter 2027".
    """
    cycles = parse_cycles(f"{posting.title} | {_cycle_blob(posting)}")
    current = [c for c in cycles if c >= _target_cycle()] or cycles
    return ", ".join(f"{_SEASON_NAME[month]} {year}" for year, month in current)


# --------------------------------------------------------------------------
# Description scoring
#
# Only for tech titles with no AI word in them. Each entry is (label, regex),
# and the score is the number of distinct labels found. A bare "AI" never
# counts: company boilerplate says "AI-powered" on every posting.
# --------------------------------------------------------------------------

AI_IN_DESCRIPTION = "AI in description"

_AI_DESC_TERMS = [
    ("machine learning", r"machine[\s\-]?learning"),
    ("deep learning", r"deep[\s\-]?learning"),
    ("LLMs", r"\bllms?\b|large[\s\-]?language[\s\-]?models?"),
    ("PyTorch", r"\bpytorch\b"),
    ("TensorFlow", r"\btensorflow\b"),
    ("JAX", r"\bjax\b"),
    ("scikit-learn", r"scikit[\s\-]?learn|\bsklearn\b"),
    ("model training", r"\btrain(?:ing|ed)?\s+(?:ml\s+|ai\s+|the\s+)?models?\b|model\s+training"),
    ("fine-tuning", r"fine[\s\-]?tun(?:e|ing)"),
    ("NLP", r"\bnlp\b|natural[\s\-]?language[\s\-]?processing"),
    ("computer vision", r"computer[\s\-]?vision"),
    ("MLOps", r"\bmlops\b"),
    ("RAG", r"\brag\b|retrieval[\s\-]?augmented"),
    ("embeddings", r"\bembeddings?\b|vector[\s\-]?(?:database|search|store)"),
    ("reinforcement learning", r"reinforcement[\s\-]?learning"),
    ("data science", r"data[\s\-]?scien(?:ce|tist)"),
    ("feature engineering", r"feature[\s\-]?engineering"),
    ("neural networks", r"neural[\s\-]?net(?:work)?s?"),
    ("transformers", r"\btransformers?\b(?!\s+(?:station|substation))"),
    ("CUDA", r"\bcuda\b"),
    ("generative AI", r"generative[\s\-]?ai|\bgenai\b|gen[\s\-]ai\b"),
    ("ML engineering", r"\bml\s+(?:engineer|pipeline|model|system|infrastructure|platform)"),
    ("AI agents", r"\b(?:ai|llm)[\s\-]?agents?\b|agentic"),
]
_AI_DESC_RES = [(label, re.compile(rx, re.IGNORECASE)) for label, rx in _AI_DESC_TERMS]
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def ai_description_score(text: str) -> tuple[int, list[str]]:
    """Distinct ML terms in a job description, as ``(count, labels)``."""
    plain = _HTML_TAG_RE.sub(" ", text or "")
    terms = [label for label, rx in _AI_DESC_RES if rx.search(plain)]
    return len(terms), terms


def _config_int(name: str, default: int) -> int:
    """An integer setting from companies.py, or ``default`` without one."""
    try:
        import companies

        return int(getattr(companies, name, default))
    except Exception:  # noqa: BLE001 - config is optional for library use
        return default


def _desc_min_terms() -> int:
    """How many distinct ML terms a description needs to make a role AI."""
    return _config_int("AI_DESC_MIN_TERMS", 3)


def closed_after_misses() -> int:
    """Complete fetches a posting may be missing from before it counts as closed."""
    return max(1, _config_int("CLOSED_AFTER_MISSES", 2))


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
    if s == "bankofmontreal":
        return "bmo"
    return s


def normalize_title(title: str) -> str:
    """Reduce a job title to a comparable key.

    Strips cycle labels, req ids, location suffixes and emoji, and folds the
    co-op / internship / new-grad spelling variants onto one token each.
    """
    s = (title or "").lower()
    s = s.replace("&", " and ")
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


# Provider requisition ids in posting URLs. Trackers retitle roles ("AI
# Developer Intern (Winter 2027)" for RBC's "2027 Winter – GRM, AI Developer
# Intern - Innovation & AI Center of Excellence (4 Months)") but keep linking
# the same posting, so the id in the URL is what ties the copies together.
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
# Workday: the last path piece ends "_R-0000188001", "_JR00128581-1",
# "_R10571" or "_REQ-2393". The "-1" suffix marks the same req re-posted on a
# second site, so it is dropped.
_WORKDAY_REQ_RE = re.compile(r"_([A-Z]{1,4}-?\d{3,})(?:-\d{1,2})?/?$")
# Greenhouse "gh_jid=", and "/jobs/123456" on Greenhouse, iCIMS, Jibe, Amazon,
# Oracle ("/job/123") and SmartRecruiters.
_GH_JID_RE = re.compile(r"[?&]gh_jid=(\d+)")
_PATH_ID_RE = re.compile(r"/(?:jobs?|postings?|positions?)/(\d{4,})(?=[/?#\-]|$)")


def requisition_id(url: str) -> str:
    """The provider's own id for a posting, read from its URL, or "" if none."""
    raw = (url or "").strip()
    if not raw:
        return ""
    match = _GH_JID_RE.search(raw)
    if match:
        return match.group(1)
    path = urlsplit(raw).path
    match = _WORKDAY_REQ_RE.search(path)
    if match:
        return match.group(1).replace("-", "").upper()
    match = _UUID_RE.search(path)
    if match:
        return match.group(0).lower()
    match = _PATH_ID_RE.search(path)
    if match:
        return match.group(1)
    return ""


def role_keys(company: str, title: str, url: str) -> list[str]:
    """Every key under which two stored rows count as the same role.

    The company+title fingerprint, plus the requisition id scoped both to the
    company and to the URL's site -- a tracker may spell the company
    differently ("RBC") while linking the employer's own posting.
    """
    keys = [f"fp:{fingerprint(company, title)}"]
    req = requisition_id(url)
    if req:
        keys.append(f"req:{normalize_company(company)}:{req}")
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        if host:
            keys.append(f"req@{host}:{req}")
    return keys


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
    digested     INTEGER NOT NULL DEFAULT 0,
    group_id     TEXT,
    last_seen    INTEGER NOT NULL DEFAULT 0,
    misses       INTEGER NOT NULL DEFAULT 0,
    term         TEXT NOT NULL DEFAULT '',
    ai_terms     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(tier, notified, digested);
CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source);

-- Every key a row is known by (fingerprint, requisition id), mapped to the
-- group of rows that are one role. See core.role_keys.
CREATE TABLE IF NOT EXISTS role_keys (
    key      TEXT PRIMARY KEY,
    group_id TEXT NOT NULL
);

-- Descriptions already read, so each posting is fetched once.
CREATE TABLE IF NOT EXISTS descriptions (
    uid        TEXT PRIMARY KEY,
    checked_at INTEGER NOT NULL,
    score      INTEGER NOT NULL DEFAULT 0,
    terms      TEXT NOT NULL DEFAULT '',
    error      TEXT
);

CREATE TABLE IF NOT EXISTS health (
    source       TEXT PRIMARY KEY,
    last_success INTEGER,
    last_error   TEXT,
    last_error_at INTEGER,
    job_count    INTEGER DEFAULT 0,
    ok           INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS discovered (
    platform     TEXT NOT NULL,
    token        TEXT NOT NULL,
    name         TEXT NOT NULL,
    first_seen   INTEGER NOT NULL,
    last_canada  INTEGER NOT NULL,
    last_ok      INTEGER,
    last_error   TEXT,
    fails        INTEGER NOT NULL DEFAULT 0,
    canada_hits  INTEGER NOT NULL DEFAULT 0,
    job_count    INTEGER NOT NULL DEFAULT 0,
    last_attempt INTEGER,
    student_jobs INTEGER NOT NULL DEFAULT 0,
    strict_jobs  INTEGER NOT NULL DEFAULT 0,
    ok_streak    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, token)
);

CREATE TABLE IF NOT EXISTS hosts (
    host          TEXT PRIMARY KEY,
    sample_url    TEXT NOT NULL,
    name          TEXT,
    hits          INTEGER NOT NULL DEFAULT 0,
    last_seen     INTEGER NOT NULL,
    last_sniffed  INTEGER,
    result        TEXT
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
        for column, decl in (
            ("last_seen", "INTEGER NOT NULL DEFAULT 0"),
            ("misses", "INTEGER NOT NULL DEFAULT 0"),
            ("term", "TEXT NOT NULL DEFAULT ''"),
            ("ai_terms", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in have:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {decl}")
        if "group_id" not in have:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN group_id TEXT")
            self._backfill_groups()
        self.conn.execute("CREATE INDEX IF NOT EXISTS jobs_group ON jobs(group_id)")
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(discovered)")}
        if "last_attempt" not in have:
            self.conn.execute("ALTER TABLE discovered ADD COLUMN last_attempt INTEGER")
        for column, decl in (
            ("student_jobs", "INTEGER NOT NULL DEFAULT 0"),
            ("strict_jobs", "INTEGER NOT NULL DEFAULT 0"),
            ("ok_streak", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in have:
                self.conn.execute(f"ALTER TABLE discovered ADD COLUMN {column} {decl}")

    def _backfill_groups(self) -> None:
        """Group rows stored before role grouping existed, oldest first."""
        rows = self.conn.execute(
            "SELECT uid, company, title, url FROM jobs ORDER BY first_seen, uid"
        ).fetchall()
        for row in rows:
            group = self._assign_group(
                row["uid"], role_keys(row["company"], row["title"], row["url"] or "")
            )
            self.conn.execute(
                "UPDATE jobs SET group_id = ? WHERE uid = ?", (group, row["uid"])
            )

    def close(self) -> None:
        self.conn.close()

    # -- role groups ------------------------------------------------------

    def _group_of(self, keys: list[str]) -> Optional[str]:
        """The group already holding any of ``keys``, or None."""
        for key in keys:
            row = self.conn.execute(
                "SELECT group_id FROM role_keys WHERE key = ?", (key,)
            ).fetchone()
            if row:
                return row["group_id"]
        return None

    def _assign_group(self, uid: str, keys: list[str]) -> str:
        """Put ``keys`` in the group of any row sharing one, else a new group."""
        group = self._group_of(keys) or uid
        self.conn.executemany(
            "INSERT OR IGNORE INTO role_keys (key, group_id) VALUES (?,?)",
            ((key, group) for key in keys),
        )
        return group

    def group_for(self, posting: Posting) -> Optional[str]:
        """The stored group this posting belongs to, if any copy is stored."""
        return self._group_of(role_keys(posting.company, posting.title, posting.url))

    def delivered(self, posting: Posting, tier: Optional[str]) -> bool:
        """:meth:`fingerprint_delivered`, but across the posting's whole group.

        Catches the same role under a different title -- a tracker's rewording
        of the employer's posting -- through the requisition id in its URL.
        """
        group = self.group_for(posting)
        if group is None:
            return False
        if tier == STRICT:
            cur = self.conn.execute(
                "SELECT 1 FROM jobs WHERE group_id = ? AND tier = ? AND notified = 1",
                (group, STRICT),
            )
        else:
            cur = self.conn.execute(
                "SELECT 1 FROM jobs WHERE group_id = ? AND tier IS NOT NULL", (group,)
            )
        return cur.fetchone() is not None

    # -- postings ---------------------------------------------------------

    def seen_uid(self, uid: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM jobs WHERE uid = ?", (uid,))
        return cur.fetchone() is not None

    def fingerprint_delivered(self, fp: str, tier: Optional[str]) -> bool:
        """Has this role already been delivered at ``tier`` or better?

        Fingerprints deliberately ignore cycle labels, so "ML Intern, Fall
        2026" and "ML Intern (Winter 2027)" collapse together. That is right
        for dedupe but wrong for suppression: an off-cycle posting lands in
        loose, and if it merely being *seen* silenced the fingerprint, the
        real Winter 2027 posting arriving later would never ping.

        So a strict candidate is only suppressed by a strict hit that was
        actually notified. A loose candidate is suppressed by any sighting
        that was kept: a rejected copy (the Vancouver listing, a past cycle)
        must not silence the same role arriving later in Ontario.
        """
        if tier == STRICT:
            cur = self.conn.execute(
                "SELECT 1 FROM jobs WHERE fingerprint = ? AND tier = ?"
                " AND notified = 1",
                (fp, STRICT),
            )
        else:
            cur = self.conn.execute(
                "SELECT 1 FROM jobs WHERE fingerprint = ? AND tier IS NOT NULL",
                (fp,),
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
        ai_terms: Iterable[str] = (),
    ) -> None:
        """Insert a posting. Existing uids are left untouched so the
        ``first_seen`` timestamp and notified flags never reset.

        The row joins the group of any stored copy of the same role -- same
        fingerprint or same requisition id -- so the feed lists it once.
        """
        if self.seen_uid(posting.uid):
            return
        now = int(time.time())
        group = self._assign_group(
            posting.uid, role_keys(posting.company, posting.title, posting.url)
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO jobs (uid, fingerprint, company, title, location,"
            " url, source, tier, reason, first_seen, posted_at, seeded,"
            " notified, digested, group_id, last_seen, term, ai_terms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                now,
                int(posting.posted_at or 0),
                int(seeded),
                int(notified),
                int(digested),
                group,
                now,
                term_label(posting),
                ", ".join(ai_terms),
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

    # Copies restated by an aggregator rather than posted by the employer.
    # When both exist, the employer's own copy is the one listed, and only the
    # employer's board closing a role is taken as the role being gone.
    _AGGREGATOR = "(uid LIKE 'tracker:%' OR uid LIKE 'getro:%' OR uid LIKE 'consider:%')"

    def _feed_query(self, tier: str, max_age_hours: Optional[int]) -> tuple[str, list]:
        """SQL for the feed rows of one tier, one row per role, and its params."""
        age_expr = "COALESCE(NULLIF(posted_at, 0), first_seen)"
        closed_after = closed_after_misses()
        params: list = [closed_after, tier]
        # A posting with no provider date that was recorded during the seed has
        # an unknown age -- first_seen is just when seeding ran, so it would
        # masquerade as brand new forever. Undated rows only count as fresh
        # when the radar genuinely discovered them after seeding.
        clause = " AND NOT (posted_at = 0 AND seeded = 1)"
        if max_age_hours:
            clause += f" AND {age_expr} >= ?"
            params.append(int(time.time()) - max_age_hours * 3600)
        params.append(closed_after)
        # One row per role. A role on both its own board and a tracker is
        # stored twice (dedupe only suppresses the second alert), and listing
        # both would put every overlap in the feed twice. Rows are one role
        # when they share a fingerprint or a requisition id (``group_id``).
        # The employer's copy wins, then the one with a provider date.
        #
        # A role is gone when its source stopped listing it for
        # CLOSED_AFTER_MISSES complete fetches in a row (``misses``).
        sql = (
            "SELECT * FROM ("
            " SELECT *, ROW_NUMBER() OVER (PARTITION BY grp"
            "   ORDER BY aggregator ASC, (posted_at > 0) DESC, first_seen ASC) AS copy"
            " FROM ("
            f"  SELECT *, {age_expr} AS shown_at,"
            "   COALESCE(group_id, fingerprint) AS grp,"
            f"  {self._AGGREGATOR} AS aggregator,"
            f"  MAX(CASE WHEN misses >= ? AND NOT {self._AGGREGATOR} THEN 1 ELSE 0 END)"
            "     OVER (PARTITION BY COALESCE(group_id, fingerprint)) AS group_closed"
            f"  FROM jobs WHERE tier = ?{clause}"
            " ) WHERE group_closed = 0 AND misses < ?"
            ") WHERE copy = 1"
        )
        return sql, params

    def recent_jobs(
        self, tier: str, limit: int = 60, max_age_hours: Optional[int] = None
    ) -> list[sqlite3.Row]:
        """Postings in one tier, freshest first, one row per role.

        Ordered and filtered by when the *provider* published the role, falling
        back to when we discovered it for sources that expose no date. A role
        that has been live for days already has hundreds of applicants, so
        ``max_age_hours`` keeps the list to things still worth racing for.
        Postings their source has stopped listing are left out.
        """
        sql, params = self._feed_query(tier, max_age_hours)
        cur = self.conn.execute(
            f"{sql} ORDER BY shown_at DESC LIMIT ?", params + [limit]
        )
        return cur.fetchall()

    def count_jobs(self, tier: str, max_age_hours: Optional[int] = None) -> int:
        """How many roles :meth:`recent_jobs` would list with no limit."""
        sql, params = self._feed_query(tier, max_age_hours)
        return self.conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]

    # -- presence: hiding postings their source no longer lists ----------

    def mark_present(self, source: str, uids: Iterable[str], stamp: int) -> None:
        """``source`` returned a complete listing at ``stamp``.

        Every stored row of it in the listing is reset; every one that was
        not has missed once more. Only call this for a listing known to be
        complete -- a capped search or a 304 says nothing about absence.
        """
        with self._lock:
            self.conn.executemany(
                "UPDATE jobs SET last_seen = ?, misses = 0 WHERE uid = ?",
                ((stamp, uid) for uid in uids),
            )
            self.conn.execute(
                "UPDATE jobs SET misses = misses + 1 WHERE source = ? AND last_seen < ?",
                (source, stamp),
            )

    # -- descriptions -----------------------------------------------------

    def description_checked(self, uid: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM descriptions WHERE uid = ?", (uid,)
        ).fetchone() is not None

    def record_description(
        self, uid: str, score: int, terms: Iterable[str], error: Optional[str] = None
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO descriptions (uid, checked_at, score, terms, error)"
            " VALUES (?,?,?,?,?)",
            (uid, int(time.time()), score, ", ".join(terms), (error or None) and error[:300]),
        )

    def description_backlog(self, limit: int, max_age_hours: int) -> list[sqlite3.Row]:
        """Stored loose rows a description might promote, newest first.

        Only rows that missed strict for want of an AI word in the title, that
        are still inside the feed window, and whose description was never read.
        """
        cutoff = int(time.time()) - max_age_hours * 3600
        return self.conn.execute(
            "SELECT * FROM jobs WHERE tier = ? AND reason = 'no AI signal'"
            " AND COALESCE(NULLIF(posted_at, 0), first_seen) >= ?"
            " AND uid NOT IN (SELECT uid FROM descriptions)"
            " ORDER BY COALESCE(NULLIF(posted_at, 0), first_seen) DESC LIMIT ?",
            (LOOSE, cutoff, limit),
        ).fetchall()

    def update_verdict(
        self, uid: str, tier: Optional[str], reason: str, ai_terms: Iterable[str] = ()
    ) -> None:
        self.conn.execute(
            "UPDATE jobs SET tier = ?, reason = ?, ai_terms = ? WHERE uid = ?",
            (tier, reason, ", ".join(ai_terms), uid),
        )

    def update_place(self, uid: str, location: str, posted_at: int) -> None:
        """Fill a location and date read from the posting page itself."""
        self.conn.execute(
            "UPDATE jobs SET location = ?,"
            " posted_at = CASE WHEN posted_at = 0 THEN ? ELSE posted_at END"
            " WHERE uid = ?",
            (location, int(posted_at or 0), uid),
        )

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

    # -- discovered boards -----------------------------------------------

    # A board is dropped from the rotation after this many failures in a row,
    # or after this long without evidence of a Canadian posting.
    DISCOVERY_MAX_FAILS = 5
    DISCOVERY_STALE_DAYS = 30

    def discovered_add(self, platform: str, token: str, name: str, hits: int) -> None:
        """Remember a board seen in this run's Canadian postings.

        Being named by a Canadian posting again counts as fresh evidence, so
        it renews ``last_canada``. It never resets ``fails``: a board that is
        down stays out however often trackers link to it.
        """
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO discovered (platform, token, name, first_seen, last_canada,"
            " canada_hits) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(platform, token) DO UPDATE SET last_canada=excluded.last_canada,"
            " canada_hits=MAX(discovered.canada_hits, excluded.canada_hits)",
            (platform, token, name, now, now, hits),
        )

    def discovered_pick(self, limit: int, top: int = 0) -> list[sqlite3.Row]:
        """The boards to scrape this run, in the order to scrape them.

        Healthy, recent boards only. The ``top`` most Canadian go first every
        run; the rest follow least-recently-attempted first, so boards a time
        budget cut off get their turn on the next run instead of starving.
        """
        cutoff = int(time.time()) - self.DISCOVERY_STALE_DAYS * 86400
        rows = self.conn.execute(
            "SELECT * FROM discovered WHERE fails < ? AND last_canada >= ?"
            " ORDER BY canada_hits DESC, first_seen ASC",
            (self.DISCOVERY_MAX_FAILS, cutoff),
        ).fetchall()
        head, tail = rows[:top], rows[top:]
        tail.sort(key=lambda r: (r["last_attempt"] or 0, -r["canada_hits"]))
        return (head + tail)[:limit]

    def discovered_ok(
        self, platform: str, token: str, jobs: int, canada: int,
        student: int = 0, strict: int = 0,
    ) -> None:
        """Record a successful scrape.

        ``ok_streak`` climbs while the board keeps returning student roles and
        falls below zero while it does not, so one empty scrape does not undo
        a promotion and two in a row do.
        """
        now = int(time.time())
        self.conn.execute(
            "UPDATE discovered SET last_ok=?, last_attempt=?, fails=0, last_error=NULL,"
            " job_count=?, student_jobs=?, strict_jobs=?,"
            " ok_streak=CASE WHEN ? > 0 THEN"
            "   CASE WHEN COALESCE(ok_streak, 0) > 0 THEN ok_streak + 1 ELSE 1 END"
            " ELSE"
            "   CASE WHEN COALESCE(ok_streak, 0) < 0 THEN ok_streak - 1 ELSE -1 END"
            " END,"
            " canada_hits=CASE WHEN ? > 0 THEN ? ELSE canada_hits END,"
            " last_canada=CASE WHEN ? > 0 THEN ? ELSE last_canada END"
            " WHERE platform=? AND token=?",
            (now, now, jobs, student, strict, student,
             canada, canada, canada, now, platform, token),
        )

    def discovered_fail(self, platform: str, token: str, error: str) -> None:
        self.conn.execute(
            "UPDATE discovered SET fails=fails+1, last_error=?, last_attempt=?"
            " WHERE platform=? AND token=?",
            (error[:500], int(time.time()), platform, token),
        )

    # -- careers-site hosts no pattern recognises -------------------------

    def hosts_add(self, host: str, sample_url: str, name: str, hits: int) -> None:
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO hosts (host, sample_url, name, hits, last_seen) VALUES (?,?,?,?,?)"
            " ON CONFLICT(host) DO UPDATE SET sample_url=excluded.sample_url,"
            " name=excluded.name, last_seen=excluded.last_seen,"
            " hits=MAX(hosts.hits, excluded.hits)",
            (host, sample_url, name, hits, now),
        )

    def hosts_due(self, limit: int, every_days: int = 14) -> list[sqlite3.Row]:
        """Recently linked hosts whose backoff has elapsed, most linked first.

        A page that names no ATS waits 30 days. One that named a platform
        waits 7. A fetch error waits ``every_days`` (14). Never sniffed is due.
        """
        now = int(time.time())
        return self.conn.execute(
            "SELECT * FROM hosts WHERE last_seen >= ? AND ("
            " last_sniffed IS NULL"
            " OR (result = 'no supported ATS' AND last_sniffed < ?)"
            " OR (result LIKE 'error:%' AND last_sniffed < ?)"
            " OR (COALESCE(result, '') != 'no supported ATS'"
            "     AND COALESCE(result, '') NOT LIKE 'error:%'"
            "     AND last_sniffed IS NOT NULL AND last_sniffed < ?)"
            ") ORDER BY hits DESC, host LIMIT ?",
            (now - self.DISCOVERY_STALE_DAYS * 86400,
             now - 30 * 86400,
             now - every_days * 86400,
             now - 7 * 86400,
             limit),
        ).fetchall()

    def hosts_sniffed(self, host: str, result: str) -> None:
        self.conn.execute(
            "UPDATE hosts SET last_sniffed=?, result=? WHERE host=?",
            (int(time.time()), result[:300], host),
        )

    def hosts_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM hosts WHERE last_sniffed IS NOT NULL ORDER BY hits DESC, host"
        ).fetchall()

    def discovered_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM discovered ORDER BY canada_hits DESC, name"
        ).fetchall()

    def discovered_summary(self) -> dict:
        """Counts for the one-line Sources summary: active boards by state."""
        cutoff = int(time.time()) - self.DISCOVERY_STALE_DAYS * 86400
        row = self.conn.execute(
            "SELECT SUM(fails = 0 AND last_ok IS NOT NULL) ok,"
            " SUM(fails > 0) failing, COUNT(*) total"
            " FROM discovered WHERE fails < ? AND last_canada >= ?",
            (self.DISCOVERY_MAX_FAILS, cutoff),
        ).fetchone()
        promoted = demoted = 0
        try:
            import companies as companies_cfg
            auto = {
                (c["platform"], str(c["token"]).rstrip("/").lower())
                for c in companies_cfg.AUTO_COMPANIES
            }
        except Exception:  # noqa: BLE001 - summary must survive a missing file
            auto = set()
        for board in self.conn.execute("SELECT platform, token, fails, ok_streak FROM discovered"):
            key = (board["platform"], str(board["token"]).rstrip("/").lower())
            if key in auto:
                promoted += 1
            elif (board["fails"] or 0) >= 3 or (board["ok_streak"] or 0) <= -2:
                demoted += 1
        return {"ok": row["ok"] or 0, "failing": row["failing"] or 0,
                "total": row["total"] or 0, "promoted": promoted, "demoted": demoted}

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
