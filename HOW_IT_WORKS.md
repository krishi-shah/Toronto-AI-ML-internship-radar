# How the radar works

The [README](README.md) has the live feed. This page covers how it is built:
the schedule, setup, the CLI, alert channels, adding companies, and how
filtering, dedupe and publishing work.

## What the schedule actually means

GitHub Actions runs the radar six times a day, at 01:17, 04:17, 13:17, 16:17,
19:17 and 22:17 UTC. Those slots stay between about 8am and midnight in
Toronto both before and after daylight saving, because the cron is UTC and
does not follow the clock change.

GitHub cron is best-effort, not a real-time scheduler. Runs are queued and can
start well after their slot under load, which is why each slot sits at `:17`
rather than on the hour, where the queue is most congested.

Nothing is lost to a late run. Every run re-fetches every source, and the
freshness window uses the provider's publish date, so a delayed run produces
the same feed a punctual one would.

Two other things worth knowing about scheduled workflows:

- **The repo must be public.** Actions minutes are unlimited on public repos.
  On a private repo this schedule is ~180 runs a month.
- **GitHub disables schedules after 60 days of repository inactivity.** The
  radar's own README commits count as activity, so an active feed keeps itself
  alive. If it ever does go idle, one `workflow_dispatch` re-enables it.

## Setup

### Cloud, which is the point

1. Push this to a **public** GitHub repo.
2. Actions → **radar** → *Run workflow* with mode **`seed`**. Seeding marks
   the ~40,000 currently-live postings as already seen. **Skip this and the
   first real run treats the entire backlog as new.**
3. Run it once more with mode **`run`** to render the feed immediately, then
   leave the schedule to it.

Do the same seed-then-run whenever the cache prefix in the workflow is bumped
(currently `radar-db-v6-`). Stored rows keep the tier they were classified
into, so a classifier change only reaches old postings through a fresh DB.

No secrets to configure. The workflow commits with the built-in `GITHUB_TOKEN`
and talks to nothing but public job boards.

State (`radar.db`: seen postings, per-source health, HTTP etags) is carried
between runs by `actions/cache`, not committed. It is ~5 MB of SQLite, and a
commit every ten minutes would add gigabytes of binary history a year. A cache
miss is survivable — every run re-fetches everything, so the only visible
effect is that postings with no publish date get re-dated as first seen today.

### Local, optional

Useful for adding companies and checking sources; not required for the feed.

```powershell
cd internship-radar
pip install -r requirements.txt
python -m playwright install chromium

python radar.py --check    # every source ok/FAIL, plus recent strict matches
python radar.py --open     # build radar.html and open it
```

`--check` prints the AI/ML roles posted in the last 48 hours, with apply links.
Locally the HTML dashboard is the default channel; `radar.html` is gitignored
and never published.

A local run shares nothing with the cloud: it keeps its own `radar.db`, so
running `--check` here cannot affect what the scheduled job considers new.

---

## CLI

```
python radar.py                  normal run: fetch, classify, alert
python radar.py --check          per-source ok/FAIL with counts, no alerts
python radar.py --seed           mark everything currently live as seen
python radar.py --digest         release the queued loose digest
python radar.py --open           rebuild and open the HTML dashboard
python radar.py --sniff <url>    detect the ATS behind a careers page
python radar.py --probe <slug>…  guess a board token on Ashby, Greenhouse, Lever, …
python radar.py --test           fire one fake alert through the channels
python radar.py --health         per-source last success, last error, counts
python radar.py --discovered     boards found from Canadian postings, with status
python radar.py --reasons        stored postings by tier and reason, with sample titles
```

## Alert channels

Set in `NOTIFIERS` in [companies.py](companies.py), or per-run with the
`RADAR_NOTIFIERS` environment variable, which is how the workflow renders the
README and feeds without also writing a dashboard nobody will open.

| Backend | What it does |
|---|---|
| `readme` | Rewrites the listings block of the README. **Used by the scheduled job.** |
| `feed` | Writes `feed.xml` (RSS) and `feed.json`: strict then loose roles in the feed window, up to 200. **Used by the scheduled job.** |
| `dashboard` | Rewrites `radar.html`. **The local default.** |
| `toast` | Native Windows notification per strict hit, click to apply. Capped at 5 per run. Windows-only; add it to `NOTIFIERS` to enable. |

