#!/usr/bin/env python3
"""
eBay new-listing monitor -> Discord webhook.

Watches an eBay search (sorted by "newly listed"), and posts a Discord embed for
every NEW listing at or above a price threshold, with no upper limit.
No eBay account, no login, no API keys:
it just reads the public search results page the same way a browser would.

Quick start:
    pip install -r requirements.txt
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    python ebay_monitor.py

Useful flags:
    --once              run a single poll and exit
    --dry-run           never POST to Discord; print the JSON that would be sent
    --no-filter         ignore the price threshold (show everything parsed)
    --alert-existing    on the very first run, alert on current listings too
    --parse-file FILE   parse a saved HTML file instead of hitting the network
    --dump-html FILE    save the fetched HTML (handy when eBay changes markup)

See README.md for setup, including how to create the Discord webhook.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote_plus, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency 'requests'. Run: pip install -r requirements.txt")

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency 'beautifulsoup4'. Run: pip install -r requirements.txt")


def _load_env_file(path: Path) -> None:
    """Read a simple KEY=VALUE .env sitting next to this script.

    This exists so the launchd agent never needs the webhook written into its
    plist: the secret lives in exactly one file, .env, chmod 600. Real environment
    variables always win, so `DISCORD_WEBHOOK_URL=... python ebay_monitor.py`
    still overrides the file.
    """
    try:
        text = path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(Path(__file__).resolve().parent / ".env")


# ---------------------------------------------------------------------------
# CONFIG  -- edit here, or override any of these with environment variables
# ---------------------------------------------------------------------------

# The eBay search to watch. Keep &_sop=10 on it -- that's "newly listed" order.
#
# Two things in this URL are load-bearing, both learned the hard way:
#
#   -slammer   eBay spell-"corrects" slammed -> slammer and silently returns 2019
#              Slam Attax singles instead of the 2026 Topps WWE Universe SLAMMED
#              insert set. Excluding the word slammer defeats the autocorrect;
#              the results heading then reads "...for wwe universe slammed".
#              check_autocorrect() below warns loudly if this ever regresses.
#
#   no _sacat  The category filter (261328, Trading Card Singles) drops real
#              listings -- it hid a $2,000 Undertaker SLAMMED insert that was
#              filed under a different category. Searching all categories costs
#              nothing here: the query is precise enough on its own.
#   -digital   the Topps *Slam* phone app resells as "DIGITAL ... 10cc/50cc"
#              listings for a few dollars. Verified empirically on 2026-09-19:
#              adding -digital returned the identical 9 item IDs, so it costs
#              nothing in recall and pre-empts cheap noise once auctions are
#              allowed to alert below the price floor.
SEARCH_URL = os.getenv(
    "EBAY_SEARCH_URL",
    "https://www.ebay.com/sch/i.html?_nkw=wwe+universe+slammed+-slammer+-digital&_sop=10",
)

# A live auction's CURRENT BID is not its value. A CASE HIT SSP sitting at $41
# with two bids is exactly the thing worth knowing about, and by the time it
# climbs past the floor on its own it is too late to act. So auctions are judged
# on the title's value signals instead of the current bid; Buy It Now keeps the
# floor, which is what suppresses cheap tat.
VALUE_SIGNAL_RE = re.compile(
    r"""(?xi)
    \bSSP\b | \bCASE\s*HIT\b | \bSUPER\s*SHORT\s*PRINT\b
    | \b(?:ULTRA|SUPER)\s+RARE\b
    | \bSHORT\s*PRINT\b | (?<![A-Z])SP(?![A-Z])
    | \b1\s*[/of]{1,2}\s*1\b                 # 1/1, 1 of 1
    | /\s?(?:\d{1,3})\b                      # /99, /25, numbered parallels
    | \bAUTO(?:GRAPH)?\b | \bPATCH\b | \bRELIC\b
    """
)

# Never alert on these however cheap or expensive -- belt and braces behind the
# -digital search term, because the auction rule deliberately drops the floor.
DIGITAL_RE = re.compile(r"(?i)\bdigital\b|\*digital\*|\b\d+\s?cc\b|\btopps\s+slam\b")

# A price DROP this large (absolute or percent) is worth a second alert. Rises
# are not alerted -- auctions rise by design and would ping on every bid -- except
# for the single "crossed your floor" event below.
PRICE_DROP_MIN_ABS = float(os.getenv("EBAY_PRICE_DROP_MIN_ABS", "25"))
PRICE_DROP_MIN_PCT = float(os.getenv("EBAY_PRICE_DROP_MIN_PCT", "10"))

# Alert on listings priced AT OR ABOVE this (in the listing's own currency).
# $45.00 exactly qualifies. There is deliberately no upper bound -- however
# expensive a listing is, you'll hear about it.
PRICE_THRESHOLD = float(os.getenv("EBAY_PRICE_THRESHOLD", "45"))

# Seconds between polls. 300 = 5 minutes. Don't go below ~120; be a good citizen.
POLL_INTERVAL = int(os.getenv("EBAY_POLL_INTERVAL", "300"))

# >>> THE ONE THING YOU STILL NEED TO FILL IN <<<
# Paste your Discord incoming-webhook URL here, or (better) set the env var
# DISCORD_WEBHOOK_URL. README.md section "Create the Discord webhook" explains how.
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE")

# Where the "already alerted" item IDs live, so restarts don't re-spam you.
STATE_FILE = Path(os.getenv("EBAY_STATE_FILE", "seen_items.json"))

# On the very first run (no state file yet) we record what's currently listed
# WITHOUT alerting, so you don't get 60 messages at once. Set to "1" to override.
ALERT_ON_FIRST_RUN = os.getenv("EBAY_ALERT_ON_FIRST_RUN", "0") == "1"

# Safety valve: never fire more than this many alerts in one poll.
MAX_ALERTS_PER_CYCLE = int(os.getenv("EBAY_MAX_ALERTS_PER_CYCLE", "10"))

# After this many consecutive failed polls, post ONE warning to Discord. Silence is
# indistinguishable from "nothing new", so a monitor that quietly stops working is
# the real danger. Set to 0 to disable. A recovery message follows when it clears.
#
# The count is PERSISTED in the state file, not held in memory. That matters: under
# `--once` (cron, GitHub Actions) every poll is a fresh process, so an in-memory
# counter could never exceed 1 -- and setting this to 2 or 3 would have silently
# disabled health alerts altogether rather than making them less twitchy.
ALERT_AFTER_FAILURES = int(os.getenv("EBAY_ALERT_AFTER_FAILURES", "3"))

# Forget seen IDs older than this many days (keeps the state file small).
STATE_RETENTION_DAYS = int(os.getenv("EBAY_STATE_RETENTION_DAYS", "45"))

REQUEST_TIMEOUT = int(os.getenv("EBAY_REQUEST_TIMEOUT", "30"))
LOG_LEVEL = os.getenv("EBAY_LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("EBAY_LOG_FILE", "")  # empty = stdout only

# Must stay consistent with SEC_CH_UA below -- a Chrome 148 UA paired with
# Chrome 131 client hints is a louder bot signal than either alone.
USER_AGENT = os.getenv(
    "EBAY_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
)
SEC_CH_UA = os.getenv("EBAY_SEC_CH_UA", '"Not/A)Brand";v="99", "Chromium";v="148"')
SEC_CH_UA_PLATFORM = os.getenv("EBAY_SEC_CH_UA_PLATFORM", '"macOS"')

# Which HTTP client talks to eBay: auto | requests | httpx | curl_cffi.
# "auto" tries requests first because that is what deploy/diagnose.py found to
# work on this machine. See the Fetcher class.
HTTP_CLIENT = os.getenv("EBAY_HTTP_CLIENT", "auto")

# eBay's bot-management cookies don't live forever. Re-warm the session at least
# this often (seconds) so a long-running process never drifts onto a stale jar.
WARM_MAX_AGE = int(os.getenv("EBAY_WARM_MAX_AGE", "1800"))  # 30 minutes

# Pause between the warm-up request and the search, so it looks like a person
# landing on the homepage and then searching rather than two instant hits.
WARM_PAUSE = float(os.getenv("EBAY_WARM_PAUSE", "1.5"))

# Pause before escalating to a different HTTP client after a block. Short-lived
# IP-reputation blocks often clear on their own, so the wait is doing real work.
RETRY_PAUSE = float(os.getenv("EBAY_RETRY_PAUSE", "20"))

DISCORD_USERNAME = os.getenv("DISCORD_USERNAME", "eBay Watch")
EMBED_COLOR = int(os.getenv("DISCORD_EMBED_COLOR", "0x0064D2"), 0)  # eBay blue

PLACEHOLDER_WEBHOOKS = {"", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE", "CHANGEME"}

# ---------------------------------------------------------------------------

log = logging.getLogger("ebay-monitor")

# eBay pads the results grid with a sponsored "Shop on eBay" placeholder card
# that always points at /itm/123456. Never alert on it.
PLACEHOLDER_IDS = {"123456", "2500219655424533"}
PLACEHOLDER_TITLES = {"shop on ebay"}

CONDITION_WORDS = {
    "brand new", "new", "new (other)", "new with tags", "new without tags",
    "new with box", "new without box", "new with defects", "pre-owned",
    "pre-owned - good", "pre-owned - excellent", "used", "open box",
    "certified - refurbished", "excellent - refurbished", "very good - refurbished",
    "good - refurbished", "seller refurbished", "refurbished",
    "for parts or not working", "graded", "ungraded", "like new",
    "very good", "good", "acceptable",
}

PRICE_RE = re.compile(
    r"(?P<cur>[A-Z]{1,3}\s?\$|[$£€¥]|AU\s?\$|C\s?\$|US\s?\$)?\s*"
    r"(?P<num>\d[\d,]*(?:\.\d{1,2})?)"
)


class SoftBlockError(RuntimeError):
    """eBay served us a page with no results -- throttling, captcha, or a redesign."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Listing:
    item_id: str
    title: str
    url: str
    price_text: str = ""
    price: float | None = None
    price_high: float | None = None
    currency: str = ""
    image: str = ""
    condition: str = ""
    seller: str = ""
    seller_feedback: str = ""
    buying_format: str = ""
    shipping: str = ""
    location: str = ""
    listed_at: str = ""
    is_new_listing: bool = False
    extras: list[str] = field(default_factory=list)
    is_auction: bool = False
    bid_count: int | None = None

    def is_digital(self) -> bool:
        return bool(DIGITAL_RE.search(self.title))

    def value_signal(self) -> str:
        m = VALUE_SIGNAL_RE.search(self.title)
        return m.group(0).strip() if m else ""

    def summary(self) -> str:
        bits = [self.price_text or "?", self.title[:70]]
        if self.condition:
            bits.append(f"({self.condition})")
        return "  ".join(bits)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _pick_parser() -> str:
    try:
        import lxml  # noqa: F401
        return "lxml"
    except ImportError:
        return "html.parser"


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


