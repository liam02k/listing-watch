#!/usr/bin/env python3
"""Work out what request signature eBay will accept from THIS machine.

The monitor got an instant HTTP 403 while Chrome on the same machine and the same
IP loaded the identical URL fine. So it isn't the IP and isn't the URL -- it's how
the request looks. This tries five progressively more browser-like variants and
tells you which ones actually work.

Run it:
    cd <this folder>
    .venv/bin/pip install -q "httpx[http2]" curl_cffi brotli
    .venv/bin/python deploy/diagnose.py

Sends nothing to Discord. Makes at most ~7 requests, spaced out.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

URL = "https://www.ebay.com/sch/i.html?_nkw=wwe+universe+slammed+-slammer&_sop=10"
HOME = "https://www.ebay.com/"
PAUSE = 4.0

try:
    import ebay_monitor as em
except Exception as exc:  # pragma: no cover
    sys.exit(f"Could not import ebay_monitor.py (run this from the project folder): {exc}")

# --- the header set real Chrome 148 on macOS sends for a top-level navigation ---
# Captured from the browser that successfully loaded this exact URL. Order matters:
# fingerprinters compare header order as well as content.
CHROME_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

def chrome_headers(navigation: bool = True) -> dict:
    h = {
        "sec-ch-ua": '"Not/A)Brand";v="99", "Chromium";v="148"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": CHROME_UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                   "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        "Sec-Fetch-Site": "none" if navigation else "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Accept-Encoding": _encodings(),
        "Accept-Language": "en-US,en;q=0.9",
        "Priority": "u=0, i",
    }
    return h


def _encodings() -> str:
    """Only advertise what we can actually decode, or the body comes back as bytes."""
    encs = ["gzip", "deflate"]
    try:
        import brotli  # noqa: F401
        encs.append("br")
    except ImportError:
        try:
            import brotlicffi  # noqa: F401
            encs.append("br")
        except ImportError:
            pass
    try:
        import zstandard  # noqa: F401
        encs.append("zstd")
    except ImportError:
        pass
    return ", ".join(encs)


# --- the old header set, for comparison -------------------------------------
LEGACY_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.ebay.com/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}

results: list[dict] = []


def assess(name: str, status, html: str, note: str = "") -> dict:
    """Status 200 isn't success -- eBay soft-blocks with 200 and an empty page."""
    listings, err, corrected = [], "", None
    if html:
        try:
            listings = em.parse_listings(html)
            corrected = em.check_autocorrect(html, URL)
        except em.SoftBlockError as exc:
            err = str(exc)[:60]
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:60]

    ok = bool(status == 200 and listings and not corrected)
    r = {"name": name, "status": status, "bytes": len(html or ""),
         "listings": len(listings), "autocorrect": corrected,
         "err": err or note, "ok": ok}
    results.append(r)

    flag = "WORKS" if ok else "no"
    print(f"  [{flag:5}] {name}")
    print(f"          status={status}  bytes={len(html or ''):,}  listings={len(listings)}"
          + (f"  autocorrected->{corrected!r}" if corrected else "")
          + (f"  ({r['err']})" if r["err"] else ""))
    if listings:
        top = max(listings, key=lambda l: l.price or 0)
        print(f"          top listing: {top.price_text} {top.title[:52]}")
    return r


print("=" * 74)
print("eBay request-signature diagnostic")
print("=" * 74)
print(f"URL: {URL}")
print(f"Advertising Accept-Encoding: {_encodings()}")
print()

# --- 1. baseline: what the monitor sends today -------------------------------
print("1. Baseline -- the headers the monitor used when it got 403")
try:
    import requests
    s = requests.Session()
    s.headers.update(LEGACY_HEADERS)
    r = s.get(URL, timeout=30)
    assess("requests + old headers", r.status_code, r.text)
except Exception as exc:
    assess("requests + old headers", "ERR", "", str(exc)[:60])
time.sleep(PAUSE)

