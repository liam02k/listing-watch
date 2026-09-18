# Is HTML scraping actually viable here?

Written after the monitor got an instant HTTP 403 from Liam's Mac while Chrome on
the same machine, same IP, same second loaded the identical URL fine.

> **RESOLVED (2026-09-18).** The diagnostic ran on Liam's Mac and the answer is
> yes: **Chrome headers + a homepage cookie warm-up on a persistent
> `requests.Session`** returns the full 515 KB results page. No new dependency.
> That is now the default path; the rest of this document is the reasoning and
> the fallback plan if it stops working.
>
> The one genuine surprise: `curl_cffi`, the "usual fix", returned HTTP 200 with
> 13 KB and **zero listings** — a silent soft block. It is now ordered last, and
> success is judged by listings parsed rather than status code.

**Short answer: yes, via cookies. The fallback if that breaks is a real browser,
not a cleverer HTTP request — and the official API is closed to us.**

## What we know

eBay sits behind **Akamai Bot Manager**. That isn't a guess from the 403; it's
visible in the cookie jar the browser accumulated on that search page:
`bm_so` and `bm_lso` are Akamai's, and the HttpOnly ones we can't read from JS
(`_abck`, `bm_sz`) are the load-bearing part. Alongside them sit `__uzma`/`__uzmb`/
`__uzmc`/`__uzmd`/`__uzmf` — a second behavioural-fingerprinting layer.

Akamai Bot Manager checks, roughly in order of how cheap they are to defeat:

| Signal | What we were sending | Fixable? |
|---|---|---|
| Header content and order | Chrome 140 UA with **no `sec-ch-ua` client hints at all** — a combination real Chrome never produces | Yes, done |
| Cookies | None. First request, cold, straight to the search page | Yes, done (warm-up) |
| HTTP version | HTTP/1.1. Chrome speaks h2 to eBay | Yes, via httpx |
| TLS/JA3 fingerprint | Python/OpenSSL — unmistakably not a browser | Only via curl_cffi |
| JS sensor cookie (`_abck`) | Never generated; it requires executing eBay's JS | **No.** Needs a real browser |

The first four are what `diagnose.py` walks through. If the answer is the fifth,
no HTTP client will do, however well dressed.

## The three real options

### 1. Fix the request signature — try this first

Cheap, keeps the current design, and the monitor is already built for it: the
`Fetcher` class takes `auto | requests | httpx | curl_cffi`, so switching is one
env var once we know which works.

- Headers + cookies + h2 are already in. If `diagnose.py` variant 2, 3 or 4 wins,
  we're done with no new dependency.
- If only variant 5 wins, add `curl_cffi` (one small dependency, wraps
  curl-impersonate to reproduce Chrome's TLS handshake) and set
  `EBAY_HTTP_CLIENT=curl_cffi`.
- **Honest risk:** this is an arms race we don't control. Akamai updates; a
  working curl_cffi setup can stop working after an eBay deploy, with no warning
  beyond the health alert firing. Budget for occasional maintenance.

### 2. Drive a real browser on a schedule — the robust bet

If nothing in `diagnose.py` works, this is where to go, and honestly it's where
I'd bet for the long run regardless.

A real browser executes eBay's JS, so `_abck` gets generated, the TLS handshake is
genuinely Chrome's, and the fingerprint is real rather than impersonated. Playwright
with a persistent profile, run every 5 minutes, handing the page HTML to the exact
same `parse_listings()` we already have and trust.

- **Cost:** ~300 MB for a Chromium install, a few hundred MB of RAM per poll, and
  roughly 3–5 seconds per run instead of 300 ms
- **Benefit:** it's the thing that provably works on this machine — the browser
  pane has fetched this search successfully many times today
- Everything downstream — parsing, the $45 filter, seen-state, Discord embeds,
  the autocorrect check — is unchanged. Only `fetch_search_html()` is replaced.
- This rules out hosted/serverless even more firmly than before: you'd be running
  a full browser on a datacenter IP that Akamai already distrusts.

### 3. The official eBay Browse API — closed, and worth saying why

This is the answer everyone reaches for, and Liam's instinct to separate "a
developer API key" from "signing in with my account" was exactly right. Two
findings, in increasing order of how much they matter:

**It does need an eBay account.** A developer account is created by signing in
with eBay credentials. But this is a fair distinction: the monitor would then
authenticate with a *client-credentials* application token — an app identity, not
a user session. It would not touch his listings, watchlist, or purchases. So the
"don't use my eBay login" requirement is not really the obstacle.

**The obstacle is that production access is partner-only.** The Buy APIs
(including Browse) are a limited release. The sandbox is open to anyone and
contains no real listings. Production requires applying through the eBay Partner
Network, submitting a business model with mocks and data flows, signing several
contracts, passing an Application Growth Check, and waiting ~10 business days —
and approval is explicitly not guaranteed, being granted on the basis of the
proposed business model. "I want to watch for wrestling cards" is not a business
model eBay is minded to approve.

So: not viable, for reasons that have nothing to do with his account.

### Non-option: RSS

Already tested and dead — `&_rss=1` redirects to eBay's category index. There is
no alternative public endpoint that returns this search as structured data.

## What I'd bet on

1. **Run `deploy/diagnose.py`.** One minute, decides everything.
2. If a variant wins: set `EBAY_HTTP_CLIENT` to it, reinstall, done. Accept that
   it may need revisiting when eBay changes something — the health alert means
   you'll be told rather than discovering it months later.
3. If nothing wins: switch to Playwright. More moving parts, much more durable,
   and it keeps every piece of logic we've already tested.

Either way the monitor stays honest about failure, which is the part that matters
most: a blocked poll exits non-zero, the installer refuses to install on a failed
preflight, and three consecutive failures put a message in Discord. The failure
mode that would actually hurt — quietly never firing — is the one we've closed off.

Sources:
[Buy APIs Requirements](https://developer.ebay.com/api-docs/buy/static/buy-requirements.html),
[Application growth check](https://developer.ebay.com/api-docs/static/gs_use-the-application-growth.html),
[eBay Buy APIs: sandbox is open, production is partner-only](https://vorplabs.com/agent-tools/ebay-buy-api),
[Getting your OAuth credentials](https://developer.ebay.com/api-docs/static/oauth-credentials.html),
[Does eBay Allow Scraping? Anti-Bot & Best Proxies](https://scrapeops.io/websites/ebay/).
