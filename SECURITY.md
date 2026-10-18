# Security

The radar publishes text scraped from job boards and community-edited tracker
repos into a public README, so the main risk is untrusted input getting into
that README: a link, script or markup that escapes the escaping in
[notify.py](notify.py).

## Reporting a vulnerability

Please **don't** open a public issue. Report it privately instead: go to the
repository's **Security** tab and choose **Report a vulnerability**.

Include the posting or source that triggers it, and what ends up in the
README. You'll get a reply as soon as the maintainer sees it.

## Scope

In scope: escaping and link handling in the published feed, the GitHub
Actions workflow, and anything that would let a scraped posting run code or
change repository contents.

Out of scope: problems with the third-party job boards themselves.