Because the README carries both tiers, the 5pm digest is now optional — loose
roles are visible without it. `--digest` still works for the toast channel.

Two other knobs live in the same file:

| Setting | Meaning |
|---|---|
| `MAX_AGE_HOURS = 168` | How fresh a posting must be to appear in loose and other fields, and to trigger an alert. 168 = seven days; drop to 48 for a stricter feed. |
| `STRICT_MAX_AGE_HOURS = 336` | How long strict (AI/ML) roles stay listed. 336 = two weeks. Never shorter than `MAX_AGE_HOURS`. |
| `DISCOVERY_TIME_BUDGET_S = 330` | Seconds after the configured sources finish during which discovered boards keep being scraped. |
| `FETCH_MAX_S = 900` | Hard stop for discovery, retries and sniffing, counted from the start of the fetch. Keep it well under the workflow's 25-minute timeout. |
| `DISCOVERY_MAX_BOARDS = 300` | Hard cap on discovered boards per run; 0 turns discovery off. |
| `SNIFF_PER_RUN = 8` | Unrecognised careers hosts checked for a supported ATS each run; 0 turns it off. A page that names no ATS waits 30 days, one that named a platform waits 7, and a fetch error waits 14. Leftover budget sniffs another batch. |
| `PROMOTE_MAX = 60` | Discovered boards written into `auto_companies.py` after they return Canadian student roles. |
| `TARGET_CYCLES = [(2027, 4), (2027, 5)]` | Spring and Summer 2027, the May to August term. Only these reach strict; anything later than the last is demoted. A title naming a live cycle (*ML Engineer – Summer 2027*) counts as a student role. Bump it next cycle. |
| `EARLIEST_LIVE_CYCLE = (2027, 1)` | Cycles from here up to the first target (Winter 2027) are demoted to loose instead of rejected; anything earlier is rejected. Set it to the first target to drop them. Rows already stored move down on their next sighting, with no alert. |
| `PORTFOLIO_BOARDS` | VC job boards on Getro or Consider (Inovia, Real Ventures, Golden Ventures), each listing roles at many portfolio startups. |
| `CLOSED_AFTER_MISSES = 2` | A posting leaves the feed once its source has returned a complete listing without it this many runs in a row. |
| `AI_DESC_MIN_TERMS = 3` | Distinct ML terms (PyTorch, LLMs, model training, …) a job description needs to make a tech role strict. |
| `AI_DESC_MIN_MENTIONS = 6` | Or this many AI mentions across at least two distinct terms: a software role whose description is mostly AI work. |
| `DESC_PER_RUN = 150`, `DESC_TIME_BUDGET_S = 180` | Cap on job descriptions read per run: tech student roles with no AI word in the title, and student roles rejected for naming no Canadian location (the posting page often names the city). Each posting is read once; the backlog drains over later runs. A description only ever promotes a row. |

Backends share one small interface in [notify.py](notify.py) — `strict`,
`digest`, `health`. Adding another channel (email, ntfy) means writing one
class with those three methods and adding its name to the list.

## Adding companies

Point the sniffer at a careers page:

```bash
python radar.py --sniff https://www.some-ai-startup.com/careers
```

It prints a line ready to paste into `COMPANIES` in
[companies.py](companies.py):

```
    {"name": "Some AI Startup", "platform": "greenhouse", "token": "someaistartup", "ai_native": False},
```

Set `ai_native: True` when *every* engineering role at the company is an AI
role. A generic title like "Software Engineer Intern" at an `ai_native`
company earns an instant alert instead of waiting for the 5pm digest.

Then confirm before trusting it: `python radar.py --check`.

A careers page that draws its list in the browser gets `browser: True` and
the page URL as `token`. The radar opens it in Chromium, then reads the same
`JobPosting` data or job links as any other HTML source. Point `token` at the
search results (internships, or Ontario), not the marketing homepage.

```
    {"name": "Some Portal", "platform": "html", "token": "https://jobs.example.com/search?q=intern", "ai_native": False, "browser": True},
```

Company-hosted SuccessFactors career sites (`/search/?q=...`) are read
directly: the token is the site's own search URL, narrowed to Ontario. Only
the older `careerN.successfactors.com` hosted sites get an HTML-fallback line
from the sniffer.

