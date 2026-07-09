#!/usr/bin/env bash
#
# poll.sh — launchd entrypoint for the self-heal poller.
#
# launchd starts jobs with a minimal environment (no shell profile), so tools
# installed via Homebrew (`gh`, `pnpm`, `fnm`) and user CLIs (`claude`) are not
# on PATH, and fnm's session-scoped multishell node path does not exist at all.
# Rebuild PATH from stable locations first, then hand off to the orchestrator.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- make tools resolvable under launchd's minimal env ---
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
# Activate the default node via fnm; fall back to the newest installed version
# so a node upgrade doesn't break the schedule.
if command -v fnm >/dev/null 2>&1; then eval "$(fnm env)"; fnm use default >/dev/null 2>&1 || true; fi
NODE_BIN="$(ls -d "$HOME"/.local/share/fnm/node-versions/*/installation/bin 2>/dev/null | sort -V | tail -1 || true)"
[ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"

# Keep launchd's captured stdout/stderr from growing unbounded.
for f in "$ROOT/logs/launchd.out.log" "$ROOT/logs/launchd.err.log"; do
  if [ -f "$f" ] && [ "$(stat -f%z "$f" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    : > "$f"
  fi
done

exec /usr/bin/python3 "$ROOT/bin/selfheal.py" poll
