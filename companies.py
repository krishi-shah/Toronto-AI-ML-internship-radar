"""Target list for the radar. This is the file you edit constantly.

Add a company by running the sniffer and pasting the line it prints:

    python radar.py --sniff https://www.somecompany.com/careers

Fields
------
name       Display name used in alerts and in the dedupe fingerprint.
platform   One of: ashby, greenhouse, lever, smartrecruiters, workable,
           recruitee, teamtailor, breezy, personio, workday, html.
token      The board token. For ``workday`` it is the full ``/wday/cxs/.../jobs``
           URL; for ``html`` it is the careers page URL.
ai_native  True for companies where *every* engineering role is an AI role.
           A generic title like "Software Engineer Intern" at one of these
           still earns an instant ping instead of waiting for the 5pm digest.
"""

COMPANIES: list[dict] = [
    # -- Toronto AI, verified live --------------------------------------
    {"name": "Cohere", "platform": "ashby", "token": "cohere", "ai_native": True},
    {"name": "Waabi", "platform": "lever", "token": "waabi", "ai_native": True},
    {"name": "BenchSci", "platform": "lever", "token": "benchsci", "ai_native": True},
    {"name": "Tenstorrent", "platform": "greenhouse", "token": "tenstorrent", "ai_native": True},
    {"name": "Wealthsimple", "platform": "ashby", "token": "wealthsimple", "ai_native": False},
    {"name": "Faire", "platform": "greenhouse", "token": "faire", "ai_native": False},

    # -- Toronto AI, HTML fallback --------------------------------------
    # No ATS signature is exposed on these careers pages, and token guesses
    # against Ashby/Greenhouse/Lever all 404. The link-diff layer is the
    # fallback: it stores the anchor set and flags any link that is new.
    {"name": "Vector Institute", "platform": "html", "token": "https://vectorinstitute.ai/careers/", "ai_native": True},
    # Clio and Xanadu render their listings client-side, so the link-diff layer
    # sees the page but finds no job anchors. They report ok with 0 postings
    # rather than failing. If you find their real board, sniff it and swap
    # these lines for an API entry -- the trackers cover them meanwhile.
    {"name": "Clio", "platform": "html", "token": "https://www.clio.com/about/careers/search/", "ai_native": False},
    {"name": "Xanadu", "platform": "html", "token": "https://www.xanadu.ai/careers/", "ai_native": True},

    # Ada: www.ada.cx returns Cloudflare 403 to every non-browser request,
    # including with full browser headers. There is no free way through it, so
    # it is disabled rather than left to fail every run and skew the health
    # warning. Ada postings still reach you via the community trackers.
    # {"name": "Ada", "platform": "html", "token": "https://www.ada.cx/careers", "ai_native": True},

    # -- Workday placeholders -------------------------------------------
    # Workday tokens cannot be guessed. To fill one in:
    #   1. Open the company's Workday careers site in a browser.
    #   2. Open DevTools -> Network, filter XHR, and search for a job.
    #   3. Find the POST whose URL contains "/wday/cxs/".
    #   4. Copy that full request URL (it ends in "/jobs") in as the token.
    # The tenant and site slugs differ from the company name often enough that
    # guessing them wastes an evening. Verify with: python radar.py --check
    #
    # {"name": "RBC Borealis", "platform": "workday",
    #  "token": "https://rbc.wd3.myworkdayjobs.com/wday/cxs/rbc/RBCCareers/jobs",
    #  "ai_native": True},
    # {"name": "TD Layer 6", "platform": "workday",
    #  "token": "https://td.wd3.myworkdayjobs.com/wday/cxs/td/TD_External/jobs",
    #  "ai_native": True},
    # {"name": "Scotiabank", "platform": "workday",
    #  "token": "https://scotiabank.wd3.myworkdayjobs.com/wday/cxs/scotiabank/Scotiabank_Careers/jobs",
    #  "ai_native": False},
    # {"name": "Nvidia", "platform": "workday",
    #  "token": "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
    #  "ai_native": True},
]

# Community trackers. Branch names differ per repo, so the fetcher tries dev,
# main and master, and prefers each repo's structured listings.json over the
# README table -- Simplify's README is an HTML <table>, not markdown pipes.
TRACKERS: list[dict] = [
    {"name": "Canadian-Tech-Internships-2027", "repo": "negarprh/Canadian-Tech-Internships-2027"},
    # hanzili/canada_sde_intern_position removed: 404 on both main and master,
    # the repo is gone. It was failing every run and showing red in Sources.
    {"name": "Summer2027-Internships", "repo": "SimplifyJobs/Summer2027-Internships"},
    {"name": "New-Grad-Positions", "repo": "SimplifyJobs/New-Grad-Positions"},
    {"name": "vansh-Summer2027", "repo": "vanshb03/Summer2027-Internships"},
]

# Where alerts go.
#
#   dashboard  rewrites radar.html every run (open it with: radar.py --open)
#   readme     rewrites the listings block of README.md -- this is the one
#              GitHub Actions commits, so the feed stays live with the laptop
#              closed
#   toast      native Windows notification per strict hit, click to apply
#
# Add "toast" back to this list if you ever want popups again. The scheduled
# GitHub job sets RADAR_NOTIFIERS=readme, which overrides this list for that
# run, so a local checkout keeps its dashboard either way.
NOTIFIERS = ["dashboard"]

# Only show postings this fresh. 168 = the last seven days.
# Older postings are still recorded (so dedupe keeps working) but never surface
# on the dashboard. Drop this to 48 if a week starts feeling like too much
# noise; the dashboard labels every role with its age either way.
MAX_AGE_HOURS = 168

# The cycle being targeted, as (year, month) of its first month.
# Winter 2027 starts January 2027. Any posting whose cycle label points at a
# cycle EARLIER than this is rejected outright -- Fall 2026 recruiting is over,
# so those postings are dead weight. Later cycles (Summer 2027, Fall 2027) are
# not rejected, only demoted to the loose tier.
# Bump this when you move on to the next cycle.
TARGET_CYCLE = (2027, 1)

# Warn me if more than this share of sources fail in one run. A silently
# broken scraper looks exactly like a quiet hiring week.
HEALTH_FAIL_THRESHOLD = 0.2

# Thread pool width for source fetching.
MAX_WORKERS = 12