When the careers page is rendered client-side or sits behind Cloudflare, the
sniffer sees nothing. Guess the token instead — the ATS APIs are usually still
reachable even when the company's own site is not:

```bash
python radar.py --probe deepgenomics deep-genomics
```

It tries each slug on Ashby, Greenhouse, Lever, SmartRecruiters, Workable,
BambooHR and JazzHR, and prints a paste-ready line for every board that has postings. An empty board
counts as a miss: SmartRecruiters answers 200 with an empty list for any slug.

### Workday

Workday tokens cannot be guessed; the tenant and site slugs rarely match the
company name. Sniffing the careers URL itself usually works:

```bash
python radar.py --sniff https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
```

If it does not, copy the real one from the browser: DevTools → **Network**,
filter **Fetch/XHR**, search for a job, find the POST whose URL contains
`/wday/cxs/`, and use that full URL (it ends in `/jobs`) as the `token`.

A posting URL already in the feed works too: in
`https://rbc.wd3.myworkdayjobs.com/RBCEARLYTALENT1/job/...` the tenant is `rbc`
and the site is `RBCEARLYTALENT1`, so the token is
`https://rbc.wd3.myworkdayjobs.com/wday/cxs/rbc/RBCEARLYTALENT1/jobs`. That is
how the RBC, TD, CIBC, BMO and Manulife boards were added.

---

## How it works

Three coverage layers feed one dedupe-and-notify pipeline.

**Layer 1 — ATS APIs.** Public JSON endpoints, no auth. One adapter per
platform in [sources.py](sources.py): Ashby, Greenhouse, Lever,
SmartRecruiters, Workable, Recruitee, Teamtailor, Breezy, Personio (XML),
Workday, Oracle Cloud Recruiting (Nokia, Definity, BGIS), Eightfold
(Qualcomm, Microsoft, Ericsson), iCIMS's Jibe sites (AMD), classic iCIMS
portals (Kinaxis, Mackenzie), SuccessFactors career sites (TELUS, Rogers,
Scotiabank), and the startup ATSes BambooHR (Signal 1), Rippling and JazzHR,
plus Amazon's own careers search (`amazon.jobs/en/search.json`),
which is where its Toronto ML and robotics internships are posted. Classic
iCIMS has no JSON, so its server-rendered listing is parsed, and its
`CA-ON-Ottawa` location codes are spelled out for the location gate.

Workday is the awkward one: its search is keyword-driven, so a single query
never surfaces everything. Each board is queried for `intern`, `co-op`,
`student` and `new grad`, paginated by offset, and merged on the provider's own
requisition id. Amazon's search is keyword-driven the same way and is handled
identically. Workday rows that say only "2 Locations" are resolved with one
detail request each (student titles only, capped per board), so a TD co-op in
Toronto and Montreal is read as the Toronto role it is.

**Portfolio job boards.** VC funds publish one job board listing every open
role across their portfolio — the Toronto AI startups no tracker covers.
Inovia and Real Ventures run on Getro, Golden Ventures on Consider; each run
pages through every Canadian posting on them, and each posting keeps its own
startup's name. Their links point at the startups' own ATS boards, which
feeds discovery below.

**Discovered boards.** Every Canadian posting the trackers and portfolio
boards carry links to the employer's own board, so each run mines those links
(Ashby, Greenhouse, Lever, SmartRecruiters, Workable, both Workday URL forms,
Oracle, Eightfold, Jibe, iCIMS, SuccessFactors, BambooHR, Rippling, JazzHR,
Recruitee, Teamtailor, Breezy and Personio) for boards not already in `companies.py`. They are remembered in a
`discovered` table and scraped as a second wave, which surfaces roles no
tracker lists.

The wave runs on a time budget rather than a fixed count: boards are scraped
for `DISCOVERY_TIME_BUDGET_S` (330) seconds after the configured sources
finish, and never past `FETCH_MAX_S` (900) seconds after the fetch began,
leaving headroom under the 25-minute job timeout. The 40 boards with the most Canadian
postings go first every run; the rest follow least recently scraped first, so
a board the budget cut off gets its turn next run. A skipped board is not a
failure. A board that fails five runs in a row, or has had no Canadian
posting in 30 days, drops out.

