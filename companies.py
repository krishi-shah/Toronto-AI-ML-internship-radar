"""Target list for the radar. This is the file you edit constantly.

Add a company by running the sniffer and pasting the line it prints:

    python radar.py --sniff https://www.somecompany.com/careers

Fields
------
name       Display name used in alerts and in the dedupe fingerprint. Match the
           name the trackers use ("Royal Bank of Canada", not "RBC") so a role
           seen on both collapses into one row.
platform   One of: ashby, greenhouse, lever, smartrecruiters, workable,
           recruitee, teamtailor, breezy, personio, workday, amazon, html.
token      The board token. For ``workday`` it is the full ``/wday/cxs/.../jobs``
           URL; for ``amazon`` an ISO-3 country code; for ``html`` it is the
           careers page URL. ``python radar.py --probe <slug>`` guesses tokens.
location   Optional. Filled into postings from this source that carry no
           location, for employers that only hire in one place.
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
    {"name": "Deep Genomics", "platform": "lever", "token": "deepgenomics", "ai_native": True},
    {"name": "Cerebras", "platform": "ashby", "token": "cerebras", "ai_native": True},

    # -- AI labs with Canadian roles ------------------------------------
    {"name": "Anthropic", "platform": "greenhouse", "token": "anthropic", "ai_native": True},
    {"name": "OpenAI", "platform": "ashby", "token": "openai", "ai_native": True},
    {"name": "Scale AI", "platform": "greenhouse", "token": "scaleai", "ai_native": False},

    # -- Toronto / Waterloo / Ottawa tech, verified live 29 Sep 2026 ------
    # Each carries Canadian postings on its board. Not ai_native: their AI roles
    # still need an AI signal in the title to reach strict.
    {"name": "1Password", "platform": "ashby", "token": "1password", "ai_native": False},
    {"name": "StackAdapt", "platform": "greenhouse", "token": "stackadapt", "ai_native": False},
    {"name": "Geotab", "platform": "greenhouse", "token": "geotab", "ai_native": False},
    {"name": "Wattpad", "platform": "lever", "token": "wattpad", "ai_native": False},
    {"name": "Loopio", "platform": "ashby", "token": "loopio", "ai_native": False},
    {"name": "Hopper", "platform": "ashby", "token": "hopper", "ai_native": False},
    {"name": "Clearco", "platform": "ashby", "token": "clearco", "ai_native": False},
    {"name": "Shakepay", "platform": "greenhouse", "token": "shakepay", "ai_native": False},
    {"name": "Neo Financial", "platform": "ashby", "token": "neofinancial", "ai_native": False},
    {"name": "Jobber", "platform": "ashby", "token": "jobber", "ai_native": False},
    {"name": "Float", "platform": "ashby", "token": "float", "ai_native": False},
    {"name": "Hootsuite", "platform": "greenhouse", "token": "hootsuite", "ai_native": False},
    {"name": "Ubisoft", "platform": "smartrecruiters", "token": "ubisoft2", "ai_native": False},
    {"name": "D2L", "platform": "greenhouse", "token": "d2l", "ai_native": False},
    {"name": "Telus Digital", "platform": "ashby", "token": "telus-digital", "ai_native": False},
    {"name": "Loblaw Digital", "platform": "smartrecruiters", "token": "LoblawDigital", "ai_native": False},

    # -- Global tech with Toronto offices -------------------------------
    {"name": "Instacart", "platform": "greenhouse", "token": "instacart", "ai_native": False},
    {"name": "Stripe", "platform": "greenhouse", "token": "stripe", "ai_native": False},
    {"name": "Lyft", "platform": "greenhouse", "token": "lyft", "ai_native": False},
    {"name": "Affirm", "platform": "greenhouse", "token": "affirm", "ai_native": False},
    {"name": "Okta", "platform": "greenhouse", "token": "okta", "ai_native": False},
    {"name": "GitLab", "platform": "greenhouse", "token": "gitlab", "ai_native": False},
    {"name": "Elastic", "platform": "greenhouse", "token": "elastic", "ai_native": False},
    {"name": "Samsara", "platform": "greenhouse", "token": "samsara", "ai_native": False},
    {"name": "Ramp", "platform": "ashby", "token": "ramp", "ai_native": False},
    {"name": "Robinhood", "platform": "greenhouse", "token": "robinhood", "ai_native": False},
    {"name": "Databricks", "platform": "greenhouse", "token": "databricks", "ai_native": False},
    {"name": "MongoDB", "platform": "greenhouse", "token": "mongodb", "ai_native": False},
    {"name": "Pinterest", "platform": "greenhouse", "token": "pinterest", "ai_native": False},
    {"name": "Airbnb", "platform": "greenhouse", "token": "airbnb", "ai_native": False},
    {"name": "Reddit", "platform": "greenhouse", "token": "reddit", "ai_native": False},
    {"name": "Dropbox", "platform": "greenhouse", "token": "dropbox", "ai_native": False},
    {"name": "Tailscale", "platform": "greenhouse", "token": "tailscale", "ai_native": False},
    {"name": "PagerDuty", "platform": "greenhouse", "token": "pagerduty", "ai_native": False},
    {"name": "Snowflake", "platform": "ashby", "token": "snowflake", "ai_native": False},
    {"name": "Twilio", "platform": "greenhouse", "token": "twilio", "ai_native": False},
    {"name": "Coinbase", "platform": "greenhouse", "token": "coinbase", "ai_native": False},
    {"name": "Confluent", "platform": "ashby", "token": "confluent", "ai_native": False},
    {"name": "ServiceNow", "platform": "smartrecruiters", "token": "servicenow", "ai_native": False},

    # -- Own careers sites with a public JSON search ---------------------
    # Amazon hires ML and robotics interns in Toronto (Annapurna Labs, Amazon
    # Robotics) but posts on amazon.jobs only.
    {"name": "Amazon", "platform": "amazon", "token": "CAN", "ai_native": False},

    # -- Workday ---------------------------------------------------------
    # Workday tokens cannot be guessed. To add one:
    #   1. Open the company's Workday careers site in a browser.
    #   2. Open DevTools -> Network, filter XHR, and search for a job.
    #   3. Find the POST whose URL contains "/wday/cxs/".
    #   4. Copy that full request URL (it ends in "/jobs") in as the token.
    # A posting URL already in the feed also works: its tenant and site slugs
    # are the two path pieces the cxs URL needs. Verify with --check.
    #
    # RBC Borealis posts through RBC's own boards, and TD Layer 6 through
    # TD_Bank_Careers (found by sniffing layer6.ai/careers), so the bank
    # entries below are how those two labs are covered.
    {"name": "Royal Bank of Canada", "platform": "workday",
     "token": "https://rbc.wd3.myworkdayjobs.com/wday/cxs/rbc/RBCEARLYTALENT1/jobs",
     "ai_native": False},
    {"name": "Royal Bank of Canada", "platform": "workday",
     "token": "https://rbc.wd3.myworkdayjobs.com/wday/cxs/rbc/rbcglobal1/jobs",
     "ai_native": False},
    {"name": "TD", "platform": "workday",
     "token": "https://td.wd3.myworkdayjobs.com/wday/cxs/td/TD_Bank_Careers/jobs",
     "ai_native": False},
    {"name": "CIBC", "platform": "workday",
     "token": "https://cibc.wd3.myworkdayjobs.com/wday/cxs/cibc/search/jobs",
     "ai_native": False},
    {"name": "CIBC", "platform": "workday",
     "token": "https://cibc.wd3.myworkdayjobs.com/wday/cxs/cibc/campus/jobs",
     "ai_native": False},
    {"name": "BMO", "platform": "workday",
     "token": "https://bmo.wd3.myworkdayjobs.com/wday/cxs/bmo/External/jobs",
     "ai_native": False},
    {"name": "Manulife Financial", "platform": "workday",
     "token": "https://manulife.wd3.myworkdayjobs.com/wday/cxs/manulife/MFCJH_Jobs/jobs",
     "ai_native": False},
    {"name": "Sun Life", "platform": "workday",
     "token": "https://sunlife.wd3.myworkdayjobs.com/wday/cxs/sunlife/Experienced-Jobs/jobs",
     "ai_native": False},
    {"name": "Nvidia", "platform": "workday",
     "token": "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs",
     "ai_native": False},
    {"name": "Intel", "platform": "workday",
     "token": "https://intel.wd1.myworkdayjobs.com/wday/cxs/intel/External/jobs",
     "ai_native": False},
    {"name": "Autodesk", "platform": "workday",
     "token": "https://autodesk.wd1.myworkdayjobs.com/wday/cxs/autodesk/Ext/jobs",
     "ai_native": False},
    {"name": "Ciena", "platform": "workday",
     "token": "https://ciena.wd5.myworkdayjobs.com/wday/cxs/ciena/Careers/jobs",
     "ai_native": False},
    {"name": "Entrust", "platform": "workday",
     "token": "https://entrust.wd1.myworkdayjobs.com/wday/cxs/entrust/entrustcareers/jobs",
     "ai_native": False},
    {"name": "Magna", "platform": "workday",
     "token": "https://magna.wd3.myworkdayjobs.com/wday/cxs/magna/Magna/jobs",
     "ai_native": False},
    # Scotiabank and AMD answer 422 to the standard cxs search; left out until
    # someone captures the request their own site sends.

    # -- Careers pages, HTML layer ---------------------------------------
    # For careers pages with no API. Pages that embed schema.org JobPosting
    # data yield real titles, locations and dates; anything else falls back
    # to the link diff, which flags every new anchor.
    # ``location`` fills postings that carry none. Without it the classifier,
    # which requires a Canadian location, would reject every link-diff row.
    {"name": "Vector Institute", "platform": "html", "token": "https://vectorinstitute.ai/careers/", "ai_native": True, "location": "Toronto, ON"},

    # Checked and left out, all covered by the trackers meanwhile:
    #   Clio, Ada      -- Cloudflare 403 to every non-browser request, and no
    #                     Ashby/Greenhouse/Lever/SmartRecruiters/Workable board
    #   Xanadu, Limina -- listings rendered client-side, no board found
    #   Untether AI    -- TLS handshake fails from python-requests
    #   Kinaxis        -- iCIMS, which answers 405 to non-browser requests
    #   Arteria AI     -- Greenhouse board exists but is empty
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
    # AI/ML-only student roles, which is exactly this radar's target.
    {"name": "speedyapply-AI-2027", "repo": "speedyapply/2027-AI-College-Jobs"},
    {"name": "speedyapply-SWE-2027", "repo": "speedyapply/2027-SWE-College-Jobs"},
    {"name": "zapply-Internships-2027", "repo": "zapplyjobs/Internships-2027"},
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

# Automatic discovery. Every run mines Canadian postings (mostly tracker rows)
# for the employer boards they link to, and scrapes up to this many boards that
# are not configured above, most Canadian postings first. 0 turns it off.
# A board drops out after 5 failures in a row or 30 days without a Canadian
# posting. See them with: python radar.py --discovered
DISCOVERY_MAX_BOARDS = 60

# Thread pool width for source fetching. Requests to one host are serialised
# by the politeness gap anyway, so this mostly overlaps different boards.
MAX_WORKERS = 16