# --- 2. modern Chrome headers, cold (no cookies) -----------------------------
print("\n2. Full Chrome 148 headers incl. client hints, no cookies")
try:
    import requests
    s = requests.Session()
    s.headers.update(chrome_headers())
    r = s.get(URL, timeout=30)
    assess("requests + Chrome headers", r.status_code, r.text)
except Exception as exc:
    assess("requests + Chrome headers", "ERR", "", str(exc)[:60])
time.sleep(PAUSE)

# --- 3. same, but warm the session on the homepage first ---------------------
print("\n3. Chrome headers + cookie warm-up (GET ebay.com, then the search)")
try:
    import requests
    s = requests.Session()
    s.headers.update(chrome_headers())
    warm = s.get(HOME, timeout=30)
    print(f"          warm-up: {warm.status_code}, {len(s.cookies)} cookies "
          f"({', '.join(sorted(c.name for c in s.cookies)[:6])})")
    time.sleep(2.0)
    h = chrome_headers(navigation=False)
    h["Referer"] = HOME
    r = s.get(URL, timeout=30, headers=h)
    assess("requests + headers + cookies", r.status_code, r.text)
except Exception as exc:
    assess("requests + headers + cookies", "ERR", "", str(exc)[:60])
time.sleep(PAUSE)

# --- 4. HTTP/2 -- Chrome never speaks HTTP/1.1 to eBay -----------------------
print("\n4. HTTP/2 via httpx (Chrome uses h2; plain requests is HTTP/1.1)")
try:
    import httpx
    with httpx.Client(http2=True, headers=chrome_headers(), timeout=30,
                      follow_redirects=True) as c:
        c.get(HOME)
        time.sleep(2.0)
        r = c.get(URL)
        assess(f"httpx HTTP/2 ({r.http_version})", r.status_code, r.text)
except ImportError:
    assess("httpx HTTP/2", "SKIP", "", 'pip install "httpx[http2]"')
except Exception as exc:
    assess("httpx HTTP/2", "ERR", "", str(exc)[:60])
time.sleep(PAUSE)

# --- 5. real Chrome TLS fingerprint ------------------------------------------
print("\n5. curl_cffi impersonating Chrome's TLS/JA3 fingerprint")
try:
    from curl_cffi import requests as cffi
    s = cffi.Session(impersonate="chrome")
    s.get(HOME, timeout=30)
    time.sleep(2.0)
    r = s.get(URL, timeout=30)
    assess("curl_cffi impersonate=chrome", r.status_code, r.text)
except ImportError:
    assess("curl_cffi impersonate=chrome", "SKIP", "", "pip install curl_cffi")
except Exception as exc:
    assess("curl_cffi impersonate=chrome", "ERR", "", str(exc)[:60])

# --- verdict -----------------------------------------------------------------
print()
print("=" * 74)
winners = [r for r in results if r["ok"]]
if winners:
    best = winners[0]
    print(f"WINNER: {best['name']}")
    print(f"  Returned {best['listings']} listings, no autocorrect. Use this one.")
    if len(winners) > 1:
        print("  Also worked: " + ", ".join(w["name"] for w in winners[1:]))
    print()
    if best["name"].startswith("requests + Chrome headers"):
        print("  -> Headers alone fix it. No new dependency needed.")
    elif "cookies" in best["name"]:
        print("  -> Needs a cookie warm-up as well as headers. No new dependency.")
    elif "httpx" in best["name"]:
        print('  -> Needs HTTP/2. Add "httpx[http2]" to requirements.txt.')
    elif "curl_cffi" in best["name"]:
        print("  -> eBay is fingerprinting TLS, not just headers.")
        print("     Add curl_cffi to requirements.txt -- headers alone will not do it.")
else:
    print("NOTHING WORKED.")
    print("  Every scripted variant was blocked, while Chrome on this machine loads")
    print("  the same URL fine. That points at browser-level fingerprinting beyond")
    print("  TLS (JS challenge / behavioural signals).")
    print("  -> Raw HTTP scraping is not viable here. Switch to driving a real")
    print("     browser on a schedule. See deploy/HOSTING.md.")
print("=" * 74)
print()
print("Paste this whole output back and I'll wire the winner into the monitor.")
