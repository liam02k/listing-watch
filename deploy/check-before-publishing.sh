#!/bin/bash
#
# Run this BEFORE pushing to a public repo.
# Checks that nothing secret is about to become world-readable.
#
#   ./deploy/check-before-publishing.sh
#
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0
say_bad()  { echo "  FAIL  $1"; fail=1; }
say_ok()   { echo "  ok    $1"; }

echo "Checking what would become public..."
echo

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Not a git repo yet -- run 'git init' first, then re-run this."
  exit 1
fi

# 1. .env must not be tracked
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  say_bad ".env is TRACKED -- your Discord webhook would be published"
  echo "        fix: git rm --cached .env  (and rotate the webhook in Discord)"
else
  say_ok ".env is not tracked"
fi

# 2. no REAL webhook URL anywhere in tracked files.
# Real webhooks are /api/webhooks/<17-20 digit id>/<60+ char token>. The obviously
# fake ones in tests (".../1/test") must not trip this, or the guard gets ignored.
REAL_WEBHOOK='discord(app)?\.com/api/webhooks/[0-9]{17,20}/[A-Za-z0-9_-]{50,}'
if git grep -qIE "$REAL_WEBHOOK" -- . 2>/dev/null; then
  say_bad "a real Discord webhook URL appears in a tracked file:"
  git grep -nIE "$REAL_WEBHOOK" -- . | sed 's/\(webhooks\/[0-9]*\/\).*/\1<TOKEN>/' | sed 's/^/        /'
  echo "        fix: remove it, ROTATE the webhook in Discord, and if you already"
  echo "             pushed, treat the old one as compromised"
else
  say_ok "no real webhook URL in tracked files"
fi

# 3. .gitignore covers the usual suspects
for pattern in ".env" "*.log"; do
  if grep -qF -- "$pattern" .gitignore 2>/dev/null; then
    say_ok ".gitignore covers $pattern"
  else
    say_bad ".gitignore is missing $pattern"
  fi
done

# 4. seen_items.json: for GitHub Actions it MUST be committed, so just show it
if [[ -f seen_items.json ]]; then
  n=$(python3 -c "import json;print(len(json.load(open('seen_items.json'))['seen']))" 2>/dev/null || echo "?")
  say_ok "seen_items.json has $n item IDs (these will be public -- eBay listing"
  echo "        numbers and timestamps only, nothing personal)"
fi

echo
echo "Everything that WOULD be published:"
git ls-files 2>/dev/null | sed 's/^/  /' || echo "  (nothing staged yet)"

echo
if [[ $fail -eq 0 ]]; then
  echo "SAFE TO PUBLISH."
else
  echo "DO NOT PUSH until the FAIL items above are fixed."
  exit 1
fi
