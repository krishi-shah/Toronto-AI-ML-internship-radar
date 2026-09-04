"""Alert delivery: Windows toasts, a local HTML dashboard, and the README feed.

Backends share one small interface so channels can be swapped without touching
the pipeline. ``companies.NOTIFIERS`` decides which are active, and the
``RADAR_NOTIFIERS`` environment variable overrides that list for one run --
GitHub Actions uses it to render the README without also writing a dashboard
file nobody will open.

Every backend writes to a plain file or to Windows' own notification API via
PowerShell; none of them needs a third-party package or an account. The only
one whose output leaves the machine is ``readme``, which GitHub Actions commits
to a public repository -- so it escapes scraped text rather than trusting it.
"""

from __future__ import annotations

import html
import os
import subprocess
import sys
import time
from typing import Any, Iterable, Optional, Protocol, Sequence

import companies as cfg
from datetime import datetime

from core import Posting, Verdict, toronto_now

DASHBOARD_PATH = os.environ.get("RADAR_DASHBOARD", "radar.html")
README_PATH = os.environ.get("RADAR_README", "README.md")

# The generated listings replace everything between these two markers, so the
# hand-written documentation around them survives every run.
LISTINGS_START = "<!-- radar:listings:start -->"
LISTINGS_END = "<!-- radar:listings:end -->"

# More than this many toasts at once is unreadable; the rest are summarised.
MAX_TOASTS_PER_RUN = 5

# How many rows each tier contributes to a rendered feed.
STRICT_LIMIT = 60
LOOSE_LIMIT = 150


class Notifier(Protocol):
    """A delivery channel. Every method returns True on success."""

    name: str

    def strict(self, hits: Sequence[tuple[Posting, Verdict]]) -> bool: ...
    def digest(self, rows: Sequence[Any]) -> bool: ...
    def health(self, message: str) -> bool: ...


# --------------------------------------------------------------------------
# Windows toast
# --------------------------------------------------------------------------

# Toasts must be raised under a registered AppUserModelID. PowerShell's own is
# always present on Windows, so borrowing it avoids having to register one.
_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid).Show($toast)
"""


def _xml_escape(text: str) -> str:
    return html.escape(text or "", quote=True)


def _raise_toast(title: str, body: str, launch: str = "") -> bool:
    """Show one Windows notification. Clicking it opens ``launch`` if given.

    The script is fed to PowerShell on stdin rather than through ``-Command``
    so that job titles containing quotes, ampersands and parentheses cannot
    break the command line.
    """
    if not sys.platform.startswith("win"):
        return False

    attrs = ""
    if launch:
        attrs = f' activationType="protocol" launch="{_xml_escape(launch)}"'

    toast_xml = (
        f"<toast{attrs}><visual><binding template=\"ToastGeneric\">"
        f"<text>{_xml_escape(title)}</text>"
        f"<text>{_xml_escape(body)}</text>"
        f"</binding></visual></toast>"
    )

    script = (
        f"$aumid = '{_AUMID}'\n"
        f"$xml = @'\n{toast_xml}\n'@\n"
        f"{_TOAST_PS}"
    )

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
            input=script,
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  ! toast failed: {exc}")
        return False

    if proc.returncode != 0:
        print(f"  ! toast failed: {(proc.stderr or '').strip()[:200]}")
        return False
    return True


class ToastNotifier:
    """Native Windows notifications, one per strict hit."""

    name = "toast"

    def strict(self, hits: Sequence[tuple[Posting, Verdict]]) -> bool:
        shown = 0
        for post, verdict in hits[:MAX_TOASTS_PER_RUN]:
            cycle = "  (Winter 2027)" if verdict.cycle == "target" else ""
            body = f"{post.company} - {post.location or 'location not listed'}{cycle}"
            if _raise_toast(post.title, body, post.url):
                shown += 1
            time.sleep(0.3)  # let the shell queue them in order

        overflow = len(hits) - MAX_TOASTS_PER_RUN
        if overflow > 0:
            _raise_toast(
                f"+{overflow} more AI/ML roles",
                "Open the radar dashboard to see the rest.",
                _dashboard_uri(),
            )
        return shown > 0

    def digest(self, rows: Sequence[Any]) -> bool:
        if not rows:
            return True
        return _raise_toast(
            f"Daily digest: {len(rows)} maybe-relevant roles",
                "Student-level roles in Ontario that missed the strict filter.",
            _dashboard_uri(),
        )

    def health(self, message: str) -> bool:
        return _raise_toast("Radar health warning", message[:180], _dashboard_uri())


def _dashboard_uri() -> str:
    """file:// URI for the dashboard, so a toast click opens it."""
    return "file:///" + os.path.abspath(DASHBOARD_PATH).replace("\\", "/")