Links to careers sites no pattern recognises (`jobs.l3harris.com`,
`billtrust.com`) are not thrown away either. Each run fetches up to
`SNIFF_PER_RUN` (8) of them, most linked first, and looks for an ATS
signature in the page; any supported board found joins the discovered boards.
Each host is re-checked on a backoff: 30 days when the page names no ATS, 7
days when it named a platform, 14 days after a fetch error.

If the time budget is not used up, the run retries each configured source
that failed, once, and sniffs another batch of hosts.

A discovered board that keeps returning Canadian student roles is promoted
into [auto_companies.py](auto_companies.py), which `all_companies()` appends
to the curated list. Promotion needs a strict (AI/ML) hit, or at least two
student roles on a successful scrape, and the board must not already be
curated. It is demoted after three failed scrapes, or two successful scrapes
in a row with no student roles. Noise boards (agencies, huge non-Canadian
boards) stay in the opportunistic wave. The file is rewritten at the end of
a normal run or `--seed`, never by `--check`, and the workflow commits it
with the README. `ai_native` is never set automatically.

Discovered boards stay out of the Sources table and the failure warning
until they are promoted. They get one summary line under it instead.
`python radar.py --discovered` lists each board as promoted, waiting, or
demoted, plus every sniffed host.

**Layer 2 — careers pages.** For careers pages with no API. If the page embeds
schema.org `JobPosting` data (what Google for Jobs indexes), those records are
used: real titles, locations, remote scope and publish dates. Otherwise it
falls back to the link diff: extract every anchor, filter to same-domain or
job-ish links, and treat any new link as a candidate. Crude by design: it
cannot miss a link appearing. Section menu links ("Student & New Grad",
"Early Careers") are skipped. Pages flagged `browser` are opened in Chromium
first, two at a time, because their job list is not in the first HTML
response.

**Job descriptions.** A title alone undersells plenty of ML work: a
"Software Developer Co-op" on a model-training team never says AI. So a tech
role in the commute area that missed strict only for want of an AI word has
its description read — through the platform's own API for Greenhouse, Lever,
Ashby, Workday, SmartRecruiters and Oracle, and otherwise from the posting
page's schema.org `JobPosting` or its text. When it names at least three
distinct ML terms (PyTorch, LLMs, model training, NLP, …; a bare "AI" never
counts) the role is promoted to strict and tagged *AI in description* with the
terms that matched. Each posting is read once, within `DESC_PER_RUN` and
`DESC_TIME_BUDGET_S`, and rows left over from earlier runs are read on later
ones. The same page also gives careers-page links the location and date their
link text lacks.

**Layer 3 — community trackers.** Nine tracker repos, including
speedyapply's AI/ML-only list and Jobright's daily SWE and data internship
lists, which link the title itself and carry same day Toronto roles. Branch names differ (`dev` vs `main`), so each
is tried rather than hardcoded. Apply buttons that are a badge image wrapped
in a link resolve to the link, never the badge.

> **Note on format:** these repos no longer agree on layout. Simplify's README
> is now an HTML `<table>`, not a markdown pipe table — a pipe parser reads it
> as zero rows and fails silently. So the fetcher prefers each repo's
> structured `.github/scripts/listings.json` (stable ids, `active` flags,
> seasons) and falls back to a tolerant table parser handling markdown pipes,
> HTML tables, header-order differences, and `↳` continuation rows. ETags mean
> an unchanged 10 MB listings file costs one 304.

### Filtering

Two tiers, because a wide net plus one channel equals noise.

**strict** — instant notification. Requires all of:

- an AI/ML signal in the title, **or** an `ai_native` company, **or** a tech
  title whose job description names enough ML terms (tagged *AI in
  description*)
- an internship-level signal (intern, co-op, student, placement, work term, …)
- a location in the commute area, Ontario with no city named, **or** remote
  within Canada
- no cycle token pointing exclusively at a season other than Spring or Summer
  2027 (a title naming a live cycle such as Summer 2027 also counts as the
  internship-level signal)

**loose** — tech-relevant student roles in the commute area that miss
strict, plus
roles that say only "Canada" with no province. Without an AI signal, the title
needs a tech term (software, developer, engineering, data, analytics, cloud,
security, QA, IT, product, R&D, …) to land here. Full-time early-career
roles (new grad, entry level, early talent) are capped here even with an AI
title: they are a different race from a 2027 co-op.

