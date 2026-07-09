#!/bin/bash
# Verify grocery-helper scrapers extract REAL data from the live sources.
#
# Run from the repo-clone root. Requires: backend/.venv already installed
# (the orchestrator's setup_cmds handle that) and SCRAPE_PLZ in the env.
#
# The scrapers silently fall back to hardcoded sample data per source when an
# upstream is unreachable, so "the script exited 0" is NOT success. Real data
# is asserted structurally: sample stores carry a " (sample)" name suffix and
# sample offers have NULL raw_payload.
set -euo pipefail

cd backend

# Fresh DB so every row we assert on came from THIS scrape run.
rm -f grocery.db

# Migrations normally run at FastAPI startup (app/main.py), not in the scrape
# script — a fresh DB needs them applied explicitly first.
.venv/bin/python -c "from app.migrations import run_migrations; run_migrations()"

.venv/bin/python -m app.scripts.scrape --plz "${SCRAPE_PLZ:?SCRAPE_PLZ is required}"

.venv/bin/python - <<'PY'
import sqlite3
import sys

db = sqlite3.connect("grocery.db")
offers = db.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
sample_stores = db.execute(
    "SELECT COUNT(*) FROM stores WHERE name LIKE '%(sample)%'"
).fetchone()[0]
null_raw = db.execute(
    "SELECT COUNT(*) FROM offers WHERE raw_payload IS NULL"
).fetchone()[0]

print(f"VERIFY offers={offers} sample_stores={sample_stores} null_raw_payload={null_raw}")
ok = offers > 0 and sample_stores == 0 and null_raw == 0
sys.exit(0 if ok else 1)
PY
