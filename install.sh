#!/usr/bin/env bash
#
# install.sh — set up the self-heal poller on this Mac.
#
#   1. creates runtime dirs and checks config.json exists
#   2. creates the `scrape-failure` + `self-heal` labels in every configured
#      repo (labels aren't part of git — an alert workflow that references a
#      missing label fails at the worst moment)
#   3. renders the launchd plist for this user and bootstraps it
#
# Idempotent: safe to re-run after config changes (it reloads the agent).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_LABEL="com.selfheal.poller"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

echo "→ preparing $ROOT"
mkdir -p "$ROOT/logs" "$ROOT/work" "$ROOT/state"
chmod +x "$ROOT/bin/poll.sh" "$ROOT/bin/selfheal.py" "$ROOT"/verify/*.sh

if [ ! -f "$ROOT/config.json" ]; then
  echo "!! config.json missing — copy config.example.json and fill in your values:"
  echo "   cp config.example.json config.json"
  exit 1
fi

echo "→ checking gh auth"
gh auth status >/dev/null

echo "→ creating labels in configured repos (idempotent)"
/usr/bin/python3 - "$ROOT/config.json" <<'PY'
import json, subprocess, sys

cfg = json.load(open(sys.argv[1]))
defaults = cfg.get("defaults", {})
for repo in cfg["repos"]:
    merged = {**defaults, **repo}
    slug = merged["slug"]
    # Each repo names its own failure label (scrapers use scrape-failure, the
    # landing page uses build-failure) — create what THIS repo is configured to
    # poll, not a hardcoded one, or the alert step and the poller disagree.
    for name, color, desc in (
        (merged.get("failure_label", "scrape-failure"), "d73a4a",
         "opened by CI when the scheduled job fails"),
        ("self-heal", "1d76db", "PR opened by the local self-healing pipeline"),
    ):
        proc = subprocess.run(
            ["gh", "label", "create", name, "--repo", slug,
             "--color", color, "--description", desc],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            print(f"   {slug}: created label {name}")
        elif "already exists" in (proc.stderr or ""):
            print(f"   {slug}: label {name} already exists")
        else:
            print(f"   {slug}: FAILED to create {name}: {proc.stderr.strip()}")
            sys.exit(1)
PY

echo "→ rendering plist → $PLIST_DST"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|/Users/CHANGE_ME|$HOME|g" "$ROOT/launchd/$PLIST_LABEL.plist" > "$PLIST_DST"
plutil -lint "$PLIST_DST"

echo "→ (re)loading launchd agent"
launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl print "gui/$(id -u)/$PLIST_LABEL" | grep -E "state|interval" | head -3 || true

cat <<EOF

Installed. Next steps:
  1. python3 bin/selfheal.py doctor      # full preflight incl. live verify runs
  2. launchctl kickstart -k gui/$(id -u)/$PLIST_LABEL   # force one tick now
  3. tail -f logs/selfheal.log
EOF