# --------------------------------------------------------------------------
# Local HTML dashboard
# --------------------------------------------------------------------------

_CSS = """
:root{
  --bg:#f7f7f8; --panel:#fff; --ink:#1a1a1c; --muted:#6b6b73; --line:#e4e4e8;
  --accent:#1f6feb; --strict:#0d8050; --strict-bg:#e8f5ef;
  --loose:#8a6d1f; --loose-bg:#fbf5e4; --fail:#c0392b;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#101013; --panel:#18181c; --ink:#ececf0; --muted:#9a9aa4; --line:#2a2a31;
  --accent:#589bff; --strict:#4ade80; --strict-bg:#12291f;
  --loose:#e3b341; --loose-bg:#2a2312; --fail:#f87171;
}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1.25rem 4rem;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,"Segoe UI",Roboto,system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:1.35rem;margin:0 0 .2rem;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:.85rem;margin-bottom:1.75rem}
h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.09em;
  color:var(--muted);margin:2rem 0 .75rem;font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:.85rem 1rem;margin-bottom:.5rem;display:flex;gap:1rem;
  align-items:baseline;flex-wrap:wrap}
.card a.title{font-weight:600;color:var(--ink);text-decoration:none;font-size:.97rem}
.card a.title:hover{color:var(--accent);text-decoration:underline}
.meta{color:var(--muted);font-size:.85rem}
.when{margin-left:auto;color:var(--muted);font-size:.78rem;white-space:nowrap}
.when.hot{color:var(--strict);background:var(--strict-bg);font-weight:700;
  padding:.16rem .5rem;border-radius:999px}
.tag{font-size:.68rem;font-weight:700;padding:.16rem .45rem;border-radius:4px;
  letter-spacing:.04em;text-transform:uppercase}
.tag.s{color:var(--strict);background:var(--strict-bg)}
.tag.l{color:var(--loose);background:var(--loose-bg)}
.empty{color:var(--muted);font-style:italic;padding:.85rem 0}
table{width:100%;border-collapse:collapse;font-size:.85rem}
td{padding:.4rem .5rem;border-bottom:1px solid var(--line)}
td.n{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.ok{color:var(--strict)} .bad{color:var(--fail);font-weight:600}
.scroll{overflow-x:auto}
"""


def _day_label(ts: int) -> tuple[str, int]:
    """Human age of a posting, as ``(label, whole_days_ago)``.

    Counted in calendar days in Toronto rather than in 24-hour blocks, so a
    role posted at 11pm last night reads "Yesterday" at 1am, not "2h ago".
    """
    now = toronto_now()
    try:
        then = datetime.fromtimestamp(int(ts or 0), tz=now.tzinfo)
    except (OSError, OverflowError, ValueError):
        return ("unknown", 999)

    days = (now.date() - then.date()).days
    if days <= 0:
        return ("Today", 0)
    if days == 1:
        return ("Yesterday", 1)
    return (f"{days} days ago", days)