**other fields** — student roles in Ontario with no AI signal whose title
either names a discipline with no AI angle (analog and mixed-signal design,
PCB, ASIC, firmware, signal integrity, electrical and mechanical engineering,
capital markets, accounting, audit, marketing, sales, HR, treasury, FP&A,
underwriting, branch advisor and relationship-manager roles) or carries no
tech term at all ("Strategy Intern", "Bid Management Intern"). Kept, never
alerted on, and shown collapsed below loose. An AI signal always wins, so
"ML Firmware Intern" stays strict.

**Rejected** — unpaid, volunteer, high school, PhD-*only*, retail and shift
work ("Student Part Time", "Franchise", store departments), **anywhere outside
the commute area**, and **postings for a cycle earlier than the target**. A bare "PhD"
mention does not reject: plenty of ML internships prefer a PhD but accept
master's students. A bare year with no season ("2026 Intern") does not reject
either — too ambiguous.

### Location scope

Ontario is checked first, so a req open in both Toronto and Vancouver is kept
as the Toronto role it is. Then:

- **Ontario outside the commute area is rejected.** The commute area is the
  GTA (Mississauga, Brampton, Vaughan, Markham, Richmond Hill, Oakville,
  Burlington, Milton, Pickering, Ajax, Whitby, Oshawa, Newmarket, Aurora,
  Thornhill, Stouffville, Caledon, Georgetown, Bowmanville, Orangeville, …),
  Hamilton, Kitchener-Waterloo, Cambridge and Guelph. Town names shared with
  US or UK places (Burlington, Milton, Cambridge, Aurora, Concord, …) count only
  when the posting does not pair them with a foreign state or country; a bare
  "Cambridge" is read as the UK or Massachusetts. A posting naming only
  Ottawa, Kanata, London, Kingston, Windsor, Barrie, Niagara or the north is
  rejected; one naming Ottawa *and* Toronto is kept. "Ontario, Canada" with no
  city is kept, since it is nearly always a Toronto head office, and so is
  remote work from anywhere in Ontario.
- **Remote in Canada is kept.** It names no province, and it is workable from
  Toronto. Bare "Remote" is rejected: it says nothing about the country, and
  in practice it is almost always a US role. The exception is a source that
  only lists Canadian roles (a Canadian tracker, a board searched with its own
  Canada filter): its rows are marked Canadian, so its "Remote" means Canada.
- **Bare "Canada" is capped at loose.** No province named, so it cannot be
  confirmed as Ontario, but it is plausibly workable.
- **The rest of Canada is rejected**, not demoted. Vancouver, Montreal,
  Calgary and Halifax are all noise for someone who needs to be in Ontario.
- **No Canadian location means rejected**, in every tier. An empty location,
  "Multiple Locations" or "Remote" alone is not evidence of Canada, and
  keeping them let US roles into loose. Careers pages scraped through the
  link-diff layer carry no location, so their entries in `companies.py` state
  one (`"location": "Toronto, ON"`). Browser rows use the link text as their
  location; when that names no place, the stated location fills it too, but
  a link that names a foreign place is left alone. Workday "N Locations" rows
  are resolved as described above.

The province abbreviation is matched as `,\s*ON(?![\w-])`. Without the
lookahead, a case-insensitive `, ON` matches the "on" in "Hybrid, on-site" and
quietly reads half the internet as Ontario.

City names shared with other places get two more guards. A street suffix
after the name means it is an address, not the city, so RTX's
`US-CT-WINDSOR LOCKS ~ 1 Hamilton Rd` is not Hamilton, Ontario. And
"Ontario, California" and "Ontario, Oregon" are US cities, not the province.

A posting naming several cycles keeps the best one: *"Fall 2026, Summer
2027"* is a live Summer 2027 opportunity, not a dead 2026 one. A season with
no year closing the title (*"Software Engineer Co-op, AI, Summer"*) means the
next such term.

A Toronto city followed by the country code (*Markham, CA*) is Canada, not
California; only a US state or a foreign country after the name rejects it.

### Freshness

