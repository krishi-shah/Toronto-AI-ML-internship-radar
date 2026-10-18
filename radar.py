#!/usr/bin/env python3
"""Toronto AI/ML internship radar.

Fetches every configured source concurrently, classifies each posting into
an instant tier or a 5pm digest, deduplicates against SQLite, and raises
Windows notifications plus a local HTML dashboard.

    python radar.py                  normal run
    python radar.py --check          per-source ok/FAIL with counts, no alerts
    python radar.py --seed           mark everything currently live as seen
    python radar.py --digest         release the queued loose digest
    python radar.py --sniff <url>    detect ATS and print a config line
    python radar.py --probe <slug>   guess a board token on the common ATSes
    python radar.py --discovered     list boards found automatically
    python radar.py --open           rebuild and open the dashboard
    python radar.py --test           fire one fake alert
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import companies as cfg
import notify
import sources
from core import (
    AI_IN_DESCRIPTION, LOOSE, OTHER, STRICT, Posting, Store, Verdict,
    ai_description_score, classify, in_canada, names_a_place, normalize_company,
    role_keys, toronto_now,
)

# Tracker READMEs and job titles carry emoji and arrows; the Windows console
# defaults to cp1252 and would crash on the first one.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = os.environ.get("RADAR_DB", "radar.db")

# Careers pages opened in Chromium. Kept off the HTTP pool so sixteen workers
# cannot start sixteen browsers.
PORTAL_WORKERS = 2


# --------------------------------------------------------------------------
# Source planning
# --------------------------------------------------------------------------


def _with_default_location(
    thunk: Callable[[], list[Posting]], location: str
) -> Callable[[], list[Posting]]:
    """Fill ``location`` into postings that come back without a readable one.

    The classifier rejects anything with no Canadian location, and the HTML
    link-diff layer never carries one, so a Toronto-only employer's careers
    page needs its location stated in config. Browser rows carry the link
    text as their location; when that names no place at all the default is
    used too, but a link that names a foreign place is left alone.
    """

    def run() -> list[Posting]:
        found = thunk()
        for post in found:
            if not names_a_place(post.location):
                post.location = location
        return found

    run.browser = getattr(thunk, "browser", False)
    return run


def build_tasks(http: sources.Http) -> list[tuple[str, Callable[[], list[Posting]], bool]]:
    """Return ``(source_name, thunk, ai_native)`` for every configured source."""
    tasks: list[tuple[str, Callable[[], list[Posting]], bool]] = []
    labels: set[str] = set()

    for entry in cfg.all_companies():
        name = entry["name"]
        platform = entry["platform"]
        token = entry["token"]
        ai_native = bool(entry.get("ai_native"))
        label = f"{name} [{platform}]"
        # One company can run several boards (RBC has two Workday sites), and
        # the health table is keyed by label, so repeats get the board's slug.
        if label in labels:
            slug = token.rstrip("/").removesuffix("/jobs").rsplit("/", 1)[-1]
            label = f"{name} [{platform}:{slug}]"
        labels.add(label)

        if platform == "html":
            browser = bool(entry.get("browser"))
            thunk = lambda n=name, t=token, b=browser: sources.html_links(  # noqa: E731
                http, n, t, browser=b
            )
            thunk.browser = browser
        elif platform in sources.ADAPTERS:
            thunk = lambda a=sources.ADAPTERS[platform], n=name, t=token: a(http, n, t)  # noqa: E731
        else:
            thunk = lambda p=platform: (_ for _ in ()).throw(  # noqa: E731
                ValueError(f"unknown platform '{p}'")
            )

        if entry.get("location"):
            thunk = _with_default_location(thunk, entry["location"])
        tasks.append((label, thunk, ai_native))

    for entry in cfg.TRACKERS:
        label = f"{entry['name']} [tracker]"
        tasks.append(
            (
                label,
                lambda n=entry["name"], r=entry["repo"]: sources.tracker(http, n, r),
                False,
            )
        )

    # VC and ecosystem job boards list many companies' roles at once, so each
    # posting carries its own company name rather than the board's.
    for entry in cfg.PORTFOLIO_BOARDS:
        adapter = sources.ADAPTERS.get(entry["platform"])
        label = f"{entry['name']} [{entry['platform']}]"
        if adapter is None:
            thunk = lambda p=entry["platform"]: (_ for _ in ()).throw(  # noqa: E731
                ValueError(f"unknown platform '{p}'")
            )
        else:
            thunk = lambda a=adapter, n=entry["name"], t=entry["token"]: a(http, n, t)  # noqa: E731
        tasks.append((label, thunk, False))

    return tasks


def ai_native_companies() -> set[str]:
    """Normalised names of every company configured ``ai_native``."""
    return {
        normalize_company(c["name"]) for c in cfg.all_companies() if c.get("ai_native")
    }


def source_count() -> int:
    """Configured sources: company boards, trackers and portfolio job boards."""
    return len(cfg.all_companies()) + len(cfg.TRACKERS) + len(cfg.PORTFOLIO_BOARDS)


def fetch_all(
    http: sources.Http, store: Store, quiet: bool = False, track: bool = True,
) -> tuple[list[Posting], dict[str, str]]:
    """Fetch every source concurrently. Returns postings and per-source errors.

    Configured sources run first. Their Canadian postings are then mined for
    boards nobody configured, and those run as a second wave. An adapter
    raising is recorded against its source and the run continues -- one dead
    board must never fail the run.

    Only configured sources are reported in ``errors`` and the health table.
    Discovered boards are speculative, so their failures are tracked in the
    ``discovered`` table instead and never trip the health warning.

    With ``track``, every complete listing also updates which stored postings
    its source still lists (see Store.mark_present). ``--check`` passes False
    because it records nothing.
    """
    deadline = time.monotonic() + cfg.DISCOVERY_TIME_BUDGET_S
    stamp = int(time.time()) if track else None
    tasks = build_tasks(http)
    http_tasks = [task for task in tasks if not getattr(task[1], "browser", False)]
    portal_tasks = [task for task in tasks if getattr(task[1], "browser", False)]
    postings, errors = _run_wave(http_tasks, store, quiet, stamp=stamp)
    if portal_tasks:
        found, portal_errors = _run_wave(
            portal_tasks, store, quiet, workers=PORTAL_WORKERS, stamp=stamp,
        )
        postings.extend(found)
        errors.update(portal_errors)
    store.commit()

    if cfg.DISCOVERY_MAX_BOARDS > 0:
        found = _run_discovered(http, store, postings, quiet, deadline, stamp)
        postings.extend(found)
        store.commit()

    if time.monotonic() < deadline and errors:
        retry = [
            (label, _before(deadline, thunk), ai)
            for label, thunk, ai in tasks
            if label in errors
        ]
        if retry and not quiet:
            print(
                f"\n  -- retrying {len(retry)} failed sources, "
                f"{max(0, int(deadline - time.monotonic()))}s left --"
            )
        retry_http = [task for task in retry if not getattr(task[1], "browser", False)]
        retry_portal = [task for task in retry if getattr(task[1], "browser", False)]
        more, still_bad = _run_wave(retry_http, store, quiet, stamp=stamp)
        if retry_portal:
            more_portal, still_portal = _run_wave(
                retry_portal, store, quiet, workers=PORTAL_WORKERS, stamp=stamp,
            )
            more.extend(more_portal)
            still_bad.update(still_portal)
        postings.extend(more)
        healthy = {row["source"] for row in store.health_rows() if row["ok"]}
        errors = {label: err for label, err in errors.items() if label not in healthy}
        errors.update(still_bad)
        store.commit()

    if (time.monotonic() < deadline and cfg.SNIFF_PER_RUN > 0
            and cfg.DISCOVERY_MAX_BOARDS > 0):
        if not quiet:
            print("\n  -- leftover time: sniffing more careers hosts --")
        _sniff_hosts(http, store, postings, quiet)
        store.commit()
    return postings, errors


class OutOfTime(Exception):
    """A discovered board reached after the time budget ran out. Not a failure."""


def _before(deadline: float, thunk: Callable[[], list[Posting]]) -> Callable[[], list[Posting]]:
    """Run ``thunk`` only if the pool reaches it before ``deadline``."""
    def run() -> list[Posting]:
        if time.monotonic() > deadline:
            raise OutOfTime()
        return thunk()

    run.browser = getattr(thunk, "browser", False)
    return run


def _run_wave(
    tasks, store: Store, quiet: bool, on_result=None, workers: int | None = None,
    stamp: Optional[int] = None,
):
    """Run ``(label, thunk, ai_native)`` tasks on the pool, in order.

    ``on_result(label, found_or_None, error_or_None)`` replaces the default
    health-table bookkeeping when given. A task raising :class:`OutOfTime`
    is reported to neither: it simply did not run.

    With ``stamp``, each complete listing marks which stored postings its
    source still lists. A capped search (:class:`sources.Partial`) or an empty
    answer -- a 304, or a board briefly returning nothing -- marks nothing.
    """
    postings: list[Posting] = []
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=workers or cfg.MAX_WORKERS) as pool:
        futures = {
            pool.submit(thunk): (label, ai_native) for label, thunk, ai_native in tasks
        }
        for future in as_completed(futures):
            label, ai_native = futures[future]
            try:
                found = future.result()
            except OutOfTime:
                continue
            except Exception as exc:  # noqa: BLE001 - every failure is per-source
                msg = f"{type(exc).__name__}: {exc}"
                if on_result:
                    on_result(label, None, msg)
                else:
                    errors[label] = msg
                    store.health_fail(label, msg)
                if not quiet:
                    print(f"  FAIL  {label:<48} {msg[:90]}")
                continue

            native = ai_native_companies()
            for post in found:
                post.source = post.source or label
                # A tracker or portfolio-board row for Cohere is as AI as one
                # from Cohere's own board.
                post.ai_native = (post.ai_native or ai_native
                                  or normalize_company(post.company) in native)
            postings.extend(found)
            if stamp is not None and found and not isinstance(found, sources.Partial):
                by_source: dict[str, list[str]] = {}
                for post in found:
                    by_source.setdefault(post.source, []).append(post.uid)
                for source, uids in by_source.items():
                    store.mark_present(source, uids, stamp)
            if on_result:
                on_result(label, found, None)
            else:
                store.health_ok(label, len(found))
            if not quiet:
                print(f"  ok    {label:<48} {len(found):>5} postings")

    return postings, errors


def configured_boards() -> set[tuple[str, str]]:
    return {
        sources.board_key(c["platform"], c["token"])
        for c in cfg.all_companies()
        if c["platform"] in sources.ADAPTERS
    }


def configured_hosts() -> set[str]:
    """Hosts already scraped through a URL-shaped token, never worth sniffing."""
    hosts = set()
    for c in cfg.all_companies():
        token = c["token"].partition("|")[0]
        if token.startswith("http"):
            hosts.add(sources._bare_host(token))
    return hosts


def _sniff_hosts(http: sources.Http, store: Store, postings: list[Posting], quiet: bool) -> int:
    """Sniff a few unrecognised careers hosts; any supported board joins discovery.

    Returns how many new boards were found. Each host is sniffed at most once
    per ``SNIFF_EVERY_DAYS``, whatever the outcome, so a dead end costs one
    request a fortnight.
    """
    for entry in sources.unplaced_hosts(postings, configured_hosts()):
        store.hosts_add(entry["host"], entry["url"], entry["name"], entry["hits"])
    store.commit()

    due = store.hosts_due(cfg.SNIFF_PER_RUN, cfg.SNIFF_EVERY_DAYS)
    if not due:
        return 0
    known = configured_boards()

    def sniff(row: sqlite3.Row) -> list[tuple[str, str]]:
        return sources.sniff_boards(http, row["sample_url"])

    found = 0
    with ThreadPoolExecutor(max_workers=min(len(due), cfg.MAX_WORKERS)) as pool:
        futures = {pool.submit(sniff, row): row for row in due}
        for future in as_completed(futures):
            row = futures[future]
            try:
                boards = [b for b in future.result() if sources.board_key(*b) not in known]
            except Exception as exc:  # noqa: BLE001 - a dead host is the common case
                store.hosts_sniffed(row["host"], f"error: {type(exc).__name__}")
                continue
            for platform, token in boards:
                store.discovered_add(platform, token, row["name"], row["hits"])
            found += len(boards)
            store.hosts_sniffed(
                row["host"], "; ".join(f"{p} {t}" for p, t in boards) or "no supported ATS")
            if not quiet and boards:
                print(f"  sniffed {row['host']}: {', '.join(p for p, _ in boards)}")
    store.commit()
    return found


def _run_discovered(
    http: sources.Http, store: Store, postings: list[Posting], quiet: bool,
    deadline: float, stamp: Optional[int] = None,
) -> list[Posting]:
    """Mine ``postings`` for new boards, then scrape known ones until ``deadline``."""
    for board in sources.discover_boards(postings, configured_boards()):
        store.discovered_add(board["platform"], board["token"], board["name"], board["hits"])
    store.commit()
    if cfg.SNIFF_PER_RUN > 0:
        _sniff_hosts(http, store, postings, quiet)

    picked = store.discovered_pick(cfg.DISCOVERY_MAX_BOARDS, cfg.DISCOVERY_TOP_BOARDS)
    if not picked:
        return []
    if not quiet:
        print(f"\n  -- up to {len(picked)} discovered boards, "
              f"{max(0, int(deadline - time.monotonic()))}s left in the budget --")

    by_label: dict[str, sqlite3.Row] = {}
    tasks = []
    for row in picked:
        label = f"{row['name']} [{row['platform']}, discovered]"
        if label in by_label:
            label = f"{row['name']} [{row['platform']}:{row['token'][-24:]}, discovered]"
        by_label[label] = row
        adapter = sources.ADAPTERS.get(row["platform"])
        if adapter is None:
            continue
        thunk = lambda a=adapter, n=row["name"], t=row["token"]: a(http, n, t)  # noqa: E731
        tasks.append((label, _before(deadline, thunk), False))

    ran = 0

    def record(label: str, found: Optional[list[Posting]], error: Optional[str]) -> None:
        nonlocal ran
        ran += 1
        row = by_label[label]
        if error is not None:
            store.discovered_fail(row["platform"], row["token"], error)
            return
        canada = sum(1 for p in found if in_canada(p))
        student = strict_n = 0
        for post in found:
            verdict = classify(post)
            if verdict.tier in (STRICT, LOOSE, OTHER):
                student += 1
            if verdict.tier == STRICT:
                strict_n += 1
        store.discovered_ok(
            row["platform"], row["token"], len(found), canada,
            student=student, strict=strict_n,
        )

    found, _ = _run_wave(tasks, store, quiet, on_result=record, stamp=stamp)
    if not quiet and ran < len(tasks):
        print(f"  -- time budget reached: {len(tasks) - ran} boards wait for the next run --")
    return found


# --------------------------------------------------------------------------
# Job descriptions
# --------------------------------------------------------------------------


def _wants_description(verdict: Verdict) -> bool:
    """Could reading the posting change this verdict?

    A tech role in the commute area that missed strict only for want of an AI
    word in its title, or a careers-page link the classifier could not place
    because the link text named no city.
    """
    if verdict.tier == LOOSE and verdict.reason == "no AI signal":
        return True
    return verdict.tier is None and verdict.student and verdict.reason == "no Canadian location"


class Describer:
    """Reads job descriptions within a per-run request and time budget.

    Each posting is read at most once ever (the ``descriptions`` table), so
    the budget goes to new postings first and the backlog drains over runs.
    With ``record`` False (``--check``) nothing is written.
    """

    def __init__(
        self, http: sources.Http, store: Store, limit: int, budget_s: float,
        record: bool = True,
    ) -> None:
        self.http = http
        self.store = store
        self.record = record
        self.left = max(0, limit)
        self.deadline = time.monotonic() + budget_s
        self.read = 0
        self.upgraded = 0

    def _budget_left(self) -> bool:
        return self.left > 0 and time.monotonic() < self.deadline

    def _read(self, post: Posting) -> Optional[sources.Description]:
        """Fetch one description and remember that it was read."""
        self.left -= 1
        self.read += 1
        try:
            desc = sources.describe(self.http, post.url, post.raw)
        except Exception as exc:  # noqa: BLE001 - one dead page is not a failed run
            if self.record:
                self.store.record_description(post.uid, 0, [], f"{type(exc).__name__}: {exc}")
            return None
        if self.record:
            score, terms = ai_description_score(desc.text if desc else "")
            self.store.record_description(post.uid, score, terms)
        return desc

    def reclassify(self, post: Posting, verdict: Verdict) -> Verdict:
        """``verdict``, revised by what the posting's own page says."""
        if not self._budget_left():
            return verdict
        if self.record and self.store.description_checked(post.uid):
            return verdict
        desc = self._read(post)
        if desc is None:
            return verdict
        # Careers-page links carry no place of their own; the posting page does.
        if desc.location and not names_a_place(post.location):
            post.location = desc.location
        if desc.posted_at and not post.posted_at:
            post.posted_at = desc.posted_at
        revised = classify(post, desc.text)
        if revised.tier == STRICT and verdict.tier != STRICT:
            self.upgraded += 1
        return revised

    def backfill(self, max_age_hours: int) -> int:
        """Read stored loose rows from earlier runs; return how many turned strict."""
        before = self.upgraded
        for row in self.store.description_backlog(self.left, max_age_hours):
            if not self._budget_left():
                break
            post = Posting(
                company=row["company"], title=row["title"], location=row["location"] or "",
                url=row["url"] or "", uid=row["uid"], source=row["source"] or "",
                posted_at=row["posted_at"] or 0,
            )
            revised = self.reclassify(post, Verdict(LOOSE, row["reason"]))
            if revised.tier != LOOSE or revised.reason != row["reason"]:
                self.store.update_verdict(post.uid, revised.tier, revised.reason, revised.ai_terms)
            if post.location != (row["location"] or ""):
                self.store.update_place(post.uid, post.location, post.posted_at)
        self.store.commit()
        return self.upgraded - before


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def triage(
    postings: list[Posting], store: Store, describer: Optional["Describer"] = None,
) -> tuple[list[tuple[Posting, Verdict]], list[tuple[Posting, Verdict]], int]:
    """Classify and dedupe. Returns new strict hits, new loose hits, skip count.

    Two dedupe layers: the per-source uid catches the same posting on a later
    run, and the role group -- company+title fingerprint or the requisition id
    in the URL -- catches the same role arriving from an ATS and a tracker.

    ``describer`` (see :class:`Describer`) reads the description of a role
    that missed strict only for want of an AI word in its title.
    """
    new_strict: list[tuple[Posting, Verdict]] = []
    new_loose: list[tuple[Posting, Verdict]] = []
    skipped = 0
    # A strict hit must never be suppressed by a role only ever seen at the
    # loose tier (see Store.delivered), and strict rows from this run are not
    # marked notified yet, so this run's strict groups are tracked here.
    strict_groups: set[str] = set()

    for post in postings:
        if not post.uid or not post.title:
            continue
        if store.seen_uid(post.uid):
            skipped += 1
            continue

        verdict = classify(post)
        if describer is not None and _wants_description(verdict):
            verdict = describer.reclassify(post, verdict)

        if verdict.tier is None:
            store.record(post, None, verdict.reason)
            continue

        # Same role already delivered under another uid: keep the row so uid
        # dedupe still works, but do not notify a second time.
        group = store.group_for(post)
        duplicate = store.delivered(post, verdict.tier) or (
            verdict.tier == STRICT and group is not None and group in strict_groups
        )

        if duplicate:
            store.record(post, verdict.tier, verdict.reason, notified=True,
                         digested=True, ai_terms=verdict.ai_terms)
            skipped += 1
            continue

        # Other-fields rows are only ever shown in the feed, never alerted on
        # or digested, so they are recorded and go no further.
        store.record(post, verdict.tier, verdict.reason, ai_terms=verdict.ai_terms)
        if verdict.tier == STRICT:
            strict_groups.add(store.group_for(post))
        if verdict.tier == STRICT:
            new_strict.append((post, verdict))
        elif verdict.tier == LOOSE:
            new_loose.append((post, verdict))

    store.commit()
    return new_strict, new_loose, skipped


