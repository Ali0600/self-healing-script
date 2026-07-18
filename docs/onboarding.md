# Onboarding a repo

Adding a repo to the self-healing pipeline takes three pieces — no changes to
the orchestrator itself. It does not have to be a scraper: anything whose
breakage a CI job can detect and a script can verify works the same way (the
Preflight landing page is onboarded on its lint + build).

## 1. Make the repo announce failures (one PR)

In the repo's scheduled-scrape workflow:

- add `issues: write` to the workflow `permissions:`
- paste [`alert-on-failure.snippet.yml`](workflows/alert-on-failure.snippet.yml)
  after the scrape steps — on failure it opens/updates ONE issue labeled
  `scrape-failure`
- paste [`close-on-success.snippet.yml`](workflows/close-on-success.snippet.yml)
  — a healthy run closes the issue again (outages that fix themselves end the
  loop with zero commits)

## 2. Write a verify script (`verify/<name>.sh`)

The contract:

- runs from the repo-clone root, exits 0 **only if real data was extracted**
- asserts the *outcome*, not a proxy: fresh rows/records with the shape you
  expect, not "the process exited 0" (watch out for sample-data fallbacks and
  stale committed snapshots — assert freshness/authenticity structurally)
- personal values (postal codes, tokens) come from env vars, injected at heal
  time from the gitignored `config.json` (`verify_env`)
- no secrets and no personal values inside the committed script

Both the healing agent (to iterate) and the orchestrator (as the authoritative
gate) run exactly this script.

## 3. Add a config block (`config.json`)

Copy an entry in `config.example.json` and fill in:

| key | what it does |
|---|---|
| `slug`, `default_branch`, `workflow_file` | where to poll issues + fetch failed-run logs |
| `failure_label` | the label this repo's alert step applies (`scrape-failure` by default, e.g. `build-failure` for a site). `install.sh` creates whichever label is configured — keep it identical in the workflow snippet and here, or the alert and the poller talk past each other |
| `purpose`, `repo_notes` | injected into the heal prompt — say what the scraper does and every known gotcha (fallback behavior, env-specific failures) |
| `setup_cmds` | make a fresh clone runnable (venv/pnpm install); cached between heals |
| `verify_script`, `verify_env`, `success_criteria` | the gate (see above) |
| `heal_hint_paths` | files the agent should look at first |
| `allowed_change_globs` | the ONLY paths a fix may touch — anything else is discarded |
| `ignore_globs` | build/data artifacts the scrape itself produces (never committed) |
| `allowed_tools` | Claude Code tool allowlist; keep Bash patterns scoped, never include `gh` or `git push` |
| `max_attempts`, `cooldown_hours`, `claude_timeout_minutes` | pacing + budget |

Then run:

```bash
./install.sh                    # creates labels in the new repo, reloads agent
python3 bin/selfheal.py doctor  # proves clone + setup + verify green on unbroken main
```

## 4. Fire drill (do not skip)

Prove the loop end-to-end before trusting it: break a selector on a branch or
`main`, dispatch the scrape workflow, watch the issue appear, then
`launchctl kickstart -k gui/$(id -u)/com.selfheal.poller` and follow
`logs/selfheal.log` until the fix PR appears. Also prove it **fails closed**:
point `verify_script` at `/usr/bin/false` once and confirm no PR gets opened.