Loose and other fields are filtered to the last `MAX_AGE_HOURS` (168, i.e.
seven days) and strict AI/ML roles to `STRICT_MAX_AGE_HOURS` (336, two weeks),
since those are rarer and still worth applying to in their second week. Only a
role inside the seven-day window triggers an alert. Both use
the **provider's** publish date, not when the radar happened to notice. Ages
are counted in calendar days in Toronto rather than 24-hour blocks, so a role
posted at 11pm last night reads "Yesterday" at 1am, not "2h ago". Dates are
parsed from ten different shapes — ISO-8601, epoch seconds, epoch milliseconds,
Workday's "Posted 30+ Days Ago", and tracker table columns like "Aug 21" or
"1d".

About 95% of postings carry a usable date. The rest come from the HTML
link-diff layer, which has no dates by nature — but a link that just appeared
*is* new, so discovery time stands in. An unparseable date counts as fresh
rather than stale: silently dropping a whole source would be the expensive
mistake.

Two deliberate calls, both favouring recall:

- **Secondary locations count.** Ashby routinely lists a US primary with
  Toronto buried in `secondaryLocations`. Every location field is searched,
  nested ones included.
- **Future cycles are demoted, not dropped.** Only cycles *earlier* than the
  target are rejected, since reqs get mislabelled and often span terms.

Co-op spelling variants (`co-op`, `co op`, `coop`, `cooperative education`) and
cycle tokens (`S27`, `Summer 2027`, `2027 Summer`, a bare `Summer` closing
the title) are all covered.

### Dedupe and state

SQLite (`radar.db`) with two layers:

- **uid** — stable per-source key, always the provider's own job id, never an
  index or timestamp. A uid that changes between runs re-notifies the same
  posting forever.
- **fingerprint** — SHA-1 of normalized company + normalized title. Collapses
  legal suffixes, cycle labels, req ids, location suffixes, co-op spellings and
  word order, so one role arriving from both an ATS and a tracker alerts once.
- **requisition id** — the provider's own id read from the posting URL
  (`_R-0000188001` on Workday, `/jobs/34407` on iCIMS and Jibe, Greenhouse's
  `gh_jid`, Lever and Ashby UUIDs). Trackers retitle roles, so RBC's
  *"2027 Winter – GRM, AI Developer Intern - Innovation & AI Center of
  Excellence (4 Months)"* reached the feed four times under four titles; the id
  ties them together. Rows sharing either key form one role, listed once, with
  the employer's own copy shown over a tracker's.

Each source that returns a complete listing also marks which stored postings
it still lists. A posting missing from `CLOSED_AFTER_MISSES` (2) complete
listings in a row is gone and leaves the feed; the employer's board is the
authority, so a tracker still carrying it does not bring it back. Capped
searches (Workday past its page limit), careers-page scrapes and 304s say
nothing about what closed, so they never hide anything — those postings
simply age out.

A `health` table records each source's last success, last error and job count.
If more than 20% of sources fail in a run you get a warning — a silently broken
scraper looks exactly like a quiet hiring week.

### Publishing the feed

The README is committed by the workflow, so two things matter that would not
matter for a local file:

- **Scraped text is escaped, never trusted.** Tracker repos are
  community-edited. A single unescaped `|` in a job title shifts every column
  of its row; GitHub renders raw HTML inside markdown; and a posting URL that
  is not `http(s)` is dropped rather than published as a clickable link.
- **An unchanged feed produces no commit.** The "Updated …" stamp moves every
  run, so the writer compares the listings *without* it and leaves the file
  untouched when nothing changed. Otherwise the schedule would push 6
identical READMEs a day. The badge above, not the stamp, is what proves the
  radar is alive.

### Engineering

- All sources fetched concurrently, 16 workers, with per-host politeness.
- Every adapter wrapped: an exception is recorded against its source and the
  run continues. One dead board never fails a run.
- Retry with exponential backoff on 429 and 5xx, honouring `Retry-After`.
- A real browser User-Agent. Several endpoints reject the stock
  `python-requests` one.
- Toasts are raised through Windows' own notification API via PowerShell, with
  no third-party package.

---

## Tests

```bash
python -m unittest discover -s tests
```

320+ tests over the places a bug silently costs a job: the tier classifier and
its commute-area gate, the dedupe fingerprint and requisition-id grouping,
closed-posting tracking, description scoring, cycle parsing, the freshness
window, every adapter's parsing, tracker and careers-page parsing, board
discovery and promotion, and the escaping and change-detection in the
published feed. Fixtures are real title and location
shapes taken from live boards. The workflow runs them before every scrape, so
a classifier bug cannot publish a garbled feed.