def parse_price(raw: str) -> tuple[float | None, float | None, str]:
    """'$29.57' -> (29.57, 29.57, '$');  '$5.99 to $12.99' -> (5.99, 12.99, '$')."""
    if not raw:
        return None, None, ""
    matches = list(PRICE_RE.finditer(raw.replace("\xa0", " ")))
    values, currency = [], ""
    for m in matches:
        try:
            values.append(float(m.group("num").replace(",", "")))
        except ValueError:
            continue
        if not currency and m.group("cur"):
            currency = m.group("cur").strip()
    if not values:
        return None, None, currency
    return min(values), max(values), currency


def _item_id_from_url(url: str) -> str:
    m = re.search(r"/itm/(?:[^/?]*/)?(\d{6,})", url or "")
    return m.group(1) if m else ""


def _clean_item_url(item_id: str, raw_url: str) -> str:
    if item_id:
        return f"https://www.ebay.com/itm/{item_id}"
    return (raw_url or "").split("?")[0]


def _upgrade_image(url: str) -> str:
    """eBay thumbs come in s-l140; bump to s-l500 so the Discord embed isn't tiny."""
    return re.sub(r"/s-l\d+\.(jpg|jpeg|png|webp)", r"/s-l500.\1", url or "")


def _parse_card(card) -> Listing | None:
    link = card.select_one("a.s-card__link[href], a.s-item__link[href]")
    raw_url = link.get("href", "") if link else ""
    item_id = (card.get("data-listingid") or "").strip() or _item_id_from_url(raw_url)
    if not item_id:
        return None

    title_node = card.select_one(".s-card__title, .s-item__title")
    # Note the flag before stripping it -- it lives inside the title node.
    is_new = bool(card.select_one(".s-card__new-listing, .LIGHT_HIGHLIGHT"))
    if title_node:
        # Drop eBay's decorations: the "New Listing" flag and the screen-reader
        # "Opens in a new window or tab" span, both of which live inside the title.
        for junk in title_node.select(".clipped, .s-card__new-listing, .LIGHT_HIGHLIGHT"):
            junk.extract()
    title = _text(title_node)

    if not title or title.lower() in PLACEHOLDER_TITLES:
        return None
    if item_id in PLACEHOLDER_IDS or _item_id_from_url(raw_url) in PLACEHOLDER_IDS:
        return None

    price_text = _text(card.select_one(".s-card__price, .s-item__price"))
    low, high, currency = parse_price(price_text)

    img = card.select_one("img.s-card__image, .s-item__image-wrapper img, img")
    image = _upgrade_image(img.get("src") or img.get("data-src") or "") if img else ""

    # Subtitles carry "Pre-Owned", "Brand New", consignment notes, etc.
    subtitles = [_text(s) for s in card.select(".s-card__subtitle, .s-item__subtitle")]
    condition = next((s for s in subtitles if s.lower() in CONDITION_WORDS), "")
    if not condition:
        condition = next(
            (s for s in subtitles if re.search(r"\b(new|pre-owned|used|refurb|graded)\b", s, re.I)),
            "",
        )

    rows = [_text(r) for r in card.select(".s-card__attribute-row, .s-item__detail")]
    rows = [r for r in rows if r and r != price_text]

    buying_format = next(
        (r for r in rows if re.search(r"^(buy it now|or best offer|\d+ bids?|bids?)", r, re.I)), ""
    )
    shipping = next((r for r in rows if re.search(r"(delivery|shipping|postage)", r, re.I)), "")
    location = next((r for r in rows if r.lower().startswith("located in")), "")
    listed_at = next(
        (r for r in rows if re.match(r"^[A-Z][a-z]{2}-\d{1,2}\s+\d{1,2}:\d{2}$", r)), ""
    )

    seller, feedback = "", ""
    for row in card.select(".s-card__attribute-row"):
        spans = [_text(s) for s in row.find_all("span", recursive=False)] or [
            _text(s) for s in row.find_all("span")
        ]
        hit = next((s for s in spans if "% positive" in s), "")
        if hit:
            feedback = hit
            seller = next((s for s in spans if s and s != hit), "")
            break
    if not seller:
        m = re.search(r"([\w.\-*]+)\s*\(?\s*[\d,.]+\s*\)?\s*[\d.]+%", " ".join(rows))
        if m:
            seller = m.group(1)

    bid_m = re.match(r"^(\d+)\s+bids?\b", buying_format or "", re.I)
    is_auction = bool(bid_m)
    bid_count = int(bid_m.group(1)) if bid_m else None

    used = {buying_format, shipping, location, listed_at, feedback, seller}
    extras = [r for r in rows if r not in used and "% positive" not in r][:3]

    return Listing(
        item_id=item_id,
        title=title,
        url=_clean_item_url(item_id, raw_url),
        price_text=price_text,
        price=low,
        price_high=high,
        currency=currency,
        image=image,
        condition=condition,
        seller=seller,
        seller_feedback=feedback,
        buying_format=buying_format,
        shipping=shipping,
        location=location,
        listed_at=listed_at,
        is_new_listing=is_new,
        extras=extras,
        is_auction=is_auction,
        bid_count=bid_count,
    )


