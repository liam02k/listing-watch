#!/usr/bin/env python3
"""Offline checks for the eBay monitor. No network, no Discord.

Run from the project root:   python tests/test_parse.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ebay_monitor as em  # noqa: E402

HERE = Path(__file__).resolve().parent
REAL = (HERE / "fixture_ebay_srp.html").read_text("utf-8")
PRICEY = (HERE / "fixture_over_threshold.html").read_text("utf-8")
SLAMMED = (HERE / "fixture_slammed_srp.html").read_text("utf-8")

GOOD_URL = "https://www.ebay.com/sch/i.html?_nkw=wwe+universe+slammed+-slammer&_sop=10"
URL_OK = GOOD_URL

em.WARM_PAUSE = 0          # don't make the test suite sit through real warm-up pauses
BAD_URL = ("https://www.ebay.com/sch/i.html?_nkw=wwe+universe+slammed"
           "&_sacat=261328&_from=R40&_blrs=spell_auto_correct&_sop=10")

passed = failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}{('  -> ' + detail) if detail else ''}")


print("\n== Parsing real eBay markup ==")
listings = em.parse_listings(REAL)
check("3 real listings parsed (sponsored placeholder dropped)", len(listings) == 3,
      f"got {len(listings)}")
check("placeholder /itm/123456 excluded",
      all(l.item_id != "123456" for l in listings))

first = listings[0]
check("title stripped of 'New Listing' / 'Opens in a new window'",
      first.title == "2019 Topps WWE Slam Attax Universe The Shield #287 00l8", first.title)
check("item id", first.item_id == "257749615059", first.item_id)
check("url cleaned of tracking params",
      first.url == "https://www.ebay.com/itm/257749615059", first.url)
check("price parsed", first.price == 5.99, str(first.price))
check("condition", first.condition == "Pre-Owned", first.condition)
check("seller", first.seller == "comc_consignment", first.seller)
check("feedback", first.seller_feedback == "99.5% positive (1.5M)", first.seller_feedback)
check("shipping", first.shipping == "+$4.99 delivery", first.shipping)
check("buying format", first.buying_format == "Buy It Now", first.buying_format)
check("listed timestamp", first.listed_at == "Sep-17 20:31", first.listed_at)
check("thumbnail upgraded to s-l500", first.image.endswith("/s-l500.jpg"), first.image)
check("'New Listing' flag detected", first.is_new_listing is True)

unquoted = [l for l in listings if l.item_id == "168695146965"][0]
check("unquoted-attribute card parsed (as eBay actually serves it)",
      unquoted.price == 32.0 and unquoted.seller == "sworc83",
      f"{unquoted.price} / {unquoted.seller}")

print("\n== Corrected search: the 2026 SLAMMED insert set ==")
slammed = em.parse_listings(SLAMMED)
check("both SLAMMED listings parsed", len(slammed) == 2, f"got {len(slammed)}")

reigns = next((l for l in slammed if "ROMAN REIGNS" in l.title.upper()), None)
check("the Roman Reigns SSP is found", reigns is not None)
if reigns:
    check("its price reads $725.00, not the range low or a bid increment",
          reigns.price == 725.0 and reigns.price_text == "$725.00",
          f"{reigns.price} / {reigns.price_text!r}")
    check("it clears the $45 bar", em.qualifies(reigns, 45.0))
    check("auction format captured", reigns.buying_format == "8 bids", reigns.buying_format)
    check("seller captured", reigns.seller == "alphabetacollect", reigns.seller)
    check("item id", reigns.item_id == "198644598436", reigns.item_id)
    check("emoji in the title survive intact", "ROMAN REIGNS | SLAMMED" in reigns.title)
    check("embed builds cleanly from it", em.build_embed(reigns)["url"].endswith("198644598436"))

wyatt = next((l for l in slammed if "BRAY WYATT" in l.title.upper()), None)
check("the £-converted UK auction parses", wyatt is not None and wyatt.price == 207.06,
      str(wyatt.price if wyatt else None))
check("its shipping estimate is picked up",
      wyatt is not None and "29.56" in wyatt.shipping, wyatt.shipping if wyatt else "")

print("\n== Spell-autocorrect detection (the wrong-result-set failure mode) ==")
check("clean search reports no autocorrect",
      em.check_autocorrect(SLAMMED, GOOD_URL) is None,
      str(em.check_autocorrect(SLAMMED, GOOD_URL)))

corrected_page = SLAMMED.replace("wwe universe slammed -slammer", "wwe universe slammer")
got = em.check_autocorrect(corrected_page, BAD_URL)
check("slammed -> slammer autocorrect is caught", got == "wwe universe slammer", str(got))

check("a heading for a different query is caught",
      em.check_autocorrect(
          '<h1 class="srp-controls__count-heading">2,300+ results for pokemon cards</h1>',
          GOOD_URL) == "pokemon cards")
check("'Save this search' suffix doesn't break the comparison",
      em.check_autocorrect(
          '<h1 class="srp-controls__count-heading">5 results for wwe universe slammed '
          '-slammer<span>Save this search</span></h1>', GOOD_URL) is None)
check("case and spacing differences are not flagged",
      em.check_autocorrect(
          '<h1 class="srp-controls__count-heading">5 results for  WWE Universe  Slammed '
          '-Slammer</h1>', GOOD_URL) is None)
check("missing heading is ignored rather than guessed at",
      em.check_autocorrect("<html><body>no heading</body></html>", GOOD_URL) is None)
check("a URL with no _nkw is ignored",
      em.check_autocorrect(SLAMMED, "https://www.ebay.com/sch/i.html?_sop=10") is None)

print("\n== The shipped default URL is the corrected one ==")
check("default drops the category filter", "_sacat" not in em.SEARCH_URL, em.SEARCH_URL)
check("default excludes the autocorrect term", "-slammer" in em.SEARCH_URL, em.SEARCH_URL)
check("default has no spell_auto_correct param", "_blrs" not in em.SEARCH_URL, em.SEARCH_URL)
check("default still sorts newest first", "_sop=10" in em.SEARCH_URL, em.SEARCH_URL)

print("\n== Price parsing ==")
cases = [
    ("$29.57", 29.57, 29.57),
    ("$2,150.00", 2150.0, 2150.0),
    ("$45.00 to $95.00", 45.0, 95.0),
    ("C $19.99", 19.99, 19.99),
    ("£10.00", 10.0, 10.0),
    ("", None, None),
]
for raw, lo, hi in cases:
    got_lo, got_hi, _ = em.parse_price(raw)
    check(f"parse_price({raw!r}) -> {lo}/{hi}", (got_lo, got_hi) == (lo, hi),
          f"got {got_lo}/{got_hi}")

print("\n== $45-and-up threshold (inclusive, no upper bound) ==")
check("default threshold is 45", em.PRICE_THRESHOLD == 45.0, str(em.PRICE_THRESHOLD))
check("CLI default matches", em.parse_args([]).threshold == 45.0)

pricey = em.parse_listings(PRICEY)
check("4 listings in threshold fixture", len(pricey) == 4, f"got {len(pricey)}")
matches = [l for l in pricey if em.qualifies(l, 45.0)]
check("exactly 3 clear the bar", len(matches) == 3, str([l.price_text for l in matches]))
check("$44.99 excluded (just under)", all(l.price != 44.99 for l in matches))
check("$45.00-$95.00 range included (low end exactly at the bar -> inclusive)",
      any(l.price == 45.0 for l in matches))
check("$189.00 included", any(l.price == 189.0 for l in matches))
check("$2,150.00 included -- no upper bound", any(l.price == 2150.0 for l in matches))

boundary = [
    (44.99, False, "a cent under"),
    (45.00, True, "exactly at the threshold"),
    (45.01, True, "a cent over"),
    (50_000.00, True, "way above any ceiling"),
    (1_000_000.00, True, "absurdly expensive still alerts"),
]
for price, want, label in boundary:
    l = em.Listing(item_id="b", title="t", url="u", price=price, price_high=price)
    check(f"${price:,.2f} {label} -> {'alert' if want else 'skip'}",
          em.qualifies(l, 45.0) is want)

check("a listing with no parseable price never alerts",
      em.qualifies(em.Listing(item_id="n", title="t", url="u"), 45.0) is False)

print("\n== Test-ping payload ==")
ping = em.build_test_payload(em.parse_args(["--test-ping"]))
check("test ping is a single embed", len(ping["embeds"]) == 1)
check("clearly labelled as a test", "Test ping" in ping["embeds"][0]["title"])
check("quotes the live threshold",
      "$45.00 and up" in json.dumps(ping), json.dumps(ping["embeds"][0]["fields"]))

print("\n== Discord payload ==")
payload = em.build_discord_payload(matches)
check("one message carries all 3 matches as embeds", len(payload["embeds"]) == 3)
check("embeds are capped at Discord's limit of 10",
      len(em.build_discord_payload(matches * 5)["embeds"]) == 10)
e = payload["embeds"][0]
check("embed has title/url/thumbnail/fields",
      all(k in e for k in ("title", "url", "thumbnail", "fields", "color", "footer")))
check("title within Discord's 256-char limit", len(e["title"]) <= 256)
check("every field value within 1024 chars", all(len(f["value"]) <= 1024 for f in e["fields"]))
check("no empty fields", all(f["value"].strip() for f in e["fields"]))
check("payload is JSON-serialisable", isinstance(json.dumps(payload), str))
check("embed url points at the item", e["url"].startswith("https://www.ebay.com/itm/"))

long_title = em.Listing(item_id="1", title="x" * 400, url="https://www.ebay.com/itm/1")
check("over-long title truncated to 256", len(em.build_embed(long_title)["title"]) == 256)
check("embed with no optional data still valid",
      em.build_embed(long_title)["fields"] == [])

print("\n== .env loading (keeps the secret out of the launchd plist) ==")
with tempfile.TemporaryDirectory() as tmp:
    envfile = Path(tmp) / ".env"
    envfile.write_text(
        '# a comment\n'
        'DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/1/from-env-file"\n'
        "export EBAY_PRICE_THRESHOLD=99\n"
        "EBAY_ALREADY_SET=from-file\n"
        "malformed line with no equals\n"
        "\n",
        "utf-8",
    )
    os.environ.pop("DISCORD_WEBHOOK_URL", None)
    os.environ["EBAY_ALREADY_SET"] = "from-real-env"
    em._load_env_file(envfile)

    check("value is read from .env",
          os.environ.get("DISCORD_WEBHOOK_URL") == "https://discord.com/api/webhooks/1/from-env-file",
          os.environ.get("DISCORD_WEBHOOK_URL", ""))
    check("surrounding quotes are stripped",
          not os.environ["DISCORD_WEBHOOK_URL"].startswith('"'))
    check("'export KEY=value' form works", os.environ.get("EBAY_PRICE_THRESHOLD") == "99")
    check("a real environment variable still wins over the file",
          os.environ["EBAY_ALREADY_SET"] == "from-real-env")
    check("comments and malformed lines are skipped without raising", True)
    for k in ("DISCORD_WEBHOOK_URL", "EBAY_PRICE_THRESHOLD", "EBAY_ALREADY_SET"):
        os.environ.pop(k, None)

check("a missing .env is not an error",
      em._load_env_file(Path("/nonexistent/.env")) is None)

print("\n== Fetching: warm-up and re-warm-on-block ==")


class FakeResponse:
    def __init__(self, status, text="", url=URL_OK):
        self.status_code = status
        self.text = text
        self.url = url
        self.headers = {}


class FakeJar(dict):
    def clear(self):
        super().clear()


class FakeClient:
    """Scripted eBay. Records every call so we can assert on the sequence."""

    def __init__(self, script):
        self.script = list(script)   # list of (match, FakeResponse)
        self.calls = []
        self.cookies = FakeJar()

    def get(self, url, **kw):
        kind = "home" if url.rstrip("/").endswith("ebay.com") else "search"
        self.calls.append(kind)
        for i, (match, resp) in enumerate(self.script):
            if match == kind:
                self.script.pop(i)
                if kind == "home":
                    self.cookies["bm_so"] = "x"
                    self.cookies["bm_s"] = "y"
                return resp
        raise AssertionError(f"FakeClient got an unscripted {kind} request")


def make_fetcher(script):
    f = em.Fetcher.__new__(em.Fetcher)      # bypass __init__'s real client build
    f.kind = "requests"
    f.warmed_at = None
    f.warm_count = 0
    f._client = FakeClient(script)
    return f


GOOD_HTML = SLAMMED
BLOCK_HTML = "<html><head><title>Error Page | eBay</title></head><body>SORRY</body></html>"

# 1. The documented reality: warm-up 403s, search then succeeds.
f = make_fetcher([("home", FakeResponse(403, BLOCK_HTML)),
                  ("search", FakeResponse(200, GOOD_HTML))])
try:
    html = em.fetch_search_html(f, URL_OK)
    check("a 403 warm-up is NOT fatal -- the search still runs and succeeds",
          "ROMAN REIGNS" in html)
except Exception as exc:
    check("a 403 warm-up is NOT fatal -- the search still runs and succeeds", False,
          f"{type(exc).__name__}: {exc}")
check("warm-up ran before the search", f._client.calls == ["home", "search"],
      str(f._client.calls))
check("cookies were collected despite the 403", "bm_so" in f._client.cookies)
check("session is marked warm", f.is_warm())

# 2. Stale cookies mid-run: 403 on the search, then recover.
f = make_fetcher([("home", FakeResponse(403)), ("search", FakeResponse(403)),
                  ("home", FakeResponse(403)), ("search", FakeResponse(200, GOOD_HTML))])
try:
    html = em.fetch_search_html(f, URL_OK)
    check("a blocked search triggers re-warm + retry, and recovers",
          "ROMAN REIGNS" in html)
except Exception as exc:
    check("a blocked search triggers re-warm + retry, and recovers", False,
          f"{type(exc).__name__}: {exc}")
check("the retry sequence is home,search,home,search",
      f._client.calls == ["home", "search", "home", "search"], str(f._client.calls))
check("it re-warmed exactly twice", f.warm_count == 2, str(f.warm_count))

# 3. The silent soft block -- 200 with no results markup.
f = make_fetcher([("home", FakeResponse(403)),
                  ("search", FakeResponse(200, "<html><body>nothing here</body></html>")),
                  ("home", FakeResponse(403)),
                  ("search", FakeResponse(200, GOOD_HTML))])
try:
    html = em.fetch_search_html(f, URL_OK)
    check("HTTP 200 with no results markup is caught and retried (the curl_cffi case)",
          "ROMAN REIGNS" in html)
except Exception as exc:
    check("HTTP 200 with no results markup is caught and retried (the curl_cffi case)",
          False, f"{type(exc).__name__}: {exc}")

# 4. Genuinely blocked: give up after one retry rather than looping.
f = make_fetcher([("home", FakeResponse(403)), ("search", FakeResponse(403)),
                  ("home", FakeResponse(403)), ("search", FakeResponse(403))])
try:
    em.fetch_search_html(f, URL_OK)
    check("a persistent block raises SoftBlockError after ONE retry", False,
          "no exception")
except em.SoftBlockError as exc:
    check("a persistent block raises SoftBlockError after ONE retry",
          "re-warm" in str(exc), str(exc)[:70])
check("it did not retry more than once",
      f._client.calls == ["home", "search", "home", "search"], str(f._client.calls))

# 5. Warm-up is reused between polls, but expires.
f = make_fetcher([("home", FakeResponse(403)), ("search", FakeResponse(200, GOOD_HTML)),
                  ("search", FakeResponse(200, GOOD_HTML))])
em.fetch_search_html(f, URL_OK)
em.fetch_search_html(f, URL_OK)
check("a second poll reuses the warm session (no second homepage hit)",
      f._client.calls == ["home", "search", "search"], str(f._client.calls))
f.warmed_at -= em.WARM_MAX_AGE + 1
check("the warm-up is considered stale once WARM_MAX_AGE passes", not f.is_warm())

# 6. reset() really does drop the jar.
f = make_fetcher([("home", FakeResponse(403)), ("search", FakeResponse(200, GOOD_HTML))])
em.fetch_search_html(f, URL_OK)
check("cookies present before reset", len(f._client.cookies) > 0)
f.reset()
check("reset clears the cookie jar", len(f._client.cookies) == 0)
check("reset forces the next warm-up", not f.is_warm())

print("\n== Client selection ==")
check("auto prefers requests (the measured winner), not curl_cffi",
      em.Fetcher("auto").kind == "requests")
check("curl_cffi is last in auto order -- it soft-blocks silently here",
      em.HTTP_CLIENT == "auto")

print("\n== Seen-state persistence ==")
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "seen_items.json"
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=90)).isoformat()
    state = {"version": 1, "seen": {"aaa": now.isoformat(), "bbb": old}}
    em.save_state(path, state)
    reloaded = em.load_state(path)
    check("state survives a save/load round trip", "aaa" in reloaded["seen"])
    check("entries older than the retention window are pruned", "bbb" not in reloaded["seen"])

    path.write_text("{ this is not json", "utf-8")
    recovered = em.load_state(path)
    check("corrupt state file recovers instead of crashing", recovered["seen"] == {})
    check("corrupt state file is backed up",
          (Path(tmp) / "seen_items.json.corrupt").exists())

    legacy = Path(tmp) / "legacy.json"
    legacy.write_text(json.dumps(["111", "222"]), "utf-8")
    check("bare list of IDs is tolerated", set(em.load_state(legacy)["seen"]) == {"111", "222"})

print("\n== No re-alerting across restarts ==")


class FakeSession:
    """Stands in for requests.Session; records what would have been POSTed."""
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)

        class R:
            status_code = 204
            text = ""
        return R()


with tempfile.TemporaryDirectory() as tmp:
    args = em.parse_args([
        "--once", "--alert-existing",
        "--webhook", "https://discord.com/api/webhooks/1/test",
        "--state-file", str(Path(tmp) / "seen.json"),
    ])
    session = FakeSession()
    state = em.load_state(args.state_file)

    sent1 = em.run_cycle(session, state, args, html_override=PRICEY)
    check("first poll alerts on the 3 qualifying listings", sent1 == 3, str(sent1))
    check("a single webhook POST carries all 3 embeds",
          len(session.posts) == 1 and len(session.posts[0]["embeds"]) == 3)

    state2 = em.load_state(args.state_file)
    check("all 4 listings recorded as seen, not just the 3 alerted",
          len(state2["seen"]) == 4, str(len(state2["seen"])))

    session2 = FakeSession()
    sent2 = em.run_cycle(session2, state2, args, html_override=PRICEY)
    check("restart with the same page sends nothing", sent2 == 0 and not session2.posts)

    new_html = PRICEY.replace("168695146965", "999888777666").replace("$2,150.00", "$45.00")
    session3 = FakeSession()
    sent3 = em.run_cycle(session3, em.load_state(args.state_file), args, html_override=new_html)
    check("a genuinely new listing at exactly $45.00 does alert", sent3 == 1, str(sent3))

print("\n== Soft-block / bad-page handling ==")
for label, html in [
    ("eBay error page", "<html><head><title>Error Page | eBay</title></head>"
                        "<body>Something went wrong on our end</body></html>"),
    ("empty page", "<html><body></body></html>"),
    ("captcha wall", "<html><title>Security Measure</title><body>Please verify you are "
                     "not a robot</body></html>"),
]:
    try:
        em.parse_listings(html)
        check(f"{label} raises SoftBlockError", False, "no exception raised")
    except em.SoftBlockError:
        check(f"{label} raises SoftBlockError", True)
    except Exception as exc:
        check(f"{label} raises SoftBlockError", False, f"raised {type(exc).__name__}")

print("\n== Malformed cards don't sink the poll ==")
broken = ('<ul><li class="s-card"></li>'
          '<li class="s-card"><a class="s-card__link" href="/itm/555000111222">'
          '<div class="s-card__title"><span>A real one</span></div></a>'
          '<span class="s-card__price">$99.00</span></li></ul>')
survivors = em.parse_listings(broken)
check("empty card skipped, good card kept", len(survivors) == 1, str(len(survivors)))
check("id recovered from href when data-listingid is missing",
      survivors[0].item_id == "555000111222", survivors[0].item_id)
check("card with no price parses without raising", survivors[0].price == 99.0)

print(f"\n{passed} passed, {failed} failed\n")
sys.exit(1 if failed else 0)
