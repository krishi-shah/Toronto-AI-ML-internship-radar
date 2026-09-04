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
    python radar.py --open           rebuild and open the dashboard
    python radar.py --test           fire one fake alert
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import companies as cfg
import notify
import sources
from core import LOOSE, STRICT, Posting, Store, Verdict, classify, toronto_now

# Tracker READMEs and job titles carry emoji and arrows; the Windows console
# defaults to cp1252 and would crash on the first one.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = os.environ.get("RADAR_DB", "radar.db")


# --------------------------------------------------------------------------
# Source planning
# --------------------------------------------------------------------------


def build_tasks(http: sources.Http) -> list[tuple[str, Callable[[], list[Posting]], bool]]:
    """Return ``(source_name, thunk, ai_native)`` for every configured source."""
    tasks: list[tuple[str, Callable[[], list[Posting]], bool]] = []

    for entry in cfg.COMPANIES:
        name = entry["name"]
        platform = entry["platform"]
        token = entry["token"]
        ai_native = bool(entry.get("ai_native"))
        label = f"{name} [{platform}]"

        if platform == "html":
            tasks.append(
                (label, lambda n=name, t=token: sources.html_links(http, n, t), ai_native)
            )
            continue

        adapter = sources.ADAPTERS.get(platform)
        if adapter is None:
            tasks.append(
                (
                    label,
                    lambda p=platform: (_ for _ in ()).throw(
                        ValueError(f"unknown platform '{p}'")
                    ),
                    ai_native,
                )
            )
            continue

        tasks.append(
            (label, lambda a=adapter, n=name, t=token: a(http, n, t), ai_native)
        )

    for entry in cfg.TRACKERS:
        label = f"{entry['name']} [tracker]"
        tasks.append(
            (
                label,
                lambda n=entry["name"], r=entry["repo"]: sources.tracker(http, n, r),
                False,
            )
        )

    return tasks


def fetch_all(
    http: sources.Http, store: Store, quiet: bool = False
) -> tuple[list[Posting], dict[str, str]]:
    """Fetch every source concurrently. Returns postings and per-source errors.

    An adapter raising is recorded against its source in the health table and
    the run continues -- one dead board must never fail the run.
    """
    tasks = build_tasks(http)
    postings: list[Posting] = []
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=cfg.MAX_WORKERS) as pool:
        futures = {
            pool.submit(thunk): (label, ai_native) for label, thunk, ai_native in tasks
        }
        for future in as_completed(futures):
            label, ai_native = futures[future]
            try:
                found = future.result()
            except Exception as exc:  # noqa: BLE001 - every failure is per-source
                msg = f"{type(exc).__name__}: {exc}"
                errors[label] = msg
                store.health_fail(label, msg)
                if not quiet:
                    print(f"  FAIL  {label:<48} {msg[:90]}")
                continue

            for post in found:
                post.source = post.source or label
                post.ai_native = post.ai_native or ai_native
            postings.extend(found)
            store.health_ok(label, len(found))
            if not quiet:
                print(f"  ok    {label:<48} {len(found):>5} postings")

    store.commit()
    return postings, errors


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def triage(
    postings: list[Posting], store: Store
) -> tuple[list[tuple[Posting, Verdict]], list[tuple[Posting, Verdict]], int]:
    """Classify and dedupe. Returns new strict hits, new loose hits, skip count.

    Two dedupe layers: the per-source uid catches the same posting on a later
    run, and the company+title fingerprint catches the same role arriving from
    an ATS and a tracker at once.
    """
    new_strict: list[tuple[Posting, Verdict]] = []
    new_loose: list[tuple[Posting, Verdict]] = []
    skipped = 0
    # Tracked separately by tier: a strict hit must never be suppressed by a
    # role that was only ever seen at the loose tier. See
    # Store.fingerprint_delivered.
    strict_fps: set[str] = set()
    any_fps: set[str] = set()

    for post in postings:
        if not post.uid or not post.title:
            continue
        if store.seen_uid(post.uid):
            skipped += 1
            continue

        verdict = classify(post)
        fp = post.fingerprint()

        if verdict.tier is None:
            store.record(post, None, verdict.reason)
            continue

        # Same role already delivered under another uid: keep the row so uid
        # dedupe still works, but do not notify a second time.
        if verdict.tier == STRICT:
            duplicate = fp in strict_fps
        else:
            duplicate = fp in any_fps
        duplicate = duplicate or store.fingerprint_delivered(fp, verdict.tier)

        if duplicate:
            store.record(post, verdict.tier, verdict.reason, notified=True, digested=True)
            skipped += 1
            continue

        any_fps.add(fp)
        if verdict.tier == STRICT:
            strict_fps.add(fp)
        store.record(post, verdict.tier, verdict.reason)
        (new_strict if verdict.tier == STRICT else new_loose).append((post, verdict))

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
    total_sources = len(cfg.COMPANIES) + len(cfg.TRACKERS)

    if seed:
        recorded = 0
        for post in postings:
            if post.uid and post.title and not store.seen_uid(post.uid):
                verdict = classify(post)
                store.record(
                    post, verdict.tier, verdict.reason,
                    notified=True, digested=True, seeded=True,
                )
                recorded += 1
        store.commit()
        print(f"\nSeeded {recorded} postings as already-seen. No alerts sent.")
        return 0

    new_strict, new_loose, skipped = triage(postings, store)
    print(
        f"\n{len(postings)} fetched · {skipped} already seen · "
        f"{len(new_strict)} strict · {len(new_loose)} queued for digest"
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

    postings, errors = fetch_all(http, store)
    total_sources = len(cfg.COMPANIES) + len(cfg.TRACKERS)

    # Apply the same fingerprint dedupe a real run would, so the counts here
    # reflect what would actually ping rather than raw source rows. RBC alone
    # publishes the same co-op through two Workday sites.
    strict: list[tuple[Posting, Verdict]] = []
    seen_fp: set[str] = set()
    raw_strict = 0
    loose = 0
    stale = 0
    for post in postings:
        verdict = classify(post)
        if verdict.tier == STRICT:
            raw_strict += 1
            fp = post.fingerprint()
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            if is_fresh(post.posted_at):
                strict.append((post, verdict))
            else:
                stale += 1
        elif verdict.tier == LOOSE:
            loose += 1

    dupes = raw_strict - len(seen_fp)
    notes = []
    if dupes:
        notes.append(f"{dupes} duplicates collapsed")
    if stale:
        notes.append(f"{stale} older than {cfg.MAX_AGE_HOURS}h")
    print(
        f"\n{total_sources - len(errors)}/{total_sources} sources ok · "
        f"{len(postings)} postings · {len(strict)} strict · {loose} loose"
        + (f"  ({', '.join(notes)})" if notes else "")
    )

    if strict:
        print(f"\nStrict matches posted in the last {cfg.MAX_AGE_HOURS}h:")
        for post, verdict in strict[:40]:
            print(f"  · {post.company} — {post.title}")
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
    print(
        "Test alert fired. You should see a Windows notification."
        if ok
        else "Test alert FAILED, see above."
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
        flag = "ok" if row["ok"] else "FAIL"
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
    parser.add_argument("--test", action="store_true", help="fire one fake alert")
    parser.add_argument("--health", action="store_true", help="print the health table")
    parser.add_argument("--open", action="store_true", help="rebuild and open the dashboard")
    parser.add_argument("--db", default=DB_PATH, help=f"SQLite path (default {DB_PATH})")
    args = parser.parse_args(argv)

    if args.sniff:
        return cmd_sniff(args.sniff)

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