---

## Live status

Verified against real boards, most recent `--check`:

```
80/80 sources ok · 39958 postings · 16 strict · 612 loose · 152 other fields
```

The run finished in about 4 minutes, inside the discovery time budget, and
`--check` left `auto_companies.py` untouched. Syncing that same database
would promote 39 boards that returned Canadian student roles (campus sites
for RBC, BMO, Sun Life and Autodesk among them) into `auto_companies.py` on
the next normal run.

Strict is small on purpose: in `--check` it counts only roles posted in the
last seven days (the alert window) that are Summer 2027 (or undated) AI/ML internships in Ontario.

Endpoint shapes were confirmed against live companies rather than assumed.
Corrections found along the way:

| Assumption | Reality |
|---|---|
| Trackers publish markdown tables | Simplify uses HTML `<table>`; all three Simplify-family repos publish `listings.json`, used instead |
| Wealthsimple is on Greenhouse | Ashby, token `wealthsimple` |
| Ada exposes a careers page | Cloudflare 403 to every non-browser request; disabled, covered via trackers |
| Clio/Xanadu list jobs in HTML | Rendered client-side (Clio also 403s), and `--probe` finds no public board; removed, covered via trackers |
| Workday tokens must come from DevTools | Any Workday posting URL already in the feed carries the tenant and site slugs; RBC, TD, CIBC, BMO and Manulife were added that way |
| TD Layer 6 is on `TD_External` | 404; `--sniff https://layer6.ai/careers/` points at `TD_Bank_Careers` |
| A tracker's apply column holds the job URL | Some wrap a shields.io badge in a link, so the first URL in the cell was the badge image |
| Trackers only list a title and link | `negar` and `vansh` carry date columns, now parsed for the freshness window |
| A tracker's apply link is the posting | `vansh` wraps every link in its `go.vansh.nyc` shortener, which hid the requisition id dedupe needs and the board discovery needs; Canadian short links are now resolved once and cached |
| Only boards linked by Canadian postings are worth checking | Most employers with a Toronto office are only ever linked by their US roles. Every board any tracker row links is now a candidate, and a nightly explore run (about 3am Toronto, 80 minute budget) checks each once and promotes the ones with Canadian student roles |
| OMERS, Ontario Teachers', CPP Investments, Intact, GM, Clio and Uber can't be scraped | Their careers pages hide the board, but tracker rows link postings on it: all seven run on Workday, SmartRecruiters or iCIMS and are now configured |
| A careers site with no API needs a browser | Many publish a job sitemap with dates; the `sitemap` platform reads it plus each posting's JSON LD. Find one with `python radar.py --sitemap <careers url>` |
| Board searches for "intern", "co-op" and "student" catch every student role | U of T's PEY placements are often titled only "PEY", and some search engines never match "coop" for "co-op"; every searchable board now uses one shared, longer term list |
| The title carries the work term | Some trackers put it only in a "Details" column (`Intern · 4mo · Fall 2026`), so cycle detection reads those fields too |
| `first_seen` approximates posting date | True only for links found *after* seeding; seeding stamps everything "now", so undated seeded rows are labelled *date unknown* and listed last, never shown as new |
| The state DB can just be committed | ~5 MB of SQLite every run is gigabytes of binary history a year; it lives in `actions/cache` instead |
| A missing location is harmless in loose | "Remote", blank and "Multiple Locations" rows were overwhelmingly US roles; now a Canadian signal is required in every tier |
| Workday posting URLs all look like `tenant.wdN.myworkdayjobs.com` | Magna, Sysco and others link as `wdN.myworkdaysite.com/recruiting/tenant/Site`; the same tenant answers on the usual host, so discovery maps one to the other |
| Eightfold's `/api/apply/v2/jobs` is the public search | It answers 403 ("Not authorized for PCSX"); `/api/pcsx/search` works and takes a location filter |
| iCIMS portals block scrapers | They answer 405 to any User-Agent containing "(KHTML, like Gecko)", which is most browser strings; a shorter Chrome string gets the page |
| AMD's Jibe search is empty | It filters on the request language, and `Accept-Language: en-CA` matches none of its `en-us` postings |
| Rejecting known foreign places is enough | It knew "united kingdom" but not "UK", and ", NY" but not "Texas", so London and San Francisco roles were read as having no location and kept. Requiring a positive Ontario signal fixes the whole class |

