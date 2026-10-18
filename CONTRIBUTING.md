# Contributing

Thanks for helping. This radar is only as good as the sources it reads, and
most of the best improvements are small ones: one company it was missing, one
posting it put in the wrong place, one board whose API changed.

You don't need to write code to help. [Opening an issue](../../issues/new/choose)
is often the most useful contribution.

## Ways to help

| You noticed… | Do this |
|---|---|
| A Toronto-area company the radar misses | Open an **Add a company** issue, or add it yourself (see below). |
| A role in the wrong tier, or a real role missing | Open a **Wrong or missing listing** issue with the posting link. |
| A source showing **FAIL** or **empty** in the Sources table | Open a blank issue with the source name and, if you know it, where the company posts jobs now. |
| A crash, a garbled feed, or anything else | Open a blank issue with the command you ran and the output. |
| A community tracker or VC job board worth reading | Open an issue, or add it to `TRACKERS` / `PORTFOLIO_BOARDS` in [companies.py](companies.py). |

## Setting up

Python 3.12 or newer.

```bash
git clone https://github.com/krishi-shah/Toronto-AI-ML-internship-radar.git
cd Toronto-AI-ML-internship-radar
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium   # only for careers pages flagged browser: True
```

Run the tests:

```bash
python -m unittest discover -s tests
```

Check every source against the live sites without recording anything. Pass a
throwaway database so your local state stays clean:

```bash
python radar.py --check --db /tmp/check.db
```

On Windows, `/tmp` usually doesn't exist; in PowerShell use
`--db "$env:TEMP\check.db"` instead.

New here? Issues labelled
[good first issue](../../labels/good%20first%20issue) are a good place to start.

## Adding a company

1. Find its board. Point the sniffer at the careers page:
   ```bash
   python radar.py --sniff https://www.example.com/careers
   ```
   If that finds nothing (common for JavaScript careers sites), guess the board
   token instead:
   ```bash
   python radar.py --probe example example-ai
   ```
2. Paste the printed line into `COMPANIES` in [companies.py](companies.py),
   in the section that fits. Use the name the community trackers use, so a role
   seen on both is listed once.
3. Set `"ai_native": True` only if *every* engineering role there is an AI
   role.
4. Run `python radar.py --check --db /tmp/check.db` and confirm the new source
   reports `ok` with postings.

Only add companies with roles in the commute area (the GTA, Hamilton,
Kitchener-Waterloo, Cambridge, Guelph) or remote within Canada. Anything else
is rejected by the classifier anyway.

## Changing what gets classified where

The rules live in [core.py](core.py) (`classify`) and are explained under
**How it works** in the [README](README.md). Every change here needs a test
in [tests/test_classify.py](tests/test_classify.py) built from the real title
and location that prompted it. A classifier bug silently hides jobs, so
changes without a test can't be merged.

## Adding a new source platform

Adapters live in [sources.py](sources.py). Each one:

- is `fn(http, company, token) -> list[Posting]` and is registered in `ADAPTERS`;
- uses the provider's own job id in `uid` (`"platform:token:id"`), never a list
  index or a timestamp, or every run re-alerts the same posting;
- returns `Partial(...)` when a search stopped at a page cap, so a capped list
  never marks postings as closed;
- raises on failure; the orchestrator records the error against the source;
- gets a `_SNIFF_PATTERNS` entry and a `board_from_url` case where the platform
  can be recognised, so `--sniff` and automatic discovery find it.

Add a test in [tests/test_adapters.py](tests/test_adapters.py) using a trimmed
copy of a real response, and say in your pull request which live board you
verified it against.

## Please don't edit

- **The listings block in README.md**, between the `radar:listings` markers.
  GitHub Actions regenerates it every run.
- **[auto_companies.py](auto_companies.py).** The radar rewrites it from
  discovery evidence. To keep a board permanently, add it to `companies.py`.

## Be polite to the sites we read

Every source is a public endpoint that a careers page itself uses. Keep it
that way: no logins, no bypassing bot protection, no scraping sites whose
terms forbid it (LinkedIn, Indeed, Glassdoor), and no lowering the
per-host delay in `sources.Http`.

## Pull requests

- Keep each pull request to one change.
- Run `python -m unittest discover -s tests` before you push; the same tests
  gate every scheduled run.
- Say in the description what the change does and how you verified it.
- Match the surrounding code: comments explain *why*, especially when a live
  site behaved unexpectedly.

By contributing you agree that your contribution is licensed under the
repository's [MIT License](LICENSE), and that you'll follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
