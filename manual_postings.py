"""Postings you add by hand: roles you found somewhere the radar can't read.

Glassdoor, Indeed, Workopolis, LinkedIn and sites like intern-list.com don't
allow automated reading, so the radar never fetches them. When you find a
role there, add it here and it is listed like any other role on the next
run: the same tiers, the same Toronto filter, and dedupe against a copy the
radar already found on the employer's own board.

Each entry needs ``url``. Two ways to add one:

1. Best: paste the EMPLOYER's link (the page "Apply" takes you to, on
   Workday, Greenhouse, the company's careers site, ...). The radar reads the
   title, company, place and date from that page, so the url is enough.

2. A Glassdoor, Indeed, Workopolis or LinkedIn link: the radar won't open
   those pages, so also fill in ``title``, ``company`` and ``location``.

Optional fields:
    title, company, location  -- override what the page says
    term       -- "Summer 2027", when the posting names no term
    added      -- "2026-11-10"; the role counts as posted that day. Without
                  it, the day the radar first sees the entry is used, so it
                  stays in the feed for the usual freshness window
    found_on   -- where you saw it, "Glassdoor"; shown nowhere, just notes

Delete an entry and the role drops out of the feed after two runs.

Example:

    {"url": "https://rbc.wd3.myworkdayjobs.com/en-US/RBCEARLYTALENT1/job/...",
     "found_on": "Workopolis"},
    {"url": "https://www.glassdoor.ca/job-listing/...",
     "title": "Machine Learning Intern", "company": "Example AI",
     "location": "Toronto, ON", "term": "Summer 2027", "added": "2026-11-10"},
"""

MANUAL_POSTINGS: list[dict] = [
]