HEADING_SELECTOR = '.srp-controls__count-heading, [class*="count-heading"]'
HEADING_RE = re.compile(r"results?\s+for\s+(.+)$", re.I | re.S)


def _requested_keywords(url: str) -> str:
    return unquote_plus(parse_qs(urlparse(url).query).get("_nkw", [""])[0]).strip()


def _normalise_kw(text: str) -> str:
    """Loose comparison: case, punctuation and spacing shouldn't count as a mismatch."""
    return " ".join(re.sub(r"[^\w\s\-]", " ", text.lower()).split())


def check_autocorrect(html: str, url: str) -> str | None:
    """Return what eBay actually searched for, if it isn't what we asked for.

    eBay silently rewrites misspelled-looking queries ("slammed" -> "slammer") and
    serves a completely different result set, with HTTP 200 and a perfectly normal
    looking page. Nothing else in this script can tell the difference, so the only
    signal is the results heading: "5 results for wwe universe slammed".

    This is a real failure mode, not a hypothetical: it's how this monitor spent
    its first day watching 2019 Slam Attax singles instead of the 2026 SLAMMED set.
    """
    requested = _requested_keywords(url)
    if not requested:
        return None

    soup = BeautifulSoup(html, _pick_parser())
    heading = _text(soup.select_one(HEADING_SELECTOR))
    if not heading:
        return None
    heading = re.split(r"save this search", heading, flags=re.I)[0]

    m = HEADING_RE.search(heading)
    if not m:
        return None

    actual = m.group(1).strip().strip('"').strip()
    if actual and _normalise_kw(actual) != _normalise_kw(requested):
        return actual
    return None


