#!/usr/bin/env bash
# Remove the launchd agent (leaves the repo, logs, and clones in place).
set -euo pipefail

PLIST_LABEL="com.selfheal.poller"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
rm -f "$PLIST_DST"
echo "→ $PLIST_LABEL unloaded and plist removed"
