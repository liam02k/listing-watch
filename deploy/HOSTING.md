# Where should this run?

Liam doesn't want this tied to his Mac being awake. Fair. Here's the honest state
of play.

## First, a correction

An earlier version of this document said datacenter IPs looked blocked, based on
a fetch that "succeeded on the old URL and timed out on the new one". That was
wrong on both halves:

- The "success" was never checked. Re-reading it, that response was a **redirect
  to `/n/all-categories`** — eBay's category index, which is its soft-block
  bounce. It never returned search results at all.
- The timeouts were transient. The same URL fetched fine later — and also landed
  on the category index.

More importantly, **all of those tests used a cold request with no cookie
warm-up** — the same thing that returns 403 from Liam's own residential IP. So
they say nothing about whether datacenter IPs work *with* the technique that we
now know works. That was a confound, and carrying the conclusion forward would
have been sloppy.

**The datacenter question is currently unanswered.** `deploy/workflows/ip-test.yml`
answers it.

## Step 1: run the test (Liam, ~5 minutes)

1. Create a **public** repo on GitHub and push this folder.
   Run `./deploy/check-before-publishing.sh` first — it refuses if `.env` or a
   real webhook is tracked.
2. Copy `deploy/workflows/ip-test.yml` to `.github/workflows/ip-test.yml`, commit, push.
3. GitHub → **Actions** → **Datacenter IP test** → **Run workflow**.
4. Open the run, expand **Run the diagnostic**, paste the output back.

It needs no secrets and sends nothing to Discord. It runs the same
`deploy/diagnose.py` that found the fix on his Mac, so the results are directly
comparable. The verdict line at the end says `WINNER: ...` or `NOTHING WORKED`.

## Step 2: it depends what comes back

### If a variant works → GitHub Actions, and that's the recommendation

Use `deploy/workflows/monitor.yml`. What Liam should know going in:

**The repo must be public.** Private repos get 2,000 Actions minutes/month;
polling every 10 minutes is ~4,300 runs/month at roughly a minute each. Public
repos are unlimited. So the repo — and everything in it — is world-readable.

**What becomes public:** this code, the README, the search URL (revealing that he
collects WWE cards), and `seen_items.json` — eBay listing numbers and timestamps.
Nothing personal, no account details, no purchase history.

**What must NOT become public:** the Discord webhook. Anyone holding it can post
to his channel. It goes in **Settings → Secrets and variables → Actions** as
`DISCORD_WEBHOOK_URL`, never in a file. `.env` stays gitignored. Two guards back
this up: `deploy/check-before-publishing.sh` before pushing, and a step in the
workflow itself that fails the run if a real webhook appears in a tracked file.
Both match real webhook shapes only, so the fake ones in `tests/` don't cry wolf.

**State: commit `seen_items.json` back to the repo.** I considered the
alternatives and rejected them:

| Option | Why not |
|---|---|
| Actions **cache** | Evicted after 7 days unused, and silently. An evicted cache means an empty state file, which the first-run logic treats as "seed silently" — so he'd *miss* alerts and never know. Silent data loss is the one failure mode this project is built to avoid. |
| Artifacts | Retention limits, and you must download the previous run's artifact by API — more moving parts for the same job. |
| **Commit back** | Durable, visible, auditable, survives indefinitely. Costs ~144 commits/day of noise, which is ugly but harmless, and conveniently counts as repo activity — which stops GitHub auto-disabling scheduled workflows after 60 days. |

The workflow pulls with `--rebase --autostash` and retries the push, so
overlapping runs can't clobber each other's state.

**Timing is worse than he asked for.** GitHub's minimum is 5 minutes, but
scheduled runs are routinely delayed 10–40 minutes under load. The workflow asks
for `*/10` because requesting `*/5` mostly buys unpredictability and overlapping
runs. For *new listing* alerts on a search with ~6 results, that's fine. It is
not a sniping tool and shouldn't be treated as one.

### If nothing works → three options that don't need the Mac awake

**A. Raspberry Pi on his home network — best of the three.** This is the one I'd
pick. It runs on the *same residential IP that already works*, which sidesteps the
entire datacenter question rather than betting on it.

- Cost: ~$15 for a Pi Zero 2 W, or ~$60–80 for a Pi 4 kit with case, PSU and SD card
- Power: 1–3 W, roughly $2–4/year
- Setup: flash Raspberry Pi OS, copy this folder over, `pip install -r requirements.txt`,
  install a systemd service (the launchd script's logic ports over in ~10 lines)
- Downside: a small amount of hardware fiddling, and it's another box to keep patched

**B. Stop the Mac sleeping — cheapest, zero new hardware.** Turns "computer
running" into "computer plugged in with the lid shut".

```bash
sudo pmset -c sleep 0          # on AC power: never sleep
sudo pmset -c disablesleep 1   # allow lid-closed (clamshell) operation
pmset -g                       # check what took effect
```

To undo: `sudo pmset -c disablesleep 0 && sudo pmset -c sleep 10`.

- Cost: $0, plus a few watts. Needs his admin password (I can't run `sudo` for him)
- Downside: the Mac must stay plugged in, and it never sleeps — more heat, more
  wear, and a laptop that stays warm in a bag if he forgets to undo it.
  `caffeinate -s` is the gentler, per-session version if he'd rather not change
  system settings permanently.

**C. A cheap always-on VPS — only if a datacenter IP turned out to work**, which
in this branch it didn't. Listed for completeness; skip it.

## My recommendation

1. **Run the IP test.** It's cheap and it's the only thing that settles the
   question. Everything else is speculation.
2. **If it passes → GitHub Actions**, public repo, webhook as a secret, state
   committed back, `*/10` schedule with realistic expectations.
3. **If it fails → Raspberry Pi**, because it keeps the residential IP that we
   have actual evidence for. `pmset` on the Mac is the zero-cost stopgap while he
   decides.

In the meantime the launchd agent works whenever the Mac is on, and the health
alert means he'll know if it stops. Nothing here is urgent enough to guess.