def _e(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


def _window_label(max_age_hours: int) -> str:
    """"168" is not a number anyone reads as a week."""
    if max_age_hours >= 48:
        return f"{max_age_hours // 24} days"
    return f"{max_age_hours} hours"


def _when_label(row: Any) -> tuple[str, bool]:
    """Age of a posting as ``(label, is_today)``.

    Sources that expose no publish date fall back to when the radar first
    spotted the link. Say "found", never "posted" -- we do not know when it was
    published, and implying we do is how a month-old role looks new.
    """
    when, days = _day_label(row["shown_at"])
    if not row["posted_at"]:
        when = f"found {when[0].lower()}{when[1:]}"
    return when, days == 0


def _row_html(row: Any, tag: str) -> str:
    label = "strict" if tag == "s" else "loose"
    when, today = _when_label(row)
    hot = " hot" if today else ""

    return (
        f'<div class="card"><span class="tag {tag}">{label}</span>'
        f'<a class="title" href="{_e(row["url"])}" target="_blank" rel="noopener">'
        f'{_e(row["title"])}</a>'
        f'<span class="meta">{_e(row["company"])}'
        f' &middot; {_e(row["location"] or "location not listed")}</span>'
        f'<span class="when{hot}">{_e(when)}</span></div>'
    )


class DashboardWriter:
    """Rewrites ``radar.html`` from the database on every run."""

    name = "dashboard"

    def __init__(self, store: Any, path: str = DASHBOARD_PATH) -> None:
        self.store = store
        self.path = path

    # The dashboard reflects the whole database, so the per-run arguments are
    # unused; it is refreshed the same way whatever triggered it.
    def strict(self, hits: Sequence[tuple[Posting, Verdict]]) -> bool:
        return self.render()

    def digest(self, rows: Sequence[Any]) -> bool:
        return self.render()

    def health(self, message: str) -> bool:
        return self.render()

    def render(self) -> bool:
        max_age = getattr(cfg, "MAX_AGE_HOURS", 168)
        window = _window_label(max_age)
        strict_rows = self.store.recent_jobs("strict", STRICT_LIMIT, max_age)
        loose_rows = self.store.recent_jobs("loose", LOOSE_LIMIT, max_age)
        health = self.store.health_rows()

        ok_count = sum(1 for h in health if h["ok"])
        stamp = toronto_now().strftime("%A %d %B, %H:%M")

        body = [
            '<div class="wrap">',
            "<h1>Internship radar</h1>",
            f'<div class="sub">Updated {_e(stamp)} Toronto &middot; '
            f"{ok_count}/{len(health)} sources healthy &middot; "
            f"last {window}, newest first</div>",
            f"<h2>Strict &middot; {len(strict_rows)} AI/ML matches</h2>",
        ]

        body.append(
            "".join(_row_html(r, "s") for r in strict_rows)
            if strict_rows
            else f'<div class="empty">Nothing posted in the last {window}. '
            "New AI/ML roles appear here within the hour of going live.</div>"
        )

        body.append(f"<h2>Loose &middot; {len(loose_rows)} to review</h2>")
        body.append(
            "".join(_row_html(r, "l") for r in loose_rows)
            if loose_rows
            else f'<div class="empty">Nothing in the last {window}.</div>'
        )

        body.append("<h2>Sources</h2><div class=\"scroll\"><table>")
        for h in health:
            state = (
                '<span class="ok">ok</span>'
                if h["ok"]
                else f'<span class="bad">FAIL</span> {_e((h["last_error"] or "")[:90])}'
            )
            body.append(
                f'<tr><td>{_e(h["source"])}</td><td class="n">{h["job_count"] or "-"}</td>'
                f"<td>{state}</td></tr>"
            )
        body.append("</table></div></div>")

        page = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Internship radar</title>"
            f"<style>{_CSS}</style></head><body>{''.join(body)}</body></html>"
        )

        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(page)
        except OSError as exc:
            print(f"  ! dashboard write failed: {exc}")
            return False
        return True


# --------------------------------------------------------------------------
# README feed
# --------------------------------------------------------------------------

# Everything below renders text scraped from third-party job boards into a
# file that gets committed to a public repository, so none of it is trusted.

_MD_SPECIALS = ("|", "[", "]", "*", "_", "`", "~", "#")


def _md(text: Any) -> str:
    """Escape scraped text so it cannot break a table row or inject markup.

    A single unescaped ``|`` in a job title silently shifts every column of
    that row, and GitHub renders raw HTML inside markdown, so angle brackets
    become tags. Newlines end the row outright.
    """
    out = " ".join(str(text or "").split())
    out = out.replace("\\", "\\\\")
    out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in _MD_SPECIALS:
        out = out.replace(char, "\\" + char)
    return out


def _md_url(url: Any) -> str:
    """Return ``url`` if it is safely linkable, else "" so the caller drops it.

    Tracker repos are community-edited, so a posting URL is attacker-adjacent
    input: anything but http(s) is refused rather than published as a
    clickable link. Parentheses and spaces would terminate the link target
    early, so they are percent-encoded instead.
    """
    raw = str(url or "").strip()
    if not raw or any(c in raw for c in "\r\n\t"):
        return ""
    if not raw.lower().startswith(("http://", "https://")):
        return ""
    for char, encoded in (
        (" ", "%20"), ("(", "%28"), (")", "%29"),
        ("<", "%3C"), (">", "%3E"), ('"', "%22"),
    ):
        raw = raw.replace(char, encoded)
    return raw


def _md_row(row: Any) -> str:
    when, today = _when_label(row)
    title = _md(row["title"])
    url = _md_url(row["url"])
    return (
        f"| [{title}]({url}) " if url else f"| {title} "
    ) + (
        f"| {_md(row['company'])} "
        f"| {_md(row['location'] or 'not listed')} "
        f"| {'**' + _md(when) + '**' if today else _md(when)} |"
    )


def _md_table(rows: Sequence[Any], window: str) -> list[str]:
    if not rows:
        return [f"_Nothing in the last {window}._"]
    return [
        "| Role | Company | Location | Posted |",
        "|---|---|---|---|",
        *(_md_row(r) for r in rows),
    ]