def parse_listings(html: str) -> list[Listing]:
    """Pull listings out of an eBay search-results page.

    Handles both the current markup (li.s-card / .su-card-container) and the
    older li.s-item grid, so this keeps working if eBay rolls either one out.
    """
    soup = BeautifulSoup(html, _pick_parser())
    cards = soup.select("li.s-card") or soup.select("li.s-item") or soup.select(".s-card")

    if not cards:
        page_title = _text(soup.title)
        if re.search(r"(error page|something went wrong|robot|captcha|access denied)",
                     page_title + " " + html[:4000], re.I):
            raise SoftBlockError(f"eBay returned a block/error page (title={page_title!r})")
        raise SoftBlockError("No listing cards found -- eBay markup may have changed")

    listings: list[Listing] = []
    seen_ids: set[str] = set()
    for card in cards:
        try:
            listing = _parse_card(card)
        except Exception:  # one bad card must never sink the whole poll
            log.debug("Failed to parse a result card", exc_info=True)
            continue
        if listing and listing.item_id not in seen_ids:
            seen_ids.add(listing.item_id)
            listings.append(listing)
    return listings


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def build_session() -> requests.Session:
    """The Discord client. Plain requests is fine here -- Discord isn't gatekeeping us."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "ebay-listing-monitor (+https://github.com/)",
        "Content-Type": "application/json",
    })
    return s


def _accept_encoding() -> str:
    """Only advertise encodings we can actually decode."""
    encs = ["gzip", "deflate"]
    for mod in ("brotli", "brotlicffi"):
        try:
            __import__(mod)
            encs.append("br")
            break
        except ImportError:
            pass
    try:
        __import__("zstandard")
        encs.append("zstd")
    except ImportError:
        pass
    return ", ".join(encs)


def chrome_headers(navigation: bool = True) -> dict[str, str]:
    """The header set real Chrome sends for a top-level navigation.

    Captured from the browser that loads this search fine on the same machine and
    IP that got a 403 from the script. Order is deliberate -- bot-management
    products compare header order as well as values. Claiming a Chrome UA while
    omitting the sec-ch-ua client hints is itself a giveaway, which is what the
    original header set did.
    """
    return {
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": USER_AGENT,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                   "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        "Sec-Fetch-Site": "none" if navigation else "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Accept-Encoding": _accept_encoding(),
        "Accept-Language": "en-US,en;q=0.9",
        "Priority": "u=0, i",
    }


class Fetcher:
    """Wraps whichever HTTP client we're using to talk to eBay.

    Separate from the Discord client on purpose: Discord is a plain JSON API that
    never blocks us, while eBay sits behind Akamai Bot Manager.

    What actually got us through (measured, not guessed -- deploy/diagnose.py):

        requests + Chrome 148 headers, no cookies   -> 403
        requests + Chrome headers + homepage warm-up -> 200, full results  <-- winner
        httpx HTTP/2 + warm-up                       -> 200, full results
        curl_cffi impersonate=chrome                 -> 200 but 13 KB and ZERO
                                                        listings: a silent soft block

    So the cookies are the thing, not the TLS fingerprint. curl_cffi is ordered
    LAST under "auto" precisely because it fails silently here -- it returns a
    plausible 200 that parses to nothing, which is the worst possible failure for
    a monitor whose job is to stay quiet.

      auto       requests -> httpx(h2) -> curl_cffi (best-known-good first)
      requests   HTTP/1.1, Python TLS fingerprint  (the winner on this machine)
      httpx      HTTP/2, also works here
      curl_cffi  Chrome JA3 -- soft-blocked here; kept for when eBay changes
    """

    HOME = "https://www.ebay.com/"

    def __init__(self, kind: str = "auto"):
        self.kind = kind
        self.warmed_at: float | None = None
        self.warm_count = 0
        self._client = None
        self._build()

    def _build(self) -> None:
        order = ["requests", "httpx", "curl_cffi"] if self.kind == "auto" else [self.kind]
        for kind in order:
            try:
                if kind == "curl_cffi":
                    from curl_cffi import requests as cffi
                    self._client = cffi.Session(impersonate="chrome")
                    self.kind = "curl_cffi"
                    return
                if kind == "httpx":
                    import httpx
                    self._client = httpx.Client(http2=True, headers=chrome_headers(),
                                                timeout=REQUEST_TIMEOUT, follow_redirects=True)
                    self.kind = "httpx"
                    return
                if kind == "requests":
                    s = requests.Session()
                    s.headers.update(chrome_headers())
                    self._client = s
                    self.kind = "requests"
                    return
            except ImportError:
                continue
            except Exception as exc:
                log.warning("Could not initialise %s client (%s); trying the next one", kind, exc)
        raise RuntimeError(f"No usable HTTP client for kind={self.kind!r}")

    def _raw_get(self, url: str, headers: dict[str, str]):
        if self.kind == "httpx":
            return self._client.get(url, headers=headers)
        return self._client.get(url, timeout=REQUEST_TIMEOUT, headers=headers,
                                allow_redirects=True)

    def cookie_names(self) -> list[str]:
        """Cookie jars differ by client: requests yields Cookie objects, httpx and
        curl_cffi behave more like dicts. Never let this raise -- it is only used
        for logging, and an exception here used to look like a network failure."""
        jar = getattr(self._client, "cookies", None)
        if jar is None:
            return []
        try:
            return sorted(c.name for c in jar)           # requests
        except (TypeError, AttributeError):
            pass
        try:
            return sorted(jar.keys())                    # httpx / curl_cffi / dict
        except Exception:
            return []

    def reset(self) -> None:
        """Throw away the cookie jar and force the next warm-up."""
        jar = getattr(self._client, "cookies", None)
        try:
            if jar is not None:
                jar.clear()
        except Exception as exc:
            log.debug("Could not clear cookies (%s); rebuilding the client instead", exc)
            try:
                self._build()
            except Exception:
                pass
        self.warmed_at = None

    def is_warm(self) -> bool:
        return (self.warmed_at is not None
                and (time.monotonic() - self.warmed_at) < WARM_MAX_AGE)

    def warm_up(self, force: bool = False) -> None:
        """Hit the homepage first so eBay hands us its bot-management cookies.

        IMPORTANT: the warm-up itself is EXPECTED to fail. On the machine this was
        built for it returns 403 and only two cookies (bm_s, bm_so) -- and the
        search that follows then succeeds with a full 515 KB of results. The
        cookies are what matter, not the status code, so a non-200 here is not an
        error and must never abort the poll. Treating it as fatal would break the
        one configuration known to work.
        """
        if not force and self.is_warm():
            return

        # Only the HTTP call is guarded. Anything after it is our own code, and
        # swallowing a bug there as if it were a network failure hides real faults.
        try:
            resp = self._raw_get(self.HOME, chrome_headers(navigation=True))
        except Exception as exc:
            # A transport failure is different from a 403: leave the session cold
            # so the next poll tries again rather than running on no cookies.
            log.debug("Warm-up request could not be made (%s); continuing uncooked", exc)
            return

        names = self.cookie_names()
        log.debug("Warm-up: HTTP %s, %d cookies (%s)%s",
                  resp.status_code, len(names), ", ".join(names[:6]),
                  "  <- non-200 is normal and fine" if resp.status_code != 200 else "")
        self.warmed_at = time.monotonic()
        self.warm_count += 1
        if WARM_PAUSE:
            time.sleep(WARM_PAUSE)

    def get(self, url: str):
        headers = chrome_headers(navigation=False)
        headers["Referer"] = self.HOME
        return self._raw_get(url, headers)


def _validate_search_response(resp, fetcher: "Fetcher", url: str) -> str:
    """Decide whether this response is really the search results.

    Status codes are not enough. curl_cffi came back 200 with 13 KB of HTML and
    zero listings -- a textbook silent soft block. So the content is checked too.
    """
    status = resp.status_code
    final_url = str(getattr(resp, "url", url))

    if status == 429:
        raise SoftBlockError(
            f"HTTP 429 rate limited (Retry-After={resp.headers.get('Retry-After')})")
    if status in (403, 503):
        raise SoftBlockError(
            f"HTTP {status} via the {fetcher.kind} client -- eBay is blocking this "
            f"request signature. Run deploy/diagnose.py to find one that works.")
    if status >= 400:
        raise SoftBlockError(f"HTTP {status} from eBay")

    # eBay bounces bot-ish requests to the homepage / category index instead of 4xx-ing.
    if "/sch/" not in final_url and "/b/" not in final_url:
        raise SoftBlockError(f"Redirected away from search results to {final_url}")

    html = resp.text
    # A real results page has result cards, or at least the "N results for ..."
    # heading when the search genuinely matches nothing.
    if ("s-card" not in html and "s-item" not in html
            and "count-heading" not in html):
        raise SoftBlockError(
            f"HTTP 200 but no results markup in {len(html):,} bytes -- "
            f"silent soft block from the {fetcher.kind} client")

    return html


def _client_escalation(current: str) -> list[str]:
    """Other clients to try, in order, after the current one gets blocked."""
    return [k for k in ("requests", "httpx", "curl_cffi") if k != current]


def fetch_search_html(fetcher: "Fetcher", url: str) -> str:
    """GET the search page, escalating through recovery attempts before giving up.

    Observed behaviour: a 403 here is usually the *runner's IP* being in poor
    standing with Akamai at that moment, not a durable block. GitHub hands out a
    different IP per run, and the identical technique that 403s on one IP returns
    a full results page on another minutes later. So a single 403 is not evidence
    that anything is broken, and bailing out on it throws away a poll for nothing.

    Escalation, cheapest first:
      1. the warm session we already have
      2. same client, cookie jar binned and re-warmed  (fixes stale cookies)
      3. a different HTTP client, after a pause         (different TLS/HTTP stack,
         and the pause alone often outlasts a short-lived block)

    Only when all of those fail do we call it a soft block.
    """
    fetcher.warm_up()
    try:
        return _validate_search_response(fetcher.get(url), fetcher, url)
    except SoftBlockError as first:
        log.warning("Attempt 1 blocked (%s)", first)
        last = first

    log.info("Attempt 2: dropping cookies and re-warming the %s client", fetcher.kind)
    fetcher.reset()
    fetcher.warm_up(force=True)
    try:
        html = _validate_search_response(fetcher.get(url), fetcher, url)
        log.info("Recovered on attempt 2 -- the cookie jar had gone stale")
        return html
    except SoftBlockError as second:
        log.warning("Attempt 2 blocked (%s)", second)
        last = second

    for n, kind in enumerate(_client_escalation(fetcher.kind), start=3):
        log.info("Waiting %ds, then attempt %d with the %s client", RETRY_PAUSE, n, kind)
        time.sleep(RETRY_PAUSE)
        try:
            alt = Fetcher(kind)
        except Exception as exc:
            log.info("  %s client unavailable (%s) -- skipping", kind, exc)
            continue
        alt.warm_up(force=True)
        try:
            html = _validate_search_response(alt.get(url), alt, url)
            log.info("Recovered on attempt %d using the %s client", n, kind)
            return html
        except SoftBlockError as exc:
            log.warning("Attempt %d (%s) blocked (%s)", n, kind, exc)
            last = exc

    raise SoftBlockError(f"{last} (every client blocked, after retries)")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


STATE_VERSION = 2


def _record_first_seen(rec: Any) -> str:
    """first_seen timestamp out of either state format."""
    return rec if isinstance(rec, str) else (rec or {}).get("first_seen", "")


def migrate_state(data: dict[str, Any]) -> dict[str, Any]:
    """Bring a v1 state file up to v2, in place and without losing anything.

    v1: {"seen": {"123": "2026-09-18T16:00:20+00:00"}}
    v2: {"seen": {"123": {"first_seen": "...", "price": null, "crossed": false}}}

    A botched migration is one of the few ways this monitor can fail silently and
    badly: drop the IDs and it re-alerts on everything; mangle them and it alerts
    on nothing ever again. So v1 records are widened, never discarded, and an
    unknown price is recorded as null rather than guessed -- a null price simply
    means "no price-change baseline yet", which the first poll then fills in.
    """
    seen = data.get("seen") or {}
    migrated = 0
    for item_id, rec in list(seen.items()):
        if isinstance(rec, str):
            seen[item_id] = {"first_seen": rec, "price": None,
                             "price_text": "", "crossed": False}
            migrated += 1
        elif isinstance(rec, dict):
            rec.setdefault("first_seen", "")
            rec.setdefault("price", None)
            rec.setdefault("price_text", "")
            rec.setdefault("crossed", False)
        else:
            seen[item_id] = {"first_seen": "", "price": None,
                             "price_text": "", "crossed": False}
            migrated += 1
    if migrated:
        log.info("Migrated %d item(s) from v1 to v2 state (no IDs dropped)", migrated)
    data["seen"] = seen
    data["version"] = STATE_VERSION
    return data


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "seen": {}}
    try:
        data = json.loads(path.read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("seen"), dict):
            return migrate_state(data)
        if isinstance(data, list):  # tolerate a bare list of IDs
            now = datetime.now(timezone.utc).isoformat()
            return migrate_state({"version": 1, "seen": {str(i): now for i in data}})
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("State file %s is unreadable (%s) -- starting fresh, backing it up", path, exc)
        try:
            path.replace(path.with_suffix(path.suffix + ".corrupt"))
        except OSError:
            pass
    return {"version": STATE_VERSION, "seen": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write, so a crash mid-save can't leave a truncated state file."""
    cutoff = time.time() - STATE_RETENTION_DAYS * 86400
    pruned = {}
    for item_id, rec in state.get("seen", {}).items():
        ts = _record_first_seen(rec)
        try:
            if datetime.fromisoformat(ts).timestamp() >= cutoff:
                pruned[item_id] = rec
        except (TypeError, ValueError):
            pruned[item_id] = rec       # unparseable timestamp: keep, never drop
    state["seen"] = pruned
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, indent=2), "utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.error("Could not write state file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def _field(name: str, value: str, inline: bool = True) -> dict[str, Any] | None:
    value = (value or "").strip()
    return {"name": name, "value": value[:1024], "inline": inline} if value else None