# --------------------------------------------------------------------------
# Alert delivery
# --------------------------------------------------------------------------


def is_fresh(posted_at: int) -> bool:
    """Is this posting recent enough to be worth racing for?

    An unknown date counts as fresh: several sources expose no publish time at
    all, and silently dropping everything they return would be the expensive
    kind of mistake.
    """
    if not posted_at:
        return True
    return posted_at >= time.time() - cfg.MAX_AGE_HOURS * 3600


def send_digest(store: Store, notifiers) -> int:
    """Hand every queued loose posting to the configured channels."""
    rows = store.pending_digest()
    if not rows:
        print("Digest: nothing queued.")
        notify.dispatch(notifiers, "digest", [])
        return 0

    if notify.dispatch(notifiers, "digest", rows):
        store.mark_digested([r["uid"] for r in rows])
        store.commit()
    print(f"Digest: {len(rows)} postings.")
    return len(rows)


def health_warning(errors: dict, total_sources: int, notifiers) -> None:
    """Warn when enough sources are down that silence stops meaning 'no jobs'."""
    if not total_sources:
        return
    ratio = len(errors) / total_sources
    if ratio <= cfg.HEALTH_FAIL_THRESHOLD:
        return
    worst = ", ".join(list(errors)[:4])
    notify.dispatch(
        notifiers,
        "health",
        f"{len(errors)}/{total_sources} sources failing ({ratio:.0%}): {worst}",
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


PROMOTE_DENY = (
    "prolific", "welo", "general dynamics", "invisible", "internshiplist",
)


def _denied_board(name: str, token: str) -> bool:
    blob = f"{name} {token}".lower()
    return any(word in blob for word in PROMOTE_DENY)


def _curated_keys() -> set[tuple[str, str]]:
    return {
        sources.board_key(c["platform"], c["token"])
        for c in cfg.COMPANIES
        if c.get("platform") and c.get("token")
    }


def _should_demote(row: sqlite3.Row) -> bool:
    return (row["fails"] or 0) >= 3 or (row["ok_streak"] or 0) <= -2


def _promotable(row: sqlite3.Row, curated: set[tuple[str, str]]) -> bool:
    if row["platform"] not in sources.ADAPTERS or (row["fails"] or 0) != 0:
        return False
    key = sources.board_key(row["platform"], row["token"])
    if key in curated or _denied_board(row["name"], row["token"]):
        return False
    strict = row["strict_jobs"] or 0
    student = row["student_jobs"] or 0
    streak = row["ok_streak"] or 0
    return strict >= 1 or (student >= 2 and streak >= 1)


def _rank_row(row: Optional[sqlite3.Row]) -> tuple:
    if row is None:
        return (0, 0, 0, "", "", "")
    return (
        -(row["strict_jobs"] or 0),
        -(row["student_jobs"] or 0),
        -(row["canada_hits"] or 0),
        (row["name"] or "").lower(),
        row["platform"],
        row["token"].lower(),
    )


def select_auto_companies(store: Store) -> tuple[list[dict], int]:
    """Boards to write into auto_companies.py, and how many were dropped.

    A board stays once promoted until it fails three scrapes or returns no
    student roles twice in a row. New boards need a strict hit, or at least
    two student roles on a successful scrape.
    """
    curated = _curated_keys()
    by_key = {
        sources.board_key(row["platform"], row["token"]): row
        for row in store.discovered_rows()
    }
    demoted = 0
    kept: list[dict] = []
    kept_keys: set[tuple[str, str]] = set()
    for entry in cfg.AUTO_COMPANIES:
        key = sources.board_key(entry["platform"], entry["token"])
        row = by_key.get(key)
        if key in curated or _denied_board(entry.get("name", ""), entry.get("token", "")):
            demoted += 1
            continue
        if row is not None and _should_demote(row):
            demoted += 1
            continue
        kept.append({
            "name": row["name"] if row is not None else entry["name"],
            "platform": entry["platform"],
            "token": entry["token"],
            "ai_native": False,
        })
        kept_keys.add(key)

    newcomers = [
        row for row in store.discovered_rows()
        if sources.board_key(row["platform"], row["token"]) not in kept_keys
        and _promotable(row, curated)
    ]
    scored: list[tuple[tuple, dict]] = []
    for entry in kept:
        row = by_key.get(sources.board_key(entry["platform"], entry["token"]))
        scored.append((_rank_row(row), entry))
    for row in newcomers:
        scored.append((_rank_row(row), {
            "name": row["name"],
            "platform": row["platform"],
            "token": row["token"],
            "ai_native": False,
        }))
    scored.sort(key=lambda item: item[0])
    return [entry for _, entry in scored[: cfg.PROMOTE_MAX]], demoted


def render_auto_companies(rows: list[dict]) -> str:
    ordered = sorted(
        rows, key=lambda row: (row["name"].lower(), row["platform"], row["token"].lower())
    )
    lines = [
        "# AUTO-GENERATED by radar.py from discovered evidence. Do not edit.",
        '"""Boards the radar promoted because they returned Canadian student roles.',
        "",
        "Curated boards stay in companies.py. This file is rewritten at the end of a",
        'normal run or ``--seed``, never by ``--check``.',
        '"""',
        "",
        "AUTO_COMPANIES: list[dict] = [",
    ]
    for row in ordered:
        payload = {
            "name": row["name"],
            "platform": row["platform"],
            "token": row["token"],
            "ai_native": False,
        }
        lines.append(f"    {payload!r},")
    lines.append("]")
    lines.append("")
    return "\n".join(lines)


def write_auto_companies(path: str, rows: list[dict]) -> bool:
    """Write ``rows`` if they differ from ``path``. Returns whether it changed.

    Refuses to replace the file unless the text is valid Python. A JSON
    ``false`` here used to crash the next import and skip the README commit.
    """
    text = render_auto_companies(rows)
    compile(text, path, "exec")
    try:
        with open(path, encoding="utf-8") as fh:
            current = fh.read()
    except OSError:
        current = None
    if current == text:
        return False
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(temporary, path)
    return True


def auto_companies_path() -> str:
    return os.environ.get(
        "RADAR_AUTO_COMPANIES",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_companies.py"),
    )


def sync_auto_companies(store: Store) -> bool:
    """Rewrite the generated source list from this run's evidence.

    Promotions are read on the next process start. This run already scraped
    those boards as discovered, so a bad write must not stop the feed.
    """
    rows, demoted = select_auto_companies(store)
    try:
        changed = write_auto_companies(auto_companies_path(), rows)
    except SyntaxError as exc:
        print(f"auto_companies.py: refused to write invalid Python: {exc}")
        return False
    if changed:
        print(f"auto_companies.py: {len(rows)} promoted, {demoted} demoted.")
    return changed


def cmd_run(store: Store, seed: bool = False) -> int:
    """Normal run: fetch, triage, alert. With ``seed``, record but never alert.

    A seed must see every posting that is live right now, so it bypasses the
    conditional-request cache entirely -- a 304 left over from an earlier
    ``--check`` would otherwise seed nothing and let the next run flood.
    """
    http = sources.Http(None if seed else store)
    started = time.time()
    mode = "SEED" if seed else "RUN"
    print(f"[{mode}] {toronto_now():%Y-%m-%d %H:%M:%S} Toronto")

    postings, errors = fetch_all(http, store)
    sync_auto_companies(store)
    total_sources = source_count()
    describer = Describer(http, store, cfg.DESC_PER_RUN, cfg.DESC_TIME_BUDGET_S)

    if seed:
        recorded = 0
        for post in postings:
            if post.uid and post.title and not store.seen_uid(post.uid):
                verdict = classify(post)
                if _wants_description(verdict):
                    verdict = describer.reclassify(post, verdict)
                store.record(
                    post, verdict.tier, verdict.reason,
                    notified=True, digested=True, seeded=True, ai_terms=verdict.ai_terms,
                )
                recorded += 1
        store.commit()
        print(f"\nSeeded {recorded} postings as already-seen. No alerts sent.")
        print(f"Descriptions: {describer.read} read, {describer.upgraded} promoted to strict.")
        return 0

    new_strict, new_loose, skipped = triage(postings, store, describer)
    backfilled = describer.backfill(cfg.MAX_AGE_HOURS)
    print(
        f"\n{len(postings)} fetched · {skipped} already seen · "
        f"{len(new_strict)} strict · {len(new_loose)} queued for digest"
    )
    print(
        f"Descriptions: {describer.read} read, {describer.upgraded} promoted to strict"
        f" ({backfilled} from earlier runs)."
    )

    notifiers = notify.build_notifiers(cfg.NOTIFIERS, store)

    # A role that went live days ago already has hundreds of applicants, so it
    # is recorded (dedupe still needs it) but never alerted on.
    fresh = [(p, v) for p, v in new_strict if is_fresh(p.posted_at)]
    stale = len(new_strict) - len(fresh)
    if stale:
        print(f"  ({stale} strict hits older than {cfg.MAX_AGE_HOURS}h, not alerted)")

    if fresh and notify.dispatch(notifiers, "strict", fresh):
        for post, _ in fresh:
            store.mark_notified(post.uid)
            print(f"  ALERT  {post.company} — {post.title}")
        store.commit()
    else:
        # Quiet run: still refresh the dashboard, so its timestamp shows the
        # radar is alive rather than merely having found nothing.
        notify.dispatch(notifiers, "strict", [])

    health_warning(errors, total_sources, notifiers)
    print(f"Done in {time.time() - started:.1f}s")
    return 0


def cmd_check(store: Store) -> int:
    """Per-source ok/FAIL with counts and a classification preview. No alerts.

    Records nothing, so it must not write etags either: a cached 304 would
    make the next seed or run skip postings it never stored.
    """
    http = sources.Http(None)
    print(f"[CHECK] {toronto_now():%Y-%m-%d %H:%M:%S} Toronto\n")

    postings, errors = fetch_all(http, store, track=False)
    total_sources = source_count()

    # Apply the same role grouping a real run would -- fingerprint or
    # requisition id -- so the counts here reflect what would actually ping
    # rather than raw source rows. RBC alone publishes the same co-op through
    # two Workday sites, and trackers retitle it again.
    describer = Describer(http, store, cfg.DESC_PER_RUN, cfg.DESC_TIME_BUDGET_S,
                          record=False)
    strict: list[tuple[Posting, Verdict]] = []
    seen_keys: set[str] = set()
    dupes = 0
    loose = 0
    other = 0
    stale = 0
    for post in postings:
        verdict = classify(post)
        if _wants_description(verdict) and is_fresh(post.posted_at):
            verdict = describer.reclassify(post, verdict)
        if verdict.tier == STRICT:
            keys = role_keys(post.company, post.title, post.url)
            if seen_keys.intersection(keys):
                dupes += 1
                seen_keys.update(keys)
                continue
            seen_keys.update(keys)
            if is_fresh(post.posted_at):
                strict.append((post, verdict))
            else:
                stale += 1
        elif verdict.tier == LOOSE:
            loose += 1
        elif verdict.tier == OTHER:
            other += 1

    notes = []
    if describer.read:
        notes.append(f"{describer.read} descriptions read, "
                     f"{describer.upgraded} promoted to strict")
    if dupes:
        notes.append(f"{dupes} duplicates collapsed")
    if stale:
        notes.append(f"{stale} older than {cfg.MAX_AGE_HOURS}h")
    print(
        f"\n{total_sources - len(errors)}/{total_sources} sources ok · "
        f"{len(postings)} postings · {len(strict)} strict · {loose} loose"
        f" · {other} other fields"
        + (f"  ({', '.join(notes)})" if notes else "")
    )

    if strict:
        print(f"\nStrict matches posted in the last {cfg.MAX_AGE_HOURS}h:")
        for post, verdict in strict[:40]:
            print(f"  · {post.company} — {post.title}")
            if verdict.reason == AI_IN_DESCRIPTION:
                print(f"      {AI_IN_DESCRIPTION}: {', '.join(verdict.ai_terms)}")
            print(f"      {post.location or '(no location listed)'}")
            print(f"      {post.url}")
    if errors:
        print("\nFailing sources:")
        for name, err in errors.items():
            print(f"  FAIL  {name}: {err[:140]}")
    return 1 if len(errors) / max(total_sources, 1) > cfg.HEALTH_FAIL_THRESHOLD else 0


def cmd_sniff(url: str) -> int:
    http = sources.Http()
    print(f"Sniffing {url}\n")
    for line in sources.sniff(http, url):
        print(line)
    return 0


def cmd_discovered(store: Store) -> int:
    """List discovered boards, so good ones can be promoted into companies.py."""
    rows = store.discovered_rows()
    if not rows:
        print("No discovered boards yet. They appear after a run or --check.")
        return 0
    auto = {
        sources.board_key(entry["platform"], entry["token"])
        for entry in cfg.AUTO_COMPANIES
    }
    print(f"{'board':<44} {'canada':>6} {'jobs':>5} {'fails':>5}  status")
    for row in rows:
        key = sources.board_key(row["platform"], row["token"])
        if key in auto:
            status = "promoted"
        elif _should_demote(row):
            status = "demoted"
        else:
            status = "waiting"
        label = f"{row['name']} [{row['platform']}]"
        print(f"{label[:44]:<44} {row['canada_hits']:>6} {row['job_count']:>5}"
              f" {row['fails']:>5}  {status}")
        if status != "promoted":
            print(f"    {row['token']}")
        if row["fails"] and row["last_error"]:
            print(f"    {row['last_error'][:140]}")

    hosts = store.hosts_rows()
    if hosts:
        print(f"\n{'sniffed careers host':<44} {'links':>6}  result")
        for row in hosts:
            print(f"{row['host'][:44]:<44} {row['hits']:>6}  {row['result']}")
    return 0


def cmd_probe(slugs: list[str]) -> int:
    http = sources.Http()
    for slug in slugs:
        print(f"Probing '{slug}'")
        for line in sources.probe(http, slug):
            print(line)
        print()
    return 0


def cmd_test(store: Store) -> int:
    """Fire one fake alert through every configured channel."""
    sample = Posting(
        company="Cohere",
        title="Machine Learning Intern, Winter 2027",
        location="Toronto, Ontario, Canada",
        url="https://jobs.ashbyhq.com/cohere",
        uid="test",
        source="radar --test",
    )
    notifiers = notify.build_notifiers(cfg.NOTIFIERS, store)
    ok = notify.dispatch(notifiers, "strict", [(sample, classify(sample))])
    channels = ", ".join(n.name for n in notifiers) or "none"
    # Only the toast channel shows the sample itself; the dashboard and README
    # render from the database, so for them this just proves they still work.
    print(
        f"Test alert sent through: {channels}."
        if ok
        else f"Test alert FAILED through: {channels}. See above."
    )
    return 0 if ok else 1


def cmd_open(store: Store) -> int:
    """Rebuild the dashboard from the database and open it in a browser."""
    dash = notify.DashboardWriter(store)
    if not dash.render():
        return 1
    path = os.path.abspath(dash.path)
    print(f"Dashboard: {path}")
    webbrowser.open(f"file:///{path}".replace("\\", "/"))
    return 0


def cmd_health(store: Store) -> int:
    print(f"{'source':<52} {'':<6} {'last n':>6}  last success")
    for row in store.health_rows():
        stamp = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row["last_success"]))
            if row["last_success"]
            else "never"
        )
        flag = "FAIL" if not row["ok"] else "empty" if not row["job_count"] else "ok"
        print(f"{row['source']:<52} {flag:<6} {row['job_count']:>6}  {stamp}")
        if not row["ok"] and row["last_error"]:
            print(f"    {row['last_error'][:150]}")
    print(f"\nstored postings by tier: {store.counts()}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Toronto AI/ML internship radar",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--check", action="store_true", help="per-source status, no alerts")
    parser.add_argument("--seed", action="store_true", help="mark everything live as seen")
    parser.add_argument("--digest", action="store_true", help="send the queued loose digest")
    parser.add_argument("--sniff", metavar="URL", help="detect ATS for a careers page")
    parser.add_argument("--probe", metavar="SLUG", nargs="+",
                        help="guess a board token on Ashby/Greenhouse/Lever/...")
    parser.add_argument("--test", action="store_true", help="fire one fake alert")
    parser.add_argument("--health", action="store_true", help="print the health table")
    parser.add_argument("--discovered", action="store_true",
                        help="list automatically discovered boards")
    parser.add_argument("--open", action="store_true", help="rebuild and open the dashboard")
    parser.add_argument("--db", default=DB_PATH, help=f"SQLite path (default {DB_PATH})")
    args = parser.parse_args(argv)

    if args.sniff:
        return cmd_sniff(args.sniff)
    if args.probe:
        return cmd_probe(args.probe)

    store = Store(args.db)
    try:
        if args.test:
            return cmd_test(store)
        if args.open:
            return cmd_open(store)
        if args.check:
            return cmd_check(store)
        if args.health:
            return cmd_health(store)
        if args.discovered:
            return cmd_discovered(store)
        if args.digest:
            send_digest(store, notify.build_notifiers(cfg.NOTIFIERS, store))
            return 0
        return cmd_run(store, seed=args.seed)
    except Exception:  # noqa: BLE001 - surface the traceback, keep the exit code sane
        traceback.print_exc()
        return 1
    finally:
        store.commit()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
