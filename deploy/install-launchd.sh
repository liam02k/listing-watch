#!/bin/bash
#
# Install the eBay monitor as a launchd agent so it runs in the background,
# starts at login, and restarts if it ever crashes. No terminal window needed.
#
#   ./deploy/install-launchd.sh
#
# Uninstall:  ./deploy/install-launchd.sh --uninstall
#
set -euo pipefail

LABEL="com.liam.ebaymonitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Uninstalled $LABEL."
  exit 0
fi

# --- work out which python to use -------------------------------------------
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  PYTHON="$PROJECT_DIR/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
  echo "note: no .venv found, using $PYTHON"
  echo "      (recommended: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)"
fi

# --- check .env, but never copy the secret anywhere ---------------------------
# ebay_monitor.py reads .env itself, so the plist stays secret-free and the
# webhook exists in exactly one file on disk.
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "error: $PROJECT_DIR/.env not found -- it must contain DISCORD_WEBHOOK_URL" >&2
  exit 1
fi
if ! grep -qE '^\s*(export\s+)?DISCORD_WEBHOOK_URL\s*=\s*"?https://(ptb\.|canary\.)?discord(app)?\.com/api/webhooks/' "$PROJECT_DIR/.env"; then
  echo "error: .env has no usable DISCORD_WEBHOOK_URL (expected a discord.com/api/webhooks/... URL)" >&2
  exit 1
fi

chmod 600 "$PROJECT_DIR/.env"
echo "Secret check: .env present, looks like a webhook, now chmod 600."

# --- preflight: make sure one poll actually works before installing ----------
echo "Running one live test poll before installing (no Discord messages sent)..."
if ! ( cd "$PROJECT_DIR" && "$PYTHON" ebay_monitor.py --once --dry-run >/tmp/ebaymon-preflight.log 2>&1 ); then
  echo >&2
  echo "error: the test poll FAILED -- not installing." >&2
  echo "       eBay could not be reached or returned no results. Output:" >&2
  tail -20 /tmp/ebaymon-preflight.log >&2
  exit 1
fi

# Exit code 0 is necessary but not sufficient: insist on evidence it really parsed.
if ! grep -qE "Parsed [0-9]+ listings" /tmp/ebaymon-preflight.log; then
  echo "error: test poll exited cleanly but parsed nothing -- not installing." >&2
  tail -20 /tmp/ebaymon-preflight.log >&2
  exit 1
fi
if grep -q "Parsed 0 listings" /tmp/ebaymon-preflight.log; then
  echo "error: test poll found 0 listings (soft block, or the search is empty)." >&2
  echo "       Not installing. Try: $PYTHON ebay_monitor.py --once --dry-run --dump-html page.html" >&2
  exit 1
fi
if grep -q "AUTO-CORRECTED" /tmp/ebaymon-preflight.log; then
  echo "error: eBay auto-corrected the search -- it would watch the WRONG items." >&2
  echo "       Not installing. Fix EBAY_SEARCH_URL first (see README, 'The search URL')." >&2
  grep -A4 "AUTO-CORRECTED" /tmp/ebaymon-preflight.log >&2
  exit 1
fi

echo "Test poll OK: $(grep -oE 'Parsed [0-9]+ listings' /tmp/ebaymon-preflight.log | tail -1), no autocorrect."

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$PROJECT_DIR/ebay_monitor.py</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <!-- No EnvironmentVariables block on purpose: ebay_monitor.py loads .env
       itself, so the webhook is never duplicated into this file. -->
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$PROJECT_DIR/monitor.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/monitor.log</string>
</dict>
</plist>
PLISTEOF

chmod 644 "$PLIST"   # contains no secret

# Paranoia: fail loudly if the webhook ever leaks into the plist.
if grep -q "discord.com/api/webhooks" "$PLIST"; then
  echo "error: the webhook ended up in $PLIST -- refusing to leave it there" >&2
  rm -f "$PLIST"
  exit 1
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

sleep 3
echo
if launchctl list | grep -q "$LABEL"; then
  echo "Verified loaded:"
  launchctl list | grep "$LABEL" | sed 's/^/  /'
  echo "  (columns: PID, last exit status, label -- a PID means it is running now)"
else
  echo "WARNING: $LABEL is not showing in launchctl list. Check $PROJECT_DIR/monitor.log" >&2
fi

if [[ -s "$PROJECT_DIR/monitor.log" ]]; then
  echo
  echo "First lines of monitor.log:"
  head -12 "$PROJECT_DIR/monitor.log" | sed 's/^/  /'
fi

echo
echo "Installed and started: $LABEL"
echo "  logs:      tail -f $PROJECT_DIR/monitor.log"
echo "  status:    launchctl print gui/$(id -u)/$LABEL | head -20"
echo "  stop:      launchctl bootout gui/$(id -u)/$LABEL"
echo "  uninstall: $0 --uninstall"
echo
echo "Note: this runs while you're logged in. It pauses when the Mac sleeps and"
echo "resumes on wake. To keep polling overnight, either set Settings > Lock Screen"
echo "and Displays to prevent sleep on power, or run: caffeinate -s -w \$(pgrep -f ebay_monitor.py)"