## Known gaps

- **Timing is best-effort.** GitHub queues scheduled runs, so a slot can
  start well after its time. The six daily runs are already a few hours
  apart. A local scheduler was punctual; this is the price of the laptop
  being closed.
- **The commute area is judged from the posting text.** A company that
  writes only "Canada" with no province, and no remote wording, is not
  confirmable as Toronto and lands in loose rather than strict. "Ontario"
  with no city is kept as Toronto, which is occasionally wrong. A posting with no location
  at all is dropped, even if it really is in Toronto.
- **Discovered boards include some noise.** A tracker row can point a
  Canadian posting at a global board (an agency, a UK subsidiary), which is
  then scraped for a month. The location gate keeps its foreign roles out of
  the feed; the cost is only fetch time.
- **Bell, OpenText and Celestica** have no public board API that `--probe`
  finds, and a browser load of Bell's careers page does not draw job links.
  Their roles arrive via the trackers. Shopify is opened in Chromium
  instead.
- **Ada** is disabled: `www.ada.cx` returns Cloudflare 403 to every non-browser
  request, full browser headers included. Ada postings still arrive via the
  trackers.
- **Clio, Xanadu, Limina and Untether AI** are not scraped directly:
  client-side rendering, or a TLS handshake python-requests cannot complete.
  The trackers cover them.
- **Scotiabank** answers 422 to the standard Workday search, so its public
  SuccessFactors careers search (internships in Ontario) is read instead.
- **Description reading is budgeted.** At most `DESC_PER_RUN` descriptions a
  run, so after a reseed the backlog takes a few runs to drain, and a page
  with no `JobPosting` is scored on its visible text, boilerplate included.
- **Cisco** runs Phenom, which has no adapter; on 17 Oct 2026 it had no intern
  role in Canada, so its careers page stays a browser scrape.
- **Careers pages that only link onward** are not scraped: Borealis AI, CPP
  Investments, OMERS, Ontario Teachers', Hydro One, Intact, Meta, GM and
  SickKids show menu links only, and Layer 6, Private AI, Ross Video,
  eSentire, Huawei, Uber, Miovision, Moneris and Descartes rendered no role
  links at all. OpenText renders roles but names no location, and its search
  is global.
- **Host sniffing only sees server-rendered pages.** Okta, Stripe, Elastic
  and most big-tech careers sites build their listings in JavaScript and name
  no ATS in the HTML, so sniffing finds nothing there. Those hosts are then
  left alone for 30 days.
- **Workday "N Locations"** rows are resolved only for student and new-grad
  titles (or titles naming a 2027 work term), up to 80 per board per run; any
  beyond that are dropped as having no Canadian location until a description
  read places them.
- **Small GTA towns can be missed.** The commute list names the towns that
  show up in postings; one it does not know (a hamlet, a business park name)
  with no "ON" after it is rejected as having no Canadian location.
- **Workday boards with no country facet** (Intel offers only city facets)
  are searched globally, so a big global board can still fill the search cap
  with other countries before reaching Toronto.
- **Avature, Phenom and Job Bank** have no adapter; their roles arrive only
  through the trackers.
- **Datacenter IPs get blocked more often** than a home connection, so a
  source can pass locally and fail on the runner. The Sources table in the feed
  is where that shows up.

## Layout

| File | Contents |
|---|---|
| [radar.py](radar.py) | CLI, concurrent orchestration, pipeline |
| [sources.py](sources.py) | ATS adapters, careers-page layer, tracker parsers, sniffer, prober |
| [core.py](core.py) | Tier classifier, fingerprint, SQLite state and health |
| [notify.py](notify.py) | README feed, RSS and JSON feeds, HTML dashboard, Windows toasts, backend interface |
| [HOW_IT_WORKS.md](HOW_IT_WORKS.md) | This page |
| [companies.py](companies.py) | Curated target list, notifiers, freshness and cycle settings |
| [auto_companies.py](auto_companies.py) | Generated list of boards promoted from discovery evidence |
| [.github/workflows/radar.yml](.github/workflows/radar.yml) | The scheduler: scrape, test, render, commit |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to set up, add a company or adapter, and open a pull request |
