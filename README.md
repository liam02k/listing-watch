# eBay → Discord listing monitor

Watches an eBay search sorted by **newly listed**, and posts a Discord embed the
moment a listing you haven't seen before shows up at or above your price threshold.

Out of the box it watches the **2026 Topps WWE Universe "SLAMMED"** insert set,
alerts on anything **$45 and up with no upper limit**, and polls every
**5 minutes**.

No eBay account, no login, no API keys. It reads the public search results page
exactly the way a browser does.

---

## Status: ready to run

- **Webhook configured** — it's in `.env` (gitignored, `chmod 600`). `.env.example`
  still holds a placeholder, so it's safe to commit.
- **Test ping delivered** — Discord returned HTTP 204 on 2026-09-18.
- **State seeded** — `seen_items.json` holds the 5 listings that were up on
  2026-09-18, so your first real run is silent and you only hear about genuinely
  new ones.
- **Search URL corrected** — see [The search URL](#the-search-url); the original
  one was watching the wrong cards entirely.

Nothing else to set up. Jump to [Run it](#run-it), then
[deploy/HOSTING.md](deploy/HOSTING.md) to keep it running without a terminal.

The rest of this section is only needed if you ever have to rotate the webhook.

### Create the Discord webhook

1. In Discord, pick (or create) the channel you want alerts in.
2. Hover the channel in the sidebar → the gear icon (**Edit Channel**).
3. **Integrations** → **Webhooks** → **New Webhook**.
4. Give it a name (e.g. *eBay Watch*) and an avatar if you like, then
   **Save Changes**.
5. Click **Copy Webhook URL**. It looks like
   `https://discord.com/api/webhooks/123456789012345678/AbCdEf...`

You need **Manage Webhooks** permission on the server. On mobile the path is
Channel → Settings → Webhooks.

Treat that URL like a password — anyone holding it can post to your channel.
Don't commit it to git.

### Paste it in

Either set an environment variable (recommended):

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/…"
```

…or copy `.env.example` to `.env`, paste it there, and `set -a; source .env; set +a`.

Or, if you'd rather keep it in the file, edit this line near the top of
`ebay_monitor.py`:

```python
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE")
```

---

## Setup

```bash
cd ebay-monitor
python3 -m venv .venv && source .venv/bin/activate     # optional but tidy
pip install -r requirements.txt
```

Python 3.9+.

## Run it

`.env` is picked up automatically — no `source` step, and a real environment
variable still overrides it.

```bash
# Confirm Discord is wired up -- sends one "monitor is live" message
python ebay_monitor.py --test-ping

# Real run: poll forever, alert on new listings at $45 and up
python ebay_monitor.py

# See what it finds right now without sending anything
python ebay_monitor.py --once --dry-run --no-filter --verbose-listings
```

Stop it with Ctrl-C.

**First run is quiet on purpose.** With no `seen_items.json`, everything currently
listed gets recorded as "seen" without alerting — otherwise you'd get 60 messages
in one go. Yours is already seeded with the 60 listings that were up on
2026-09-18, so you'll only hear about new ones. Pass `--alert-existing` if you'd
rather be told about what's up now, or delete `seen_items.json` to re-seed.

### Flags

| Flag | What it does |
|---|---|
| `--once` | Single poll, then exit (good for cron) |
| `--test-ping` | Send one "monitor is live" message to Discord and exit. No eBay request, no state change |
| `--dry-run` | Never POST to Discord; print the payload instead. Doesn't update state, so you can re-run it |
| `--no-filter` | Ignore the price threshold — show everything |
| `--verbose-listings` | Log every listing parsed, marked `NEW` and `>` (clears threshold) |
| `--alert-existing` | On first run, alert on listings already up |
| `--threshold 100` | Override the price bar |
| `--interval 600` | Override the poll interval (seconds) |
| `--url "…"` | Watch a different search |
| `--parse-file page.html` | Parse a saved file instead of hitting the network |
| `--dump-html page.html` | Save the fetched HTML (useful if eBay changes markup) |

### Config

Everything lives in a `CONFIG` block at the top of `ebay_monitor.py`, and every
value can be overridden by an environment variable — see `.env.example`.

| Setting | Env var | Default |
|---|---|---|
| Search URL | `EBAY_SEARCH_URL` | the WWE Universe Slammed search, `_sop=10` |
| Price threshold | `EBAY_PRICE_THRESHOLD` | `45` (inclusive, no max) |
| Poll interval | `EBAY_POLL_INTERVAL` | `300` |
| Webhook | `DISCORD_WEBHOOK_URL` | *(placeholder)* |
| State file | `EBAY_STATE_FILE` | `seen_items.json` |
| Alerts per poll cap | `EBAY_MAX_ALERTS_PER_CYCLE` | `10` |

If you change the search URL, keep `&_sop=10` on it — that's what makes eBay
return newest-first. And read the next section first.

## The search URL

```
https://www.ebay.com/sch/i.html?_nkw=wwe+universe+slammed+-slammer&_sop=10
```

Two parts of that are load-bearing, both found by chasing a listing that the
monitor should have caught and didn't:

**`-slammer`** — eBay decides "slammed" is a typo and silently searches for
**slammer** instead, returning 2019 Slam Attax Universe singles (top price ~$32)
rather than the 2026 SLAMMED insert set (top price $10,000). It does this with
HTTP 200 and a completely normal-looking page; the only tell is the results
heading. Excluding the word `slammer` defeats the correction — the heading then
reads *"5 results for wwe universe slammed -slammer"*. Adding `_blrs=spell_check`
or quoting the phrase does **not** work (quoting returns 0 results).

**No `_sacat`** — the category filter `261328` (Trading Card Singles) was hiding
a real $2,000 Undertaker SLAMMED insert filed elsewhere. Searching all categories
costs nothing: the query is precise enough that it returns ~5 listings, all from
the set you want.

`check_autocorrect()` re-checks this on **every poll** and logs a loud warning if
eBay ever starts rewriting the query again:

```
WARNING eBay AUTO-CORRECTED your search and returned a different result set.
WARNING   you asked for : 'wwe universe slammed'
WARNING   eBay searched : 'wwe universe slammer'
WARNING Alerts below are for the WRONG items. Fix the search URL -- adding
WARNING   -slammer  to the keywords usually defeats the autocorrect.
```

If you want wider coverage at the cost of some noise, `wwe slammed -slammer`
(29 results) also catches all five inserts, but drags in 1987 WWF "The Giant Is
Slammed" cards, a few of which clear $45.

## Getting past eBay's bot management

eBay sits behind Akamai Bot Manager. A bare scripted request gets an instant 403
even from a residential IP that Chrome uses successfully on the same machine.
`deploy/diagnose.py` measured what actually works here:

| Variant | Result |
|---|---|
| requests + old headers | 403 |
| requests + Chrome 148 headers, no cookies | 403 |
| **requests + Chrome headers + homepage warm-up** | **200, 515 KB, 6 listings** |
| httpx HTTP/2 + warm-up | 200, 6 listings |
| curl_cffi impersonate=chrome | 200, 13 KB, **0 listings** — silent soft block |

So it's the **cookies**, not the TLS fingerprint. Before each search the monitor
GETs `ebay.com` to collect eBay's bot cookies, then requests the search on the
same session.

Two things worth knowing:

**The warm-up is expected to fail.** It returns 403 and just two cookies
(`bm_s`, `bm_so`) — and the search that follows then succeeds. The cookies are
what matter, not the status code, so a non-200 warm-up is never treated as an
error. Don't "fix" that.

**curl_cffi is deliberately last** in the `auto` order. It returns a plausible
HTTP 200 that parses to nothing, which is the worst failure a quiet monitor can
have. This is also why success is judged by *listings parsed*, never by status
code.

Cookies expire, so the session re-warms every 30 minutes
(`EBAY_WARM_MAX_AGE`), and any blocked-looking poll drops the cookie jar,
re-warms and retries **once** before being counted as a failure. Override the
client with `EBAY_HTTP_CLIENT` (`auto` | `requests` | `httpx` | `curl_cffi`) or
`--http-client`; re-run `deploy/diagnose.py` if eBay ever changes the rules.

## When the monitor itself breaks

Silence from a monitor is ambiguous — it means "nothing new" *or* "I stopped
working", and those look identical. So after `EBAY_ALERT_AFTER_FAILURES`
consecutive failed polls (default 3) it posts one orange warning embed to
Discord, and one green "recovered" embed when polling works again. Set the env
var to `0` to turn this off.

---

## How it decides what to alert on

1. Fetch the search page with normal browser headers and a persistent session.
2. Parse every result card (`li.s-card`; the older `li.s-item` grid is handled
   too). eBay's sponsored "Shop on eBay" filler card is dropped.
3. Drop anything whose item ID is already in `seen_items.json`.
4. Keep what's priced **at or above** the threshold. The comparison is
   inclusive — a listing at exactly $45.00 alerts — and there is **no upper
   bound**, so a $45 card and a $5,000 card both come through.
5. Batch up to 10 into one Discord message, post, and record every ID seen this
   poll — including the ones that didn't clear the bar, so they can't alert later.

**Price ranges.** A multi-variation listing shows as `$45.00 to $95.00`. It only
qualifies if the *low* end clears the threshold, so a range only alerts when
every variation is at or above it. Change `qualifies()` in the script if you'd
rather alert when any variation is.

**Currency.** The threshold is compared against the number as listed. If your
search returns non-USD prices (`C $`, `£`), the comparison is naive — adjust the
threshold or filter the search to a single currency.

## Being polite to eBay

- One request per poll, 5 minutes apart, with ±10% jitter so it's not perfectly robotic.
- A real browser User-Agent plus the usual `Accept` / `Sec-Fetch-*` headers, on a
  session that keeps cookies.
- On HTTP 429/403/503, a redirect away from the results page, or a results page
  with zero cards, the poll is treated as a **soft block**: logged as a warning,
  and the next poll waits 2×, 4×, up to 8× the normal interval. It backs off
  rather than hammering, and resets after the first good poll.
- Network errors, filesystem errors, and anything else unexpected are caught and
  logged — the loop never dies. After 10 consecutive failures it says so loudly.
- Discord 429s are honoured via `retry_after`; 5xx get three tries with backoff.

Polling faster than ~2 minutes is how you get rate-limited. 5 minutes is fine.

## State

`seen_items.json` maps eBay item IDs to when they were first seen:

```json
{ "version": 1, "seen": { "257749615059": "2026-09-18T15:24:51+00:00" } }
```

Written atomically, so a crash mid-write can't corrupt it; if it's damaged
anyway, the script backs it up to `.corrupt` and starts clean. Entries older than
45 days are pruned. Delete the file to start over (your next run will go quiet
again while it re-seeds).

## Keeping it running

**Recommended: launchd on this Mac.**

```bash
./deploy/install-launchd.sh
```

It runs one live test poll first and **refuses to install** unless that poll
reaches eBay, parses at least one listing, and comes back without an autocorrect
warning. Only then does it install a launch agent that starts at login and
restarts on crash — no terminal window. Logs go to `monitor.log`. Uninstall with
`./deploy/install-launchd.sh --uninstall`.

The plist holds **no secret**: the monitor reads `.env` itself, so the webhook
lives in exactly one file (`chmod 600`). The installer aborts if the webhook ever
turns up in the plist.

Useful afterwards:

```bash
launchctl list | grep ebaymonitor       # running? (a PID in column 1 = yes)
tail -f monitor.log                     # watch it poll
launchctl bootout gui/$(id -u)/com.liam.ebaymonitor   # stop
```

Why not a hosted/serverless runner? eBay's anti-bot stack scores datacenter IPs
poorly and its characteristic failure is an HTTP 200 page with **no results** —
which a quiet monitor cannot distinguish from "nothing new". The full comparison,
including what I measured and a ready GitHub Actions workflow if you want it
anyway, is in **[deploy/HOSTING.md](deploy/HOSTING.md)**.

<details>
<summary>The plist, if you'd rather write it by hand</summary>

Create `~/Library/LaunchAgents/com.liam.ebaymonitor.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.liam.ebaymonitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/full/path/to/ebay-monitor/.venv/bin/python</string>
    <string>/full/path/to/ebay-monitor/ebay_monitor.py</string>
  </array>
  <key>WorkingDirectory</key><string>/full/path/to/ebay-monitor</string>
  <key>EnvironmentVariables</key>
  <dict><key>DISCORD_WEBHOOK_URL</key><string>https://discord.com/api/webhooks/…</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/full/path/to/ebay-monitor/monitor.log</string>
  <key>StandardErrorPath</key><string>/full/path/to/ebay-monitor/monitor.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.liam.ebaymonitor.plist
```

`chmod 600` that file — it contains the webhook.

</details>

**cron instead** — use `--once` and let cron do the scheduling:

```
*/5 * * * * cd /full/path/to/ebay-monitor && DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/…" .venv/bin/python ebay_monitor.py --once >> monitor.log 2>&1
```

## Tests

```bash
python tests/test_parse.py
```

64 offline checks — parsing against markup captured from the live eBay page,
threshold boundaries ($44.99 skipped, $45.00 exactly alerts, $1M alerts),
Discord payload limits, state persistence, no re-alerting across restarts,
soft-block detection, and malformed-card recovery. No network, no Discord.

## When it breaks

eBay redesigns its search page every so often. Symptom: `No listing cards found`
warnings every poll.

```bash
python ebay_monitor.py --once --dry-run --dump-html page.html
```

Open `page.html` and look at what wraps each result. If the class names moved,
update the selectors in `parse_listings()` and `_parse_card()` — they're all in
one place, near the top of the parsing section.

If instead you see `eBay returned a block/error page` occasionally, that's normal
throttling; the backoff handles it. If it's *every* poll, raise the interval or
try again in an hour.

> A note on eBay's RSS: appending `&_rss=1` to a search URL used to return a
> feed, but as of September 2026 it just redirects to eBay's category index — the
> feed is gone. That's why this scrapes the HTML page instead. If eBay brings it
> back, RSS would be the gentler option.
