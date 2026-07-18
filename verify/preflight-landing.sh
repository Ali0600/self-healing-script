#!/bin/bash
# Verify the Preflight landing page still lints and builds into a real page.
#
# Run from the repo-clone root. Requires deps installed (the orchestrator's
# setup_cmds handle that).
#
# "next build exited 0" is not the outcome that matters — the page rendering is.
# A failed build can also leave a PREVIOUS run's prerendered HTML on disk, so
# asserting on that file without clearing it first would pass while the site is
# broken. Wipe the output, rebuild, then assert the page is substantive.
set -euo pipefail

rm -rf .next

npm run lint
npm run build

node <<'JS'
const fs = require("fs");

const page = ".next/server/app/index.html";
if (!fs.existsSync(page)) {
  console.error(`VERIFY failed: ${page} was not generated`);
  process.exit(1);
}

const html = fs.readFileSync(page, "utf8");
const hasMain = html.includes("<main");
const hasBrand = html.includes("Preflight");
const bigEnough = html.length > 20000;

console.log(
  `VERIFY bytes=${html.length} main=${hasMain} brand=${hasBrand}`
);
process.exit(bigEnough && hasMain && hasBrand ? 0 : 1);
JS