class ReadmeWriter:
    """Rewrites the listings block of README.md from the database.

    This is the only backend whose output is published: GitHub Actions commits
    the README after every run, which is what makes the feed readable with the
    laptop closed.
    """

    name = "readme"

    def __init__(self, store: Any, path: str = README_PATH) -> None:
        self.store = store
        self.path = path

    # Like the dashboard, this reflects the whole database rather than one
    # run's hits, so the per-run arguments are unused.
    def strict(self, hits: Sequence[tuple[Posting, Verdict]]) -> bool:
        return self.render()

    def digest(self, rows: Sequence[Any]) -> bool:
        return self.render()

    def health(self, message: str) -> bool:
        return self.render()

    def _block(self) -> str:
        max_age = getattr(cfg, "MAX_AGE_HOURS", 168)
        window = _window_label(max_age)
        strict_rows = self.store.recent_jobs("strict", STRICT_LIMIT, max_age)
        loose_rows = self.store.recent_jobs("loose", LOOSE_LIMIT, max_age)
        health = self.store.health_rows()
        ok_count = sum(1 for h in health if h["ok"])

        lines = [
            f"_Updated {toronto_now():%A %d %B, %H:%M} Toronto"
            f" &middot; {ok_count}/{len(health)} sources healthy"
            f" &middot; last {window}, newest first._",
            "",
            f"### Strict &middot; {len(strict_rows)} AI/ML matches",
            "",
            *_md_table(strict_rows, window),
            "",
            f"### Loose &middot; {len(loose_rows)} to review",
            "",
            *_md_table(loose_rows, window),
            "",
            "### Sources",
            "",
            "| Source | Postings | Status |",
            "|---|---|---|",
        ]

        for row in health:
            state = "ok" if row["ok"] else f"**FAIL** {_md((row['last_error'] or '')[:90])}"
            lines.append(
                f"| {_md(row['source'])} | {row['job_count'] or '-'} | {state} |"
            )

        return "\n".join(lines)

    @staticmethod
    def _without_stamp(block: str) -> str:
        """The block minus its timestamp line, for change detection.

        The stamp moves every run whether or not anything was found. Comparing
        without it is what keeps a quiet radar from committing an identical
        README every hour.
        """
        return "\n".join(
            line for line in block.splitlines() if not line.startswith("_Updated ")
        ).strip()

    def render(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as fh:
                current = fh.read()
        except OSError as exc:
            print(f"  ! readme read failed: {exc}")
            return False

        start = current.find(LISTINGS_START)
        end = current.find(LISTINGS_END)
        if start < 0 or end < 0 or end < start:
            print(
                f"  ! {self.path} has no {LISTINGS_START} / {LISTINGS_END} pair,"
                " leaving it untouched"
            )
            return False

        block = self._block()
        head = current[: start + len(LISTINGS_START)]
        tail = current[end:]
        old_block = current[start + len(LISTINGS_START) : end]

        # An unchanged feed must not produce a diff, or the scheduled job
        # commits every hour forever.
        if self._without_stamp(old_block) == self._without_stamp(block):
            return True

        try:
            with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f"{head}\n\n{block}\n\n{tail}")
        except OSError as exc:
            print(f"  ! readme write failed: {exc}")
            return False
        return True


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_notifiers(names: Iterable[str], store: Any) -> list[Notifier]:
    """Instantiate the configured backends, skipping any that are unavailable.

    ``RADAR_NOTIFIERS`` overrides the configured list for one run, so the same
    checkout can render toasts and a dashboard locally while GitHub Actions
    renders only the README.
    """
    override = os.environ.get("RADAR_NOTIFIERS", "").strip()
    if override:
        names = [part.strip() for part in override.split(",") if part.strip()]

    built: list[Notifier] = []
    for name in names:
        if name == "toast":
            if sys.platform.startswith("win"):
                built.append(ToastNotifier())
            else:
                print("  ! toast backend is Windows-only, skipping")
        elif name == "dashboard":
            built.append(DashboardWriter(store))
        elif name == "readme":
            built.append(ReadmeWriter(store))
        else:
            print(f"  ! unknown notifier '{name}', skipping")
    return built


def dispatch(notifiers: Sequence[Notifier], method: str, *args: Any) -> bool:
    """Call one method on every backend. A failing channel never stops another."""
    sent = False
    for notifier in notifiers:
        try:
            sent = bool(getattr(notifier, method)(*args)) or sent
        except Exception as exc:  # noqa: BLE001 - a broken channel is not fatal
            print(f"  ! {notifier.name} {method} failed: {exc}")
    return sent
