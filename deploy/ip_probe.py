#!/usr/bin/env python3
"""Does the cookie warm-up technique survive a datacenter IP?

Self-contained on purpose: it only needs requests, so it can run on a fresh
runner with nothing else checked out. It reproduces the three request
signatures we measured on a residential connection and reports what each one
actually gets back.

Success is NOT "HTTP 200". eBay soft-blocks by redirecting to /n/all-categories
and by returning short pages with no result cards, both with status 200. So we
check the final URL, the byte count, the number of result cards, and the search
heading before calling anything a win.
"""
import re, sys, time
import requests

URL  = "https://www.ebay.com/sch/i.html?_nkw=wwe+universe+slammed+-slammer&_sop=10"
HOME = "https://www.ebay.com/"

CHROME_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

def chrome_headers(nav=True):
    return {
        "sec-ch-ua": '"Not/A)Brand";v="99", "Chromium";v="148"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": CHROME_UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                   "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        "Sec-Fetch-Site": "none" if nav else "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
        "Priority": "u=0, i",
    }

LEGACY = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": HOME,
    "Upgrade-Insecure-Requests": "1",
}

def heading(html):
    m = re.search(r'class="srp-controls__count-heading"[^>]*>(.*?)</h1>', html, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()[:80] if m else ""

def report(name, resp, err=""):
    if resp is None:
        print("  [ no  ] %-34s ERROR %s" % (name, err[:60]))
        return False
    html  = resp.text
    cards = len(re.findall(r'<li class="s-card', html))
    final = resp.url
    head  = heading(html)
    bounced = "/sch/" not in final
    ok = (resp.status_code == 200) and cards > 0 and not bounced
    print("  [%s] %s" % ("WORKS" if ok else " no  ", name))
    print("          status=%s bytes=%s cards=%s" % (resp.status_code, len(html), cards))
    print("          final_url=%s" % final[:95])
    if bounced:
        print("          ^^ REDIRECTED AWAY -- this is eBay's soft block, not results")
    if head:
        print("          heading=%r" % head)
    return ok

print("=" * 72)
print("Datacenter IP probe -- eBay search fetch")
print("=" * 72)

results = {}

print("\n1. Old headers, no warm-up (expected to fail everywhere)")
try:
    s = requests.Session(); s.headers.update(LEGACY)
    results["legacy"] = report("legacy headers, cold", s.get(URL, timeout=30))
except Exception as e:
    results["legacy"] = report("legacy headers, cold", None, str(e))
time.sleep(4)

print("\n2. Chrome 148 headers, still no cookies")
try:
    s = requests.Session(); s.headers.update(chrome_headers())
    results["cold"] = report("chrome headers, cold", s.get(URL, timeout=30))
except Exception as e:
    results["cold"] = report("chrome headers, cold", None, str(e))
time.sleep(4)

print("\n3. Chrome headers + homepage warm-up  <-- the technique under test")
try:
    s = requests.Session(); s.headers.update(chrome_headers())
    w = s.get(HOME, timeout=30)
    print("          warm-up: HTTP %s, %d cookies (%s)"
          % (w.status_code, len(s.cookies), ", ".join(sorted(c.name for c in s.cookies))[:70]))
    print("          (a non-200 warm-up is normal -- the cookies are the point)")
    time.sleep(2)
    h = chrome_headers(nav=False); h["Referer"] = HOME
    results["warm"] = report("chrome headers + warm-up", s.get(URL, timeout=30, headers=h))
except Exception as e:
    results["warm"] = report("chrome headers + warm-up", None, str(e))

print("\n" + "=" * 72)
if results.get("warm"):
    print("VERDICT: the warm-up technique WORKS from this datacenter IP.")
    print("         GitHub Actions is viable.")
elif results.get("cold") or results.get("legacy"):
    print("VERDICT: mixed -- a simpler variant worked but the warm-up one did not.")
    print("         Unexpected; paste this whole log back.")
else:
    print("VERDICT: BLOCKED. No variant returned real results from this IP.")
    print("         Datacenter hosting is not viable; use a home-network device.")
print("=" * 72)
