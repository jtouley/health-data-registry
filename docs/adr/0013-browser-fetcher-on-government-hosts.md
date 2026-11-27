# 0013. The browser fetcher may read government hosts, under stop conditions

- **Status:** Accepted (the maintainer's decision, 24 Nov 2026); superseded in part by
  [ADR 0016](0016-crawler-factory.md) (accepted 27 Nov 2026): the stop rule for a refused robots.txt on a reviewed
  government host, marked below
- **Date:** 2026-11-24
- **Spec:** "Crawling"; "Privacy and licences"
- **Related:** [ADR 0002](0002-primary-sources-only.md) (primary sources only), Hard rule 6 in `CLAUDE.md`
  (fetches only to catalogued publisher hosts, over HTTPS), review of PR #20

## Context

michigan.gov, like many state and county government sites, sits behind an edge network (Akamai). It refuses
plain HTTPS clients from cloud IP ranges, such as GitHub's runners, and serves the same public pages, including
its sitemaps, to a real browser. The weekly sitemap job (`sitemaps.yml`) and the Michigan adapters need those
public pages.

The PR #20 review asked whether switching to headless Chromium after a plain-HTTPS refusal works around the
publisher's bot-management decision. That would contradict the rule that a refusal is final, so it needs a
maintainer's decision, not a workflow comment.

Government publishers put these pages up for the public. The edge network's refusal of cloud HTTP clients is a
broad default against unidentified scrapers, not a decision about an honest, slow, identified reader of a public
sitemap. A real refusal looks different: a challenge page, a CAPTCHA, a 401, 403 or 429.

## Decision

The browser fetcher (`browser_fetch.py`, headless Chromium through Playwright) may be used on michigan.gov and,
in general, on government hosts that the edge network serves to browsers. It may be used only on these terms:

1. **Honest user agent.** Every request names the project and its repository
   (`health-data-registry/<version> (+repo URL)`). No browser impersonation, no rotated or spoofed agents.
2. **At least 10 s between requests to one host,** or the host's robots.txt `Crawl-delay` if that is longer
   (`sitemaps.PACE_S`).
3. **The government-host policy.** robots.txt is read first, by the same browser, and its rules are obeyed.
   Only a 404 or 410 for robots.txt means "no rules". Public pages only: no logins, no forms, no personal data.
4. **No evasion.** No proxies or IP rotation, no stealth plugins, no fingerprint changes, no CAPTCHA solving, no
   retry through another client, address or schedule after a refusal.

**Stop conditions.** Any of these stops the crawl of that host for the run and is recorded as blocked (the seed
is "not taken" and the job opens or updates the "Sitemap refresh blocked" issue):

- a challenge page or interstitial, or an "Access Denied" page (an HTML page where a sitemap was expected);
- a CAPTCHA;
- a 401, 403 or 429, for the page or for robots.txt;
- a robots.txt that disallows the path.

*Superseded in part by [ADR 0016](0016-crawler-factory.md):* on a reviewed government host, a robots.txt that
disallows the path, or a 401, 403 or 429 for robots.txt itself, no longer stops the crawl; it is overridden and
recorded. A 401, 403 or 429 for the page still stops it.

After a block, the only other route is a maintainer downloading the file in their own browser and uploading it
with `sitemaps snapshot --from-file`.

**Hosts.** Hard rule 6 still holds: a host is fetched only when a catalog entry names it. The two sitemap seed
hosts, `www.michigan.gov` and `www.kentcountymi.gov`, are recorded here as an exception until the Michigan
catalog entries (PR #19) name them. They are listed once, in `sitemaps.ADR_0013_HOSTS`, and adding any other
host to that list needs an amendment to this record. Child sitemaps must be HTTPS on the seed's own host.

## Consequences

- The sitemap job and the Michigan adapters can read michigan.gov from GitHub's runners. Until
  `browser_fetch.py` is on main, the sitemap job uses plain HTTPS and the Michigan seeds are refused, which
  opens the blocked issue.
- A crawl with 10 s spacing is slow. The sitemap job's timeout allows for up to 201 sitemaps per seed.
- If a government host starts showing challenge pages or 403s to the browser, the job stops for that host and
  says so; nothing changes client to get past it. The maintainer then asks the publisher, or uses
  `--from-file`.
- A non-government host is not covered by this record.
