# self-healing-script

When a repo's CI breaks in a way nobody is watching — a scraped site changed
its markup, a half-finished refactor stopped the build — this pipeline
notices, runs a **headless Claude Code session on my Mac** to fix it, proves
the fix by running the real thing, and opens a pull request. I review and
merge; the issue closes itself.

```
 target repo (GitHub Actions)                     this Mac (launchd, every 30 min)
┌──────────────────────────────┐                 ┌────────────────────────────────────┐
│ scheduled job fails           │                 │ poller finds the issue             │
│   └─ opens issue              │   gh (poll)     │   └─ dedicated clone, fresh reset  │
│      label: <failure_label> ──┼────────────────▶│      └─ headless `claude -p`       │
│                               │                 │         fixes the break, iterates  │
│ next green run                │                 │         against verify script      │
│   └─ auto-closes the issue ◀──┼──┐              │      └─ orchestrator re-verifies   │
└──────────────────────────────┘  │              │         INDEPENDENTLY, then pushes │
                                   │  PR: Fixes #n│         branch + opens PR ─────────┼──▶ human review
                                   └──────────────┴────────────────────────────────────┘
```

Currently healing:

| repo | what breaks | failure label |
|---|---|---|
| [grocery-helper](https://github.com/Ali0600/grocery-helper) | JSON-endpoint scrapers | `scrape-failure` |
| [clothing-sales-tracker](https://github.com/Ali0600/clothing-sales-tracker) | Playwright DOM scraping | `scrape-failure` |
| [preflight-landing](https://github.com/Ali0600/preflight-landing) | Next.js lint + build | `build-failure` |

One config file, zero repo-specific code in the orchestrator — the landing
page was onboarded without touching `selfheal.py` at all, which is what
"generic" was supposed to mean.

## Why local, not CI?

clothing-sales-tracker's first healer ran inside GitHub Actions. Moving it
here bought three things:

- **Subscription auth, not API billing** — headless `claude -p` uses the
  Mac's keychain OAuth (Claude subscription) instead of an `ANTHROPIC_API_KEY`
  secret metered per token.
- **A residential German IP** — the target sites are `*.de` retailers;
  Actions runners come from Azure US/EU ranges that can see different regional
  content or hit bot walls. The fix is verified from the same vantage point a
  real user has.
- **One pipeline for every repo** — onboarding a new scraper is a config
  block + a verify script, not a copied workflow.

## How it works

`bin/selfheal.py poll` (launchd, every 30 min, at most **one heal per tick**):

1. **Find work** — open issues labeled `scrape-failure` in each configured
   repo (the repo's own scrape workflow opens/closes them; snippets in
   [`docs/workflows/`](docs/workflows)).
2. **Check eligibility** — skip if a fix PR is already open, the attempt
   budget is spent, or the cooldown hasn't elapsed. State lives in hidden
   `<!-- self-heal {...} -->` marker comments **on the issue itself** — GitHub
   is the durable state machine; the Mac can be wiped without losing history.
3. **Prepare** — dedicated clone under `work/` (never my working checkouts),
   hard-reset to `origin/main`, run the repo's `setup_cmds`.
4. **Gather evidence** — issue thread, `--log-failed` tail of the failing run,
   failure artifacts (e.g. clothing's `stage`/`expectedCount`/`htmlSnippet`).
5. **Heal** — headless `claude -p` with a rendered prompt and a **scoped tool
   allowlist**, wall-clock-bounded (`SIGTERM`→`SIGKILL` on the process group).
   The agent's loop: reproduce with the verify script *first*, edit minimally,
   re-verify, write `HEAL_REPORT.md`, end with a machine-readable verdict:
   `FIXED | CANNOT_REPRODUCE | SITE_DOWN | GAVE_UP`.
6. **Gate** — the orchestrator doesn't trust the agent: it re-runs the verify
   script itself, and diffs the working tree against `allowed_change_globs`
   (any out-of-scope edit — including anything under `.github/` — discards the
   whole heal).
7. **Publish** — branch `self-heal/issue-<n>-a<k>`, commit, push, PR with the
   heal report + verification output (`Fixes #n`). Merging deploys the fix and
   closes the issue; the next green scheduled scrape would close it too.
8. **Announce, always** — every outcome (success *or any failure mode*) posts
   an issue comment, fires a macOS notification, and logs. A degraded pipeline
   is never silent.

### Fail-closed by construction

The gate can't be talked into passing: a heal only publishes if the
**orchestrator's own** verify run exits 0. "Verify couldn't run" is a failure,
not a skip. Verify scripts assert *outcomes*, not proxies — grocery-helper's
scrapers fall back to sample data on error, so its script asserts zero
`(sample)` stores and non-NULL raw payloads; clothing's snapshot is committed,
so its script asserts `scrapedAt` freshness, not just row count.

### Security model

Scraped web content flows into an LLM's context, so the design assumes prompt
injection will eventually be attempted:

- the agent's allowlist has **no `gh`, no `git commit/push`** — publishing is
  done by the deterministic orchestrator, so page content can't reach repo
  credentials;
- out-of-scope diffs (workflows, lockfiles, anything outside the scraper
  globs) are rejected mechanically after the fact;
- default merge policy is **PR + human review** — the diff gets eyes before it
  ships (config supports `automerge` per repo for lower-stakes pipelines);
- residual risk stated honestly: scoped `Bash(pnpm:*)`-style patterns still
  execute code the repo itself runs; the boundaries that hold are
  credential-reach, diff scope, and review.

## Quick start

```bash
cp config.example.json config.json   # fill slugs, postal code, tuning
./install.sh                         # labels, plist, launchd bootstrap
python3 bin/selfheal.py doctor       # auth (incl. non-interactive keychain),
                                     # clones, setup, verify green on main
launchctl kickstart -k gui/$(id -u)/com.selfheal.poller   # force a tick
```

`config.json` is gitignored: postal codes, machine paths, and tuning stay off
GitHub (`config.example.json` carries neutral placeholders).

## Operations

```bash
python3 bin/selfheal.py status              # open issues, attempts, pending PRs
python3 bin/selfheal.py heal --repo NAME    # heal now (--force ignores cooldown)
python3 bin/selfheal.py doctor --fast       # skip the live verify runs
tail -f logs/selfheal.log                   # poller log (rotated)
ls logs/heal-*.json                         # full agent transcript envelopes
./uninstall.sh                              # unload the launchd agent
```

Onboarding another repo: [`docs/onboarding.md`](docs/onboarding.md) — a
workflow snippet, a verify script, a config block, and a mandatory fire drill
(prove the loop heals a deliberately broken selector **and** prove it opens no
PR when verification fails).

## Experience Gained

- Designed an **autonomous remediation pipeline**: CI failure detection →
  durable GitHub-issue queue → local LLM repair agent → independent
  verification gate → reviewed pull request, with attempt budgets, cooldowns,
  and every degradation announced (no silent failure modes).
- Ran **headless LLM agents (Claude Code) under macOS launchd**: keychain
  OAuth in a non-interactive session, TCC-safe working directories,
  PATH/version-manager reconstruction (fnm) in a minimal daemon environment,
  process-group timeouts for browser-spawning children.
- Used **GitHub as the state machine** — labeled issues as a work queue,
  hidden marker comments as an attempt ledger, `Fixes #n` linkage and
  success-run auto-close as loop termination — so the system survives machine
  wipes and stays human-inspectable.
- Applied **fail-closed gate design**: the supervisor re-verifies outcomes
  itself (never trusting agent claims), asserts data authenticity over exit
  codes, and mechanically rejects out-of-scope diffs.
- Bounded **prompt-injection blast radius** for an agent that ingests
  untrusted web content: credential-free tool allowlists,
  supervisor-owned publishing, scope-checked diffs, human review by default.

## Roadmap

- Onboard [grocery-price-history](https://github.com/Ali0600/grocery-price-history)
  (weekly snapshot scraper) and
  [macbook-pro-tracker](https://github.com/Ali0600/macbook-pro-tracker)
  (Kleinanzeigen deals).
- Per-repo model escalation (retry a failed heal on a stronger model).
- A `selfheal report` subcommand summarizing heal history from the marker
  ledger.
