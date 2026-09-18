# How the monitor works

Plain-language version. No code required.

## The one-sentence version

Every five minutes it loads your eBay search the way a browser would, reads the
listings off the page, throws away every one it has seen before, keeps what costs
$45 or more, and posts those to Discord.

## The loop, step by step

**1. Warm up.** Before asking for search results, it visits `ebay.com` — the
plain homepage — and keeps whatever cookies eBay hands over.

This step looks pointless and is the whole reason the thing works. eBay sits
behind bot-detection software. A script that walks straight up to a search URL
with no cookies gets refused instantly, even from your own home internet. Visit
the front door first, collect the cookies, and the search then goes through.

Odd detail worth knowing: **the homepage visit itself gets refused** — it returns
an error and only two cookies. Those two cookies are enough. The monitor doesn't
care that the warm-up "failed", because the thing it wanted was the cookies, not
the page.

Cookies go stale, so it re-does this every 30 minutes, and immediately if a
search ever looks blocked.

**2. Fetch the search.** It requests your search URL, pretending to be Chrome on
a Mac — right down to the little headers real Chrome sends announcing its version
and platform. Getting those subtly wrong is what a bot looks like.

Your search URL has two deliberate quirks:

- **`-slammer`** — eBay thinks "slammed" is a typo for "slammer" and quietly
  searches for the wrong word, returning 2019 cards worth about $30 instead of
  the 2026 SLAMMED inserts worth thousands. Telling it to exclude "slammer"
  stops the substitution. The monitor re-checks this every single poll and
  complains loudly in the log if eBay ever starts doing it again.
- **No category filter** — the category filter was hiding a real $2,000 card
  that eBay had filed somewhere else.

**3. Check we got a real page.** A blocked response doesn't always look blocked.
One of the tools we tested returned a perfectly normal-looking "success" with an
empty page inside. So success is judged by *whether listings were actually
found*, never by the server saying OK. If the page looks empty, it bins the
cookies, warms up again, and retries once before treating it as a failure.

**4. Read the listings.** It picks each result card off the page and pulls out
the title, price, condition, seller, shipping, when it was listed, the thumbnail
and the link. It skips eBay's sponsored "Shop on eBay" filler card, which isn't a
real listing.

**5. Drop anything seen before.** Every listing has an eBay item number. Those
are kept in a file called `seen_items.json`. Anything already in that file is
ignored — that's what stops the same card being announced over and over, and it
survives restarts because it's a file on disk, not memory.

The file was pre-loaded with the listings that existed when it was set up, so it
starts quiet and only tells you about genuinely new ones.

**6. Apply the price filter.** Keep anything **$45 or more**. Exactly $45 counts.
There's no upper limit — a $10,000 card alerts the same as a $45 one. For a
listing showing a price *range* (multi-variation), it only counts if the bottom
of the range clears $45.

**7. Post to Discord.** Whatever survives becomes a message in your channel: title,
price, thumbnail, condition, seller rating, and a link straight to the item.
Several new listings in one poll get combined into a single message rather than
several.

**8. Remember everything.** Every listing seen this round goes into the file —
including the cheap ones that didn't qualify — so nothing can alert late.

Then it sleeps about five minutes (with a little randomness, so it isn't hitting
eBay on a robotic metronome) and starts again.

## When things go wrong

The failure that actually matters isn't a crash — it's the monitor quietly doing
nothing while looking fine, because silence is exactly what "no new listings"
looks like. So:

- Network problems, blocked requests and unexpected errors are caught and logged;
  the loop never dies.
- Repeated blocks make it wait longer between tries (2×, 4×, up to 8×) instead of
  hammering eBay.
- **After 3 failed polls in a row it posts an orange warning to Discord**, and a
  green one when it recovers. So silence means "nothing new" and you can trust it.
- If eBay starts substituting your search terms again, that's flagged in the log
  every poll.

## What it does NOT do

- It never signs into your eBay account, and doesn't use one. It reads public
  search pages, the same ones anyone gets without logging in.
- It never bids, buys, watches or messages anyone. It only reads.
- It only reads the **first page** of results, sorted newest-first. That's all it
  needs — anything new appears at the top, and your search only returns about six
  listings in total.