def build_embed(listing: Listing, reason: str = "") -> dict[str, Any]:
    desc_bits = []
    if listing.price_text:
        desc_bits.append(f"**{listing.price_text}**")
    if listing.buying_format:
        desc_bits.append(listing.buying_format)
    if reason and reason != "new listing":
        # Say why this one made it through -- especially for a sub-floor auction,
        # where otherwise it just looks like the price filter is broken.
        desc_bits.append(f"\n{reason}")

    seller = listing.seller
    if seller and listing.seller_feedback:
        seller = f"{seller} — {listing.seller_feedback}"
    elif not seller:
        seller = listing.seller_feedback

    fields = [
        _field("Condition", listing.condition),
        _field("Seller", seller),
        _field("Shipping", listing.shipping),
        _field("Listed", listing.listed_at),
        _field("Location", listing.location),
    ]

    embed: dict[str, Any] = {
        "title": listing.title[:256],
        "url": listing.url,
        "color": EMBED_COLOR,
        "description": " · ".join(desc_bits)[:4096] or "​",
        "fields": [f for f in fields if f][:25],
        "footer": {"text": f"eBay item {listing.item_id}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if listing.image:
        embed["thumbnail"] = {"url": listing.image}
    return embed


def build_discord_payload(listings: Iterable[Listing],
                          reasons: dict[str, str] | None = None) -> dict[str, Any]:
    """Discord allows up to 10 embeds per message."""
    reasons = reasons or {}
    return {
        "username": DISCORD_USERNAME,
        "embeds": [build_embed(l, reasons.get(l.item_id, "")) for l in list(listings)[:10]],
    }


def build_test_payload(args) -> dict[str, Any]:
    """A single 'monitor is live' message, in exactly the embed format real alerts use.

    Clearly labelled as a test so nobody mistakes it for a listing.
    """
    return {
        "username": DISCORD_USERNAME,
        "embeds": [{
            "title": "Test ping — eBay monitor is live",
            "url": args.url,
            "color": EMBED_COLOR,
            "description": (
                "This is a **one-off test**, not a listing. Real alerts land here "
                "in this same format: title, price, thumbnail, condition, seller "
                "and a link straight to the item."
            ),
            "fields": [
                {"name": "Watching", "value": "wwe universe slammed · trading card singles",
                 "inline": True},
                {"name": "Alerts on", "value": f"${args.threshold:,.2f} and up (no max)",
                 "inline": True},
                {"name": "Checks every", "value": f"{args.interval // 60} min", "inline": True},
            ],
            "footer": {"text": "eBay listing monitor · setup check"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }


def build_health_payload(args, failures: int, reason: str, recovered: bool = False) -> dict[str, Any]:
    """Tell Discord the monitor itself is in trouble (or back on its feet)."""
    if recovered:
        embed = {
            "title": "Monitor recovered",
            "color": 0x2ECC71,
            "description": f"Polling is working again after {failures} failed "
                           f"{'attempt' if failures == 1 else 'attempts'}.",
        }
    else:
        embed = {
            "title": "Monitor is not reaching eBay",
            "color": 0xE67E22,
            "description": (
                f"**{failures} consecutive polls failed.** No alerts can be sent until "
                f"this clears, so treat silence as broken rather than 'nothing new'.\n\n"
                f"Last error: `{reason[:300]}`"
            ),
            "fields": [
                {"name": "Likely causes",
                 "value": "eBay soft-block or rate limit, a changed results page, "
                          "or no network where the monitor runs.",
                 "inline": False},
                {"name": "Next step",
                 "value": "Run `python ebay_monitor.py --once --dry-run --dump-html page.html` "
                          "and look at what came back.",
                 "inline": False},
            ],
        }
    embed["footer"] = {"text": "eBay listing monitor · health check"}
    embed["timestamp"] = datetime.now(timezone.utc).isoformat()
    return {"username": DISCORD_USERNAME, "embeds": [embed]}


def send_discord(session: requests.Session, webhook: str, payload: dict[str, Any],
                 dry_run: bool = False) -> bool:
    if dry_run or webhook in PLACEHOLDER_WEBHOOKS:
        reason = "dry run" if dry_run else "no webhook configured"
        log.info("Not sending to Discord (%s). Payload that would be POSTed:", reason)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return False

    for attempt in range(1, 4):
        try:
            resp = session.post(webhook, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            log.warning("Discord POST failed (attempt %d/3): %s", attempt, exc)
            time.sleep(2 * attempt)
            continue

        if resp.status_code == 429:
            wait = 5.0
            try:
                wait = float(resp.json().get("retry_after", wait))
            except (ValueError, AttributeError, json.JSONDecodeError):
                pass
            log.warning("Discord rate limited; retrying in %.1fs", wait)
            time.sleep(min(wait + 0.5, 60))
            continue

        if resp.status_code in (200, 204):
            return True

        if 400 <= resp.status_code < 500:
            log.error("Discord rejected the payload (HTTP %s): %s",
                      resp.status_code, resp.text[:400])
            return False

        log.warning("Discord returned HTTP %s (attempt %d/3)", resp.status_code, attempt)
        time.sleep(2 * attempt)

    log.error("Gave up sending this batch to Discord")
    return False


# ---------------------------------------------------------------------------
# Core cycle
# ---------------------------------------------------------------------------


def qualifies(listing: Listing, threshold: float) -> bool:
    """Should this listing be alerted on? Returns True/False; see why() for the reason.

    Two different tests, because a price floor means different things to the two
    selling formats:

      Buy It Now   price >= threshold. The asking price IS the price, so the floor
                   is meaningful and it's what keeps cheap tat out.

      Auction      price >= threshold OR the title carries a value signal
                   (SSP, CASE HIT, /99, auto, relic...). An auction's current bid
                   says nothing about what the card is worth -- everything opens
                   at a dollar. Waiting for the bid to cross the floor means
                   hearing about it only once it's already expensive.

    Digital listings never qualify, at any price, by either route.

    Ranges ("$45.00 to $90.00") use the LOW end, so a range only clears the floor
    when every variation does.
    """
    return bool(why_it_qualifies(listing, threshold))


def why_it_qualifies(listing: Listing, threshold: float) -> str:
    """The human-readable reason a listing qualifies, or "" if it doesn't."""
    if listing.price is None:
        return ""
    if listing.is_digital():
        return ""
    if listing.price >= threshold:
        return f"${listing.price:,.2f} is at or above your ${threshold:,.0f} floor"
    if listing.is_auction:
        signal = listing.value_signal()
        if signal:
            bids = f"{listing.bid_count} bid{'s' if listing.bid_count != 1 else ''}"
            return (f"auction at {listing.price_text} ({bids}) — under your floor, but "
                    f"the title says **{signal}**")
    return ""


def run_cycle(session: requests.Session, state: dict[str, Any], args,
              html_override: str | None = None, fetcher: "Fetcher | None" = None) -> int:
    """One poll. Returns the number of alerts sent (or previewed).

    `session` talks to Discord; `fetcher` talks to eBay. They are different clients
    because only one of them has to get past bot management.
    """
    if html_override is not None:
        html = html_override
    else:
        if fetcher is None:
            fetcher = Fetcher(HTTP_CLIENT)
        html = fetch_search_html(fetcher, args.url)

    if args.dump_html:
        Path(args.dump_html).write_text(html, "utf-8")
        log.info("Wrote %d bytes of HTML to %s", len(html), args.dump_html)

    corrected = check_autocorrect(html, args.url)
    if corrected:
        log.warning("=" * 78)
        log.warning("eBay AUTO-CORRECTED your search and returned a different result set.")
        log.warning("  you asked for : %r", _requested_keywords(args.url))
        log.warning("  eBay searched : %r", corrected)
        log.warning("Alerts below are for the WRONG items. Fix the search URL -- adding")
        log.warning("  -%s  to the keywords usually defeats the autocorrect.",
                    corrected.split()[-1] if corrected.split() else "<term>")
        log.warning("=" * 78)

    listings = parse_listings(html)
    log.info("Parsed %d listings from the search page", len(listings))

    seen: dict[str, str] = state["seen"]
    first_run = not seen
    now = datetime.now(timezone.utc).isoformat()

    fresh = [l for l in listings if l.item_id not in seen]
    log.info("%d of them are new since the last poll", len(fresh))

    if args.verbose_listings:
        for l in listings:
            mark = "NEW " if l.item_id not in seen else "    "
            over = ">" if qualifies(l, args.threshold) else " "
            kind = "auction" if l.is_auction else "BIN"
            log.info("  %s%s [%s] %s", mark, over, kind, l.summary())

    # --- what deserves an alert this cycle -------------------------------------
    # Three distinct events, not just "is it new":
    #   NEW      unseen and it qualifies
    #   DROP     price fell materially on something we're already tracking
    #   CROSSED  a sub-floor auction has now climbed past the floor (fires once)
    # Price RISES are otherwise ignored: auctions rise by design and alerting on
    # every bid would be unusable.
    events: list[tuple[Listing, str]] = []

    for l in listings:
        rec = seen.get(l.item_id)
        reason = why_it_qualifies(l, args.threshold)

        if rec is None:
            if args.no_filter or reason:
                events.append((l, reason or "new listing"))
            continue

        if not isinstance(rec, dict):
            continue
        old_price = rec.get("price")
        if old_price is None or l.price is None:
            continue

        if l.price < old_price:
            drop = old_price - l.price
            pct = (drop / old_price * 100) if old_price else 0
            if drop >= PRICE_DROP_MIN_ABS or pct >= PRICE_DROP_MIN_PCT:
                events.append((l, f"price dropped {pct:.0f}% — was "
                                  f"${old_price:,.2f}, now {l.price_text}"))
                continue

        if (l.price >= args.threshold > old_price) and not rec.get("crossed"):
            events.append((l, f"now {l.price_text} — has crossed your "
                              f"${args.threshold:,.0f} floor"))

    matches = [l for l, _ in events]
    reasons = {l.item_id: r for l, r in events}

    if fresh and first_run and not args.alert_existing:
        log.info("First run: recording %d current listings as seen without alerting "
                 "(use --alert-existing to change that)", len(fresh))
        matches = []

    if matches and len(matches) > args.max_alerts:
        log.warning("Capping %d matches at %d for this cycle", len(matches), args.max_alerts)
        matches = matches[: args.max_alerts]

    sent = 0
    for i in range(0, len(matches), 10):
        batch = matches[i: i + 10]
        payload = build_discord_payload(batch, reasons)
        ok = send_discord(session, args.webhook, payload, dry_run=args.dry_run)
        for l in batch:
            log.info("%s %s — %s  [%s]", "ALERT" if ok else "MATCH",
                     l.price_text or "?", l.title[:70], reasons.get(l.item_id, ""))
        sent += len(batch)
        if ok and i + 10 < len(matches):
            time.sleep(1)  # be gentle with the webhook

    # Record every listing we saw, with its current price, so the next poll has a
    # baseline to compare against. Everything is recorded whether or not it
    # alerted -- that's what stops a listing alerting twice. Dry runs record
    # nothing, so a dry run can be repeated and still show the same result.
    if not args.dry_run:
        for l in listings:
            rec = seen.get(l.item_id)
            if not isinstance(rec, dict):
                rec = {"first_seen": _record_first_seen(rec) or now,
                       "price": None, "price_text": "", "crossed": False}
                seen[l.item_id] = rec
            rec["price"] = l.price
            rec["price_text"] = l.price_text
            if l.price is not None and l.price >= args.threshold:
                rec["crossed"] = True
        save_state(args.state_file, state)
    else:
        log.info("Dry run: not updating %s", args.state_file)

    return sent


# ---------------------------------------------------------------------------
# CLI / main loop
# ---------------------------------------------------------------------------


def setup_logging(level: str, log_file: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch an eBay search and alert Discord on new listings.")
    p.add_argument("--url", default=SEARCH_URL, help="eBay search URL (keep &_sop=10)")
    p.add_argument("--threshold", type=float, default=PRICE_THRESHOLD,
                   help="alert at or above this price (inclusive, no upper limit)")
    p.add_argument("--interval", type=int, default=POLL_INTERVAL, help="seconds between polls")
    p.add_argument("--webhook", default=DISCORD_WEBHOOK_URL, help="Discord webhook URL")
    p.add_argument("--state-file", type=Path, default=STATE_FILE, help="where seen IDs are stored")
    p.add_argument("--max-alerts", type=int, default=MAX_ALERTS_PER_CYCLE)
    p.add_argument("--once", action="store_true", help="single poll, then exit")
    p.add_argument("--test-ping", action="store_true",
                   help="send one 'monitor is live' message to Discord and exit "
                        "(no eBay request, no state change)")
    p.add_argument("--dry-run", action="store_true", help="print the Discord JSON instead of sending")
    p.add_argument("--no-filter", action="store_true",
                   help="ignore the price threshold entirely")
    p.add_argument("--alert-existing", action="store_true",
                   help="on first run, alert on listings that are already up")
    p.add_argument("--verbose-listings", action="store_true",
                   help="log every listing parsed, with NEW / price-match markers")
    p.add_argument("--parse-file", help="parse this local HTML file instead of fetching")
    p.add_argument("--dump-html", help="save the fetched HTML here")
    p.add_argument("--http-client", default=HTTP_CLIENT,
                   choices=["auto", "requests", "httpx", "curl_cffi"],
                   help="which client fetches eBay (see deploy/diagnose.py)")
    p.add_argument("--log-level", default=LOG_LEVEL)
    return p.parse_args(argv)


_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    log.info("Received signal %s -- finishing up and exiting", signum)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level, LOG_FILE)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    if args.webhook in PLACEHOLDER_WEBHOOKS and not args.dry_run:
        log.warning("No Discord webhook configured -- running in dry-run mode. "
                    "Set DISCORD_WEBHOOK_URL (see README) to actually send alerts.")
        args.dry_run = True
    elif args.webhook not in PLACEHOLDER_WEBHOOKS and not args.webhook.startswith(
            ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/",
             "https://ptb.discord.com/api/webhooks/", "https://canary.discord.com/api/webhooks/")):
        log.error("That doesn't look like a Discord webhook URL. Expected "
                  "https://discord.com/api/webhooks/<id>/<token>")
        return 2

    log.info("Watching: %s", args.url)
    log.info("Threshold: >=%.2f (no upper limit) | interval: %ds | state: %s | dry-run: %s",
             args.threshold, args.interval, args.state_file, args.dry_run)

    session = build_session()
    health_warned = False

    if args.test_ping:
        log.info("Sending a single test ping to Discord")
        ok = send_discord(session, args.webhook, build_test_payload(args), dry_run=args.dry_run)
        if args.dry_run:
            return 0
        log.info("Test ping %s", "delivered" if ok else "FAILED -- see the error above")
        return 0 if ok else 1

    state = load_state(args.state_file)
    log.info("Loaded %d previously seen item IDs", len(state["seen"]))

    html_override = Path(args.parse_file).read_text("utf-8") if args.parse_file else None

    fetcher = None
    if html_override is None:
        fetcher = Fetcher(args.http_client)
        log.info("eBay client: %s%s", fetcher.kind,
                 "" if args.http_client != "auto" else " (auto-selected, best available)")
        if fetcher.kind == "requests":
            log.info("Note: plain requests sends a Python TLS fingerprint. If eBay returns "
                     "403, run deploy/diagnose.py -- curl_cffi usually fixes it.")

    backoff = 1
    consecutive_failures = 0

    last_cycle_ok = False

    while True:
        started = time.monotonic()
        last_error = ""
        try:
            run_cycle(session, state, args, html_override=html_override, fetcher=fetcher)
            last_cycle_ok = True
            # Failure bookkeeping lives in the state file so it survives one-shot runs.
            if state.get("failures") or state.get("health_alerted"):
                if state.get("health_alerted"):
                    send_discord(session, args.webhook,
                                 build_health_payload(args, int(state.get("failures", 0)), "",
                                                      recovered=True),
                                 dry_run=args.dry_run)
                    log.info("Polling recovered after %s failed poll(s)", state.get("failures"))
                state["failures"] = 0
                state["health_alerted"] = False
                if not args.dry_run:
                    save_state(args.state_file, state)
            health_warned = False
            backoff = 1
            consecutive_failures = 0
        except SoftBlockError as exc:
            consecutive_failures += 1
            last_cycle_ok = False
            backoff = min(backoff * 2, 8)
            last_error = f"soft block: {exc}"
            log.warning("Poll blocked/empty (%s). Backing off %dx.", exc, backoff)
        except requests.RequestException as exc:
            consecutive_failures += 1
            last_cycle_ok = False
            backoff = min(backoff * 2, 8)
            last_error = f"network error: {exc}"
            log.warning("Network error: %s. Backing off %dx.", exc, backoff)
        except OSError as exc:
            consecutive_failures += 1
            last_cycle_ok = False
            last_error = f"filesystem error: {exc}"
            log.error("Filesystem error: %s", exc)
        except Exception as exc:  # last line of defence -- the loop must not die
            consecutive_failures += 1
            last_cycle_ok = False
            backoff = min(backoff * 2, 8)
            last_error = f"{type(exc).__name__}: {exc}"
            log.exception("Unexpected error during poll; continuing")

        # Record the failure durably, so consecutive failures accumulate across
        # one-shot runs instead of resetting with every fresh process.
        if not last_cycle_ok:
            state["failures"] = int(state.get("failures", 0)) + 1
            if not args.dry_run:
                save_state(args.state_file, state)
            log.warning("Failed poll #%s in a row", state["failures"])

        # Silence looks exactly like "nothing new", so say so out loud -- once.
        if (ALERT_AFTER_FAILURES and not last_cycle_ok
                and not state.get("health_alerted")
                and int(state.get("failures", 0)) >= ALERT_AFTER_FAILURES):
            log.error("%s consecutive failed polls -- notifying Discord", state["failures"])
            send_discord(session, args.webhook,
                         build_health_payload(args, int(state["failures"]), last_error),
                         dry_run=args.dry_run)
            state["health_alerted"] = True
            health_warned = True
            if not args.dry_run:
                save_state(args.state_file, state)

        if args.once or _stop:
            break

        # Jitter so we're not hitting eBay on a perfectly robotic schedule.
        elapsed = time.monotonic() - started
        sleep_for = max(5.0, args.interval * backoff * random.uniform(0.9, 1.1) - elapsed)
        log.debug("Sleeping %.0fs", sleep_for)
        slept = 0.0
        while slept < sleep_for and not _stop:
            time.sleep(min(1.0, sleep_for - slept))
            slept += 1.0
        if _stop:
            break

    log.info("Done.")

    # A one-shot run must report failure in its exit code -- cron, CI and the
    # launchd installer's preflight all decide what to do based on it, and
    # "exited 0 having fetched nothing" is exactly the silent failure this
    # monitor is supposed to make impossible.
    if args.once and not last_cycle_ok:
        log.error("Single poll did not complete successfully -- exiting non-zero")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
