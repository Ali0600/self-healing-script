#!/bin/bash
# Verify the clothing-sales-tracker Uniqlo scraper extracts real products
# from the live site.
#
# Run from the repo-clone root. Requires: pnpm install + playwright chromium
# already done (the orchestrator's setup_cmds handle that).
#
# Freshness matters: data/uniqlo-de-men.json is committed to the repo, so a
# products.length check alone would pass on a STALE snapshot even if the
# scraper is still broken. Assert scrapedAt is newer than this run's start.
set -euo pipefail

pnpm -r typecheck

T0="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
pnpm scrape:uniqlo

T0="$T0" node <<'JS'
const fs = require("fs");
const t0 = Date.parse(process.env.T0);
const snap = JSON.parse(fs.readFileSync("data/uniqlo-de-men.json", "utf8"));
const n = (snap.products || []).length;
// 5s slack: scrapedAt is written mid-run, T0 taken just before launch.
const fresh = Date.parse(snap.scrapedAt) >= t0 - 5000;
console.log(`VERIFY products=${n} scrapedAt=${snap.scrapedAt} fresh=${fresh}`);
process.exit(n > 0 && fresh ? 0 : 1);
JS
