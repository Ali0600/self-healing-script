# Learnings

Teachable concepts that came up while building this project.

## A subprocess's exit code answers a different question than its output

One-line explanation: `claude -p` exits non-zero for anything that went wrong
in the run — including a user hook that got cancelled — while the *outcome of
the turn* lives in the JSON envelope's `is_error` / `result`.

**Why it came up:** the poller's auth probe judged `returncode` and quoted
`stderr`, so when the OAuth session expired the log blamed an unrelated
dashboard SessionEnd hook (whose 1.5s budget the probe's short session blew).
Three hours of ticks pointed at an innocent file. The fix reads the envelope;
a non-zero exit with a successful envelope now logs a warning instead of
failing the healer, and the probe runs with `--settings '{"disableAllHooks":
true}'` so unrelated hooks can't taint the measurement.

**Takeaway:** when a tool reports both a status code and structured output,
decide which one *answers your question* and assert on that — the other is a
proxy that will eventually disagree with it.

## A repeated alert is a silenced alert

One-line explanation: an unattended job that notifies on every failed tick
sends the same message dozens of times a day, training the user to ignore it.

**Why it came up:** the expired login failed every 30 min; without backoff
that's 48 identical macOS notifications daily. Now the failure is *logged*
every tick but *announced* only when its signature changes or 12h pass, and
the record is deleted on any healthy tick so the next incident is immediate.

**Takeaway:** separate the log (every occurrence, for debugging) from the
alert (state changes, for humans) — and key the alert on a signature so a
genuinely new failure still interrupts immediately.

## Claude Code headless mode (`claude -p`) as an automation primitive

One-line explanation: `claude -p "<prompt>"` runs a full agentic Claude Code
session non-interactively — tool allowlists, permission modes, JSON result
envelope — authenticated via the Mac's keychain OAuth (subscription), no API
key needed.

**Why it came up:** the whole healer is built around it: the orchestrator
renders a prompt, runs `claude -p --permission-mode acceptEdits
--allowedTools "…" --output-format json`, and parses the JSON envelope
(`result`, `is_error`) for a machine-readable verdict.

**Takeaway:** treat a headless LLM session like any other subprocess — scope
its tools, cap its wall clock from outside (this CLI version has no
`--max-turns`), and parse a structured output contract, never prose.

## Keychain-authenticated CLIs under launchd need an in-context auth proof

One-line explanation: launchd agents run outside your login shell but inside
your user GUI session — keychain-backed CLIs (`claude`, `gh`) *usually* work,
but nothing guarantees it until proven in that exact context.

**Why it came up:** the poller's first run deletes nothing and assumes
nothing: it performs a tiny `claude -p "reply OK"` probe and a `gh auth
status` from *inside the launchd job* before any heal is trusted, and the
rollout re-ran the probe after `launchctl kickstart` specifically.

**Takeaway:** for any scheduled job, prove each credential path
non-interactively in the scheduler's own environment before trusting the
schedule — an interactive-terminal success proves nothing about launchd.

## A repo you can't `pip install -r` fresh is already broken

One-line explanation: dependency floors can silently outgrow the interpreter
your dev venv runs — the existing venv keeps working on old installs while a
fresh clone can no longer be set up at all.

**Why it came up:** the healer's first `doctor` run built grocery-helper's
venv from scratch on Python 3.9 (matching the documented dev setup) and
`fastapi>=0.138.1` refused to resolve — the repo's own dev venv only works
because it predates the floor bump. The healer now builds on Homebrew 3.12
(matching CI/prod).

**Takeaway:** "works in my venv" ≠ "installable" — a fresh-environment
install (exactly what CI, a new machine, or an automation clone does) is the
real test of a requirements file.

## GitHub as a durable state machine for automation

One-line explanation: labeled issues as a work queue, hidden HTML marker
comments as an attempt ledger, `Fixes #n` + a success-run auto-close step as
loop termination — no local database to lose.

**Why it came up:** the poller needed attempt counts and cooldowns that
survive reboots/reinstalls, stay human-inspectable, and dedupe across
machines; issue comments carry `<!-- self-heal {"attempt":1,…} -->` markers
that the orchestrator parses back.

**Takeaway:** for small automation, prefer state that lives where humans
already look (the issue thread) over hidden local files — it's durable,
auditable, and debuggable with zero extra tooling.

## Prompt injection shapes tool allowlists, not just prompts

One-line explanation: an agent that ingests untrusted web content (scraped
HTML, CI logs) must be assumed steerable — so the boundary is what its tools
*can reach*, not what the prompt asks it to do.

**Why it came up:** the healer feeds failure artifacts with raw
`htmlSnippet`s into the agent. Its allowlist therefore has no `gh` and no
`git push` — publishing is done by the deterministic orchestrator after an
independent verify and a mechanical diff-scope check (`.github/**` always
rejected).

**Takeaway:** put the security boundary one layer outside the LLM:
credential-free tool allowlists, supervisor-owned side effects, and
mechanical post-hoc checks — the prompt's rules are guidance, not
enforcement.
