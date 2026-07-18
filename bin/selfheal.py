#!/usr/bin/env python3
"""selfheal — generic self-healing pipeline for web-scraper repos.

When a repo's scheduled scrape fails in GitHub Actions, the workflow opens an
issue labeled `scrape-failure`. This orchestrator (run by launchd every 30 min)
finds such issues, runs a headless Claude Code session in a dedicated clone to
fix the broken extraction logic, independently re-verifies the fix against the
live site, and opens a PR that closes the issue. Every outcome — success or
any failure — is announced via an issue comment, a macOS notification, and the
log. Nothing is ever pushed to the default branch.

Python 3.9 stdlib only (runs on the stock macOS /usr/bin/python3).

Subcommands:
    poll                     one tick: scan repos, heal at most one issue
    heal --repo NAME         heal now (optionally --issue N, --force)
    doctor [--fast]          preflight: auth, config, clones, verify harness
    status                   show open issues / attempts / pending PRs
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import fnmatch
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
PROMPT_TEMPLATE = ROOT / "prompts" / "heal.md"
LOG_DIR = ROOT / "logs"
WORK_DIR = ROOT / "work"
STATE_DIR = ROOT / "state"
LOCK_PATH = STATE_DIR / "poller.lock"
CLAUDE_AUTH_MARKER = STATE_DIR / "claude-auth-ok"

MARKER_RE = re.compile(r"<!--\s*self-heal\s+(\{.*?\})\s*-->", re.S)
VERDICT_RE = re.compile(r"^VERDICT:\s*(FIXED|CANNOT_REPRODUCE|SITE_DOWN|GAVE_UP)\s*$", re.M)

# Verdicts the agent may declare, plus orchestrator-assigned failure verdicts.
FAILURE_HINTS = {
    "SETUP_FAILED": "installing the repo's dependencies failed",
    "CONTEXT_FAILED": "could not gather failure context from GitHub",
    "TIMEOUT": "the healing session exceeded its wall-clock budget and was killed",
    "AGENT_ERROR": "the headless Claude session errored (possibly a usage limit)",
    "GAVE_UP": "the agent could not determine a safe fix",
    "CANNOT_REPRODUCE": "the failure did not reproduce locally — likely environmental "
    "(production-only, regional content, or transient)",
    "SITE_DOWN": "the target site is unreachable or blocking automation",
    "SCOPE_VIOLATION": "the agent edited files outside the allowed scope; changes discarded",
    "NO_CHANGES": "the agent claimed FIXED but changed nothing",
    "VERIFY_FAILED": "the independent verification run failed — fix not proven",
    "PUSH_FAILED": "pushing the fix branch or opening the PR failed",
}

log = logging.getLogger("selfheal")


# ---------------------------------------------------------------- utilities


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(LOG_DIR / "selfheal.log", maxBytes=2_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(ts: str) -> dt.datetime:
    # gh emits e.g. 2026-07-09T04:12:45Z; py3.9 fromisoformat can't parse "Z".
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def notify(message: str, title: str = "Self-heal") -> None:
    """macOS notification; best-effort (never lets a UI failure mask the log)."""
    try:
        script = 'display notification {} with title {} sound name "Ping"'.format(
            json.dumps(message[:180]), json.dumps(title)
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        log.warning("notification failed: %s", exc)


def run_cmd(
    cmd: List[str],
    cwd: Optional[Path] = None,
    timeout: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    shown = " ".join(shlex.quote(c)[:80] for c in cmd[:8])
    log.info("$ %s%s", shown, " …" if len(cmd) > 8 else "")
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        timeout=timeout, env=env,
    )


def gh(args: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return run_cmd(["gh"] + args, timeout=timeout)


def gh_json(args: List[str], timeout: int = 120) -> Any:
    proc = gh(args, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:4])} failed: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout or "null")


def tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])


# ------------------------------------------------------------------- config


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    defaults = raw.get("defaults", {})
    repos = []
    for repo in raw["repos"]:
        merged = {**defaults, **repo}
        for key in ("name", "slug", "default_branch", "workflow_file",
                    "verify_script", "allowed_change_globs", "allowed_tools"):
            if key not in merged:
                raise ValueError(f"repo {repo.get('name', '?')}: missing config key '{key}'")
        repos.append(merged)
    return {"defaults": defaults, "repos": repos}


def claude_bin(cfg: Dict[str, Any]) -> str:
    path = os.path.expanduser(cfg.get("claude_bin", "~/.local/bin/claude"))
    if os.path.exists(path):
        return path
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("claude CLI not found")
    return found


# ------------------------------------------------- GitHub issue state layer


def open_failure_issues(repo: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues = gh_json([
        "issue", "list", "--repo", repo["slug"],
        "--label", repo.get("failure_label", "scrape-failure"),
        "--state", "open",
        "--json", "number,title,body,createdAt,comments",
    ])
    return sorted(issues or [], key=lambda i: i["createdAt"])


def parse_markers(issue: Dict[str, Any]) -> List[Dict[str, Any]]:
    markers = []
    for comment in issue.get("comments", []):
        for blob in MARKER_RE.findall(comment.get("body", "")):
            try:
                markers.append(json.loads(blob))
            except json.JSONDecodeError:
                log.warning("unparseable self-heal marker on issue: %.80s", blob)
    return markers


def heal_pr_open(repo: Dict[str, Any], issue_number: int) -> Optional[str]:
    prs = gh_json([
        "pr", "list", "--repo", repo["slug"], "--state", "open",
        "--json", "headRefName,url",
    ])
    prefix = f"self-heal/issue-{issue_number}-"
    for pr in prs or []:
        if pr["headRefName"].startswith(prefix):
            return pr["url"]
    return None


def post_issue_comment(repo: Dict[str, Any], issue_number: int, body: str) -> None:
    comment_file = STATE_DIR / "comment.md"
    comment_file.write_text(body, encoding="utf-8")
    proc = gh([
        "issue", "comment", str(issue_number), "--repo", repo["slug"],
        "--body-file", str(comment_file),
    ])
    if proc.returncode != 0:
        log.error("posting issue comment failed: %s", proc.stderr.strip()[:300])


def marker_blob(attempt: int, verdict: str, pr_url: Optional[str] = None) -> str:
    payload: Dict[str, Any] = {
        "v": 1, "attempt": attempt, "ts": now_utc().isoformat(), "verdict": verdict,
    }
    if pr_url:
        payload["pr"] = pr_url
    return f"<!-- self-heal {json.dumps(payload)} -->"


class Eligibility:
    def __init__(self, ok: bool, reason: str, attempts: int = 0):
        self.ok = ok
        self.reason = reason
        self.attempts = attempts


def check_eligibility(repo: Dict[str, Any], issue: Dict[str, Any], force: bool,
                      announce: bool = True) -> Eligibility:
    """announce=False for read-only callers (status) — never posts comments."""
    markers = parse_markers(issue)
    attempts = len([m for m in markers if m.get("verdict") != "EXHAUSTED"])
    max_attempts = int(repo.get("max_attempts", 3))
    number = issue["number"]

    pr_url = heal_pr_open(repo, number)
    if pr_url:
        return Eligibility(False, f"fix PR already open, awaiting review: {pr_url}", attempts)

    if force:
        return Eligibility(True, "forced", attempts)

    if attempts >= max_attempts:
        if announce and not any(m.get("verdict") == "EXHAUSTED" for m in markers):
            post_issue_comment(
                repo, number,
                f"🛑 Self-heal gave up after {attempts} attempt(s) — a human needs to look "
                f"at this one.\n\n{marker_blob(attempts, 'EXHAUSTED')}",
            )
            notify(f"{repo['name']}: self-heal exhausted after {attempts} attempts — needs you")
        return Eligibility(False, f"attempts exhausted ({attempts}/{max_attempts})", attempts)

    stamps = [parse_iso(m["ts"]) for m in markers if m.get("ts")]
    if stamps:
        cooldown = dt.timedelta(hours=float(repo.get("cooldown_hours", 6)))
        remaining = max(stamps) + cooldown - now_utc()
        if remaining > dt.timedelta(0):
            return Eligibility(
                False,
                f"cooling down, next attempt in {int(remaining.total_seconds()) // 60} min",
                attempts,
            )

    return Eligibility(True, "eligible", attempts)


# ------------------------------------------------------------ heal pipeline


def prepare_clone(repo: Dict[str, Any]) -> Path:
    WORK_DIR.mkdir(exist_ok=True)
    clone = WORK_DIR / repo["name"]
    branch = repo["default_branch"]
    if not (clone / ".git").exists():
        proc = gh(["repo", "clone", repo["slug"], str(clone)], timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"clone failed: {proc.stderr.strip()[:300]}")
    for cmd in (
        ["git", "fetch", "origin"],
        ["git", "checkout", branch],
        ["git", "reset", "--hard", f"origin/{branch}"],
        ["git", "clean", "-fd"],  # deliberately not -x: keep .venv/node_modules caches
    ):
        proc = run_cmd(cmd, cwd=clone, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()[:300]}")
    selfheal_dir = clone / ".selfheal"
    shutil.rmtree(selfheal_dir, ignore_errors=True)
    selfheal_dir.mkdir()
    return clone


def run_setup(repo: Dict[str, Any], clone: Path) -> None:
    timeout = int(repo.get("setup_timeout_minutes", 10)) * 60
    for cmd in repo.get("setup_cmds", []):
        proc = run_cmd(["bash", "-c", cmd], cwd=clone, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(
                f"setup command failed: {cmd}\n{tail(proc.stderr or proc.stdout, 30)}"
            )


def gather_context(repo: Dict[str, Any], issue: Dict[str, Any], clone: Path) -> Dict[str, str]:
    """Collect failure evidence: issue text, failed-run log tail, artifacts."""
    comments = [
        c["body"] for c in issue.get("comments", []) if not MARKER_RE.search(c.get("body", ""))
    ]
    issue_context = issue.get("body") or "(no body)"
    if comments:
        issue_context += "\n\nRecent comments:\n" + "\n---\n".join(comments[-3:])

    run_log, run_url = "(no failed run found)", ""
    try:
        runs = gh_json([
            "run", "list", "--repo", repo["slug"], "--workflow", repo["workflow_file"],
            "--status", "failure", "--limit", "1", "--json", "databaseId,url,createdAt",
        ])
        if runs:
            run_id, run_url = str(runs[0]["databaseId"]), runs[0]["url"]
            proc = gh(["run", "view", run_id, "--repo", repo["slug"], "--log-failed"],
                      timeout=180)
            if proc.returncode == 0 and proc.stdout.strip():
                run_log = tail(proc.stdout, int(repo.get("log_tail_lines", 150)))
            if repo.get("download_artifacts"):
                art_dir = clone / ".selfheal" / "artifacts"
                gh(["run", "download", run_id, "--repo", repo["slug"],
                    "--pattern", repo.get("artifact_pattern", "*"),
                    "--dir", str(art_dir)], timeout=180)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        log.warning("failure-context gathering degraded: %s", exc)
        run_log = f"(could not fetch run log: {exc})"

    artifacts = []
    for path in sorted((clone / ".selfheal" / "artifacts").rglob("*.failure.json")):
        artifacts.append(f"`{path.name}`:\n```json\n{path.read_text()[:6000]}\n```")
    artifacts_text = "\n\n".join(artifacts) if artifacts else "(none)"

    return {"issue_context": issue_context, "run_log": run_log,
            "run_url": run_url, "artifacts": artifacts_text}


def render_verify(repo: Dict[str, Any], clone: Path) -> Path:
    """Compose .selfheal/verify.sh = env exports (personal values) + committed script."""
    script_src = ROOT / repo["verify_script"]
    exports = "".join(
        f"export {key}={shlex.quote(str(val))}\n"
        for key, val in repo.get("verify_env", {}).items()
    )
    body = script_src.read_text(encoding="utf-8")
    rendered = clone / ".selfheal" / "verify.sh"
    rendered.write_text(
        "#!/bin/bash\n# Rendered by selfheal — env from config.json + "
        f"{repo['verify_script']}\n{exports}\n{body}",
        encoding="utf-8",
    )
    rendered.chmod(0o755)
    return rendered


def render_prompt(repo: Dict[str, Any], issue: Dict[str, Any], ctx: Dict[str, str],
                  clone: Path) -> str:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    hints = "\n".join(f"- `{p}`" for p in repo.get("heal_hint_paths", [])) or "(explore)"
    replacements = {
        "{{REPO_NAME}}": repo["name"],
        "{{SLUG}}": repo["slug"],
        "{{DEFAULT_BRANCH}}": repo["default_branch"],
        "{{PURPOSE}}": repo.get("purpose", ""),
        "{{REPO_NOTES}}": repo.get("repo_notes", ""),
        "{{ISSUE_NUMBER}}": str(issue["number"]),
        "{{ISSUE_TITLE}}": issue.get("title", ""),
        "{{ISSUE_CONTEXT}}": ctx["issue_context"][:4000],
        "{{RUN_LOG_TAIL}}": ctx["run_log"][:12000],
        "{{ARTIFACTS}}": ctx["artifacts"][:20000],
        "{{HINT_PATHS}}": hints,
        "{{ALLOWED_CHANGE_GLOBS}}": ", ".join(f"`{g}`" for g in repo["allowed_change_globs"]),
        "{{SUCCESS_CRITERIA}}": repo.get("success_criteria", "the verify script exits 0"),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    (clone / ".selfheal" / "prompt.md").write_text(template, encoding="utf-8")
    return template


def agent_env() -> Dict[str, str]:
    """Clean env: subscription keychain OAuth, never an inherited API key,
    and no nested-session vars leaking in when run manually from a Claude session."""
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE")):
            env.pop(key)
    return env


def run_agent(repo: Dict[str, Any], cfg: Dict[str, Any], prompt: str, clone: Path,
              tag: str) -> Tuple[str, str]:
    """Run headless Claude Code. Returns (verdict, result_text)."""
    cmd = [
        claude_bin(cfg), "-p", prompt,
        "--permission-mode", "acceptEdits",
        "--allowedTools", ",".join(repo["allowed_tools"]),
        "--model", repo.get("model", "sonnet"),
        "--output-format", "json",
    ]
    if repo.get("fallback_model"):
        cmd += ["--fallback-model", repo["fallback_model"]]
    timeout = int(repo.get("claude_timeout_minutes", 30)) * 60
    log.info("running headless claude (model=%s, timeout=%ss)", repo.get("model", "sonnet"),
             timeout)
    proc = subprocess.Popen(
        cmd, cwd=str(clone), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, env=agent_env(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("claude session exceeded %ss — killing process group", timeout)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return "TIMEOUT", ""

    envelope_path = LOG_DIR / f"heal-{tag}.json"
    envelope_path.write_text(
        json.dumps({"stdout": stdout, "stderr": tail(stderr or "", 100)}), encoding="utf-8"
    )
    log.info("agent transcript envelope: %s", envelope_path)

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        log.error("agent produced no JSON envelope (rc=%s): %.200s", proc.returncode, stderr)
        return "AGENT_ERROR", ""
    result_text = envelope.get("result") or ""
    if envelope.get("is_error"):
        log.error("agent reported error: %.300s", result_text)
        return "AGENT_ERROR", result_text
    matches = VERDICT_RE.findall(result_text)
    if not matches:
        log.error("agent output missing VERDICT line — treating as GAVE_UP")
        return "GAVE_UP", result_text
    return matches[-1], result_text


def classify_changes(repo: Dict[str, Any], clone: Path) -> Tuple[List[str], List[str]]:
    """Split working-tree changes into (candidates, violations) — ignores ignore_globs."""
    proc = run_cmd(["git", "status", "--porcelain"], cwd=clone, timeout=60)
    ignore = list(repo.get("ignore_globs", [])) + [".selfheal/*", "HEAL_REPORT.md"]
    allowed = repo["allowed_change_globs"]
    candidates, violations = [], []
    for line in proc.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if any(fnmatch.fnmatch(path, g) for g in ignore):
            continue
        if path.startswith(".github/") or path.startswith(".git/"):
            violations.append(path)
        elif any(fnmatch.fnmatch(path, g) for g in allowed):
            candidates.append(path)
        else:
            violations.append(path)
    return candidates, violations


def reset_clone(clone: Path, branch: str) -> None:
    run_cmd(["git", "checkout", branch], cwd=clone, timeout=60)
    run_cmd(["git", "reset", "--hard", f"origin/{branch}"], cwd=clone, timeout=60)
    run_cmd(["git", "clean", "-fd"], cwd=clone, timeout=60)


def independent_verify(repo: Dict[str, Any], clone: Path) -> Tuple[bool, str]:
    timeout = int(repo.get("verify_timeout_minutes", 15)) * 60
    try:
        proc = run_cmd(["bash", ".selfheal/verify.sh"], cwd=clone, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"verify timed out after {timeout}s"
    output = tail((proc.stdout or "") + "\n" + (proc.stderr or ""), 40)
    return proc.returncode == 0, output


def read_heal_report(clone: Path) -> str:
    report = clone / "HEAL_REPORT.md"
    if report.exists():
        return report.read_text(encoding="utf-8")[:4000]
    return ""


def summary_line(heal_report: str, issue_number: int) -> str:
    for line in heal_report.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("**"):
            return line[:72]
    return f"automated extraction fix for issue #{issue_number}"


def publish_fix(repo: Dict[str, Any], issue: Dict[str, Any], clone: Path, attempt: int,
                candidates: List[str], heal_report: str, verify_output: str,
                run_url: str) -> str:
    """Branch, commit (authored as the user, no co-author trailer), push, open PR."""
    number = issue["number"]
    branch = f"self-heal/issue-{number}-a{attempt}"
    summary = summary_line(heal_report, number)

    steps = [
        ["git", "checkout", "-B", branch],
        ["git", "add", "--"] + candidates,
        ["git", "commit", "-m", f"{repo.get('commit_prefix', 'fix')}: {summary}",
         "-m", f"Automated self-heal for the failed scheduled scrape.\n\nFixes #{number}"],
        ["git", "push", "-u", "origin", branch, "--force-with-lease"],
    ]
    for cmd in steps:
        proc = run_cmd(cmd, cwd=clone, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])} failed: {tail(proc.stderr, 15)}")

    pr_body = (
        f"Automated fix from the [self-healing pipeline]"
        f"(https://github.com/Ali0600/self-healing-script) — review the diff "
        f"before merging.\n\n"
        f"### Heal report\n\n{heal_report or '(agent wrote no report)'}\n\n"
        f"### Independent verification (orchestrator re-run)\n\n```\n{verify_output}\n```\n\n"
        f"Failed run: {run_url or '(unknown)'}\n\nFixes #{number}\n"
    )
    body_file = clone / ".selfheal" / "pr-body.md"
    body_file.write_text(pr_body, encoding="utf-8")
    proc = gh([
        "pr", "create", "--repo", repo["slug"], "--base", repo["default_branch"],
        "--head", branch, "--title", f"self-heal: {summary}",
        "--body-file", str(body_file),
    ], timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {tail(proc.stderr, 15)}")
    pr_url = proc.stdout.strip().splitlines()[-1]

    gh(["pr", "edit", pr_url, "--add-label", "self-heal"])  # best-effort
    if repo.get("merge_policy", "pr") == "automerge":
        proc = gh(["pr", "merge", pr_url, "--auto", "--squash"])
        if proc.returncode != 0:
            log.warning("enabling auto-merge failed: %s", tail(proc.stderr, 5))
    return pr_url


def heal_issue(repo: Dict[str, Any], cfg: Dict[str, Any], issue: Dict[str, Any],
               attempt: int) -> None:
    """Run the full heal pipeline for one issue. Always announces the outcome."""
    number = issue["number"]
    max_attempts = int(repo.get("max_attempts", 3))
    cooldown = repo.get("cooldown_hours", 6)
    tag = f"{repo['name']}-{number}-{now_utc().strftime('%Y%m%d-%H%M%S')}"
    log.info("=== healing %s#%s (attempt %s/%s) ===", repo["slug"], number, attempt,
             max_attempts)

    verdict, detail, pr_url, run_url = "SETUP_FAILED", "", None, ""
    clone: Optional[Path] = None
    try:
        clone = prepare_clone(repo)
        run_setup(repo, clone)

        verdict = "CONTEXT_FAILED"
        ctx = gather_context(repo, issue, clone)
        run_url = ctx["run_url"]
        render_verify(repo, clone)
        prompt = render_prompt(repo, issue, ctx, clone)

        verdict, agent_text = run_agent(repo, cfg, prompt, clone, tag)
        heal_report = read_heal_report(clone) if clone else ""
        detail = heal_report or agent_text[-1500:]

        if verdict == "FIXED":
            candidates, violations = classify_changes(repo, clone)
            if violations:
                log.error("scope violation: %s", violations)
                verdict, detail = "SCOPE_VIOLATION", f"out-of-scope paths: {violations}"
            elif not candidates:
                verdict = "NO_CHANGES"
            else:
                ok, verify_output = independent_verify(repo, clone)
                detail = f"{detail}\n\nverify output:\n{verify_output}"
                if not ok:
                    verdict = "VERIFY_FAILED"
                else:
                    try:
                        pr_url = publish_fix(repo, issue, clone, attempt, candidates,
                                             heal_report, verify_output, run_url)
                        verdict = "PR_OPENED"
                    except RuntimeError as exc:
                        verdict, detail = "PUSH_FAILED", str(exc)
    except RuntimeError as exc:
        detail = str(exc)
        log.error("heal aborted at %s: %s", verdict, detail)
    finally:
        if clone is not None:
            shutil.rmtree(clone / ".selfheal", ignore_errors=True)
            reset_clone(clone, repo["default_branch"])

    # --- announce, always: issue comment + notification + log -----------------
    if verdict == "PR_OPENED":
        comment = (
            f"🤖 Self-heal attempt {attempt}/{max_attempts} **opened a fix PR**: {pr_url}\n\n"
            f"The fix was verified locally against the live site by an independent re-run "
            f"of the verify script. Review and merge to close this issue.\n\n"
            f"{marker_blob(attempt, verdict, pr_url)}"
        )
        notify(f"{repo['name']}: fix PR ready — {pr_url}")
    else:
        hint = FAILURE_HINTS.get(verdict, "unexpected failure")
        retry = (
            f"Next automatic attempt after a {cooldown}h cooldown."
            if attempt < max_attempts
            else "That was the last automatic attempt — a human needs to take over."
        )
        excerpt = detail.strip()[:2500] or "(no diagnostic output)"
        comment = (
            f"🤖 Self-heal attempt {attempt}/{max_attempts} finished **without a fix** "
            f"(`{verdict}`: {hint}). {retry}\n\n"
            f"<details><summary>Diagnostics</summary>\n\n{excerpt}\n\n</details>\n\n"
            f"{marker_blob(attempt, verdict)}"
        )
        notify(f"{repo['name']}: heal attempt {attempt} → {verdict}")
    post_issue_comment(repo, issue["number"], comment)
    log.info("=== heal finished: %s ===", verdict)


# ------------------------------------------------------------- entrypoints


def preflight(cfg: Dict[str, Any]) -> None:
    proc = gh(["auth", "status"])
    if proc.returncode != 0:
        raise RuntimeError(f"gh auth failed: {tail(proc.stderr, 5)}")
    if not CLAUDE_AUTH_MARKER.exists():
        log.info("first run: proving claude keychain auth non-interactively…")
        proc = subprocess.run(
            [claude_bin(cfg), "-p", "Reply with exactly: OK", "--output-format", "json",
             "--model", "haiku"],
            capture_output=True, text=True, timeout=120, env=agent_env(),
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude non-interactive auth failed under this environment: "
                f"{tail(proc.stderr, 5)}"
            )
        CLAUDE_AUTH_MARKER.write_text(now_utc().isoformat(), encoding="utf-8")
        log.info("claude auth OK — marker written")


def cmd_poll(force: bool = False) -> int:
    STATE_DIR.mkdir(exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another selfheal run holds the lock — exiting")
        return 0
    try:
        cfg = load_config()
        preflight(cfg)
        for repo in cfg["repos"]:
            issues = open_failure_issues(repo)
            if not issues:
                log.info("%s: no open %s issues", repo["name"],
                         repo.get("failure_label", "scrape-failure"))
                continue
            for issue in issues:
                elig = check_eligibility(repo, issue, force)
                if not elig.ok:
                    log.info("%s#%s: skip — %s", repo["name"], issue["number"], elig.reason)
                    continue
                heal_issue(repo, cfg, issue, elig.attempts + 1)
                return 0  # at most one heal per tick
        log.info("tick complete — nothing to heal")
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level: announce, never die silently
        log.exception("poll tick failed")
        notify(f"selfheal poll failed: {exc}"[:180])
        return 1
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def cmd_heal(repo_name: str, issue_number: Optional[int], force: bool) -> int:
    cfg = load_config()
    repos = [r for r in cfg["repos"] if r["name"] == repo_name]
    if not repos:
        log.error("no repo named %r in config.json", repo_name)
        return 1
    repo = repos[0]
    preflight(cfg)
    issues = open_failure_issues(repo)
    if issue_number:
        issues = [i for i in issues if i["number"] == issue_number]
    if not issues:
        log.error("no matching open %s issue in %s",
                  repo.get("failure_label", "scrape-failure"), repo["slug"])
        return 1
    issue = issues[0]
    elig = check_eligibility(repo, issue, force)
    if not elig.ok:
        log.error("not eligible: %s (use --force to override)", elig.reason)
        return 1
    heal_issue(repo, cfg, issue, elig.attempts + 1)
    return 0


def cmd_doctor(fast: bool) -> int:
    failures: List[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    print("selfheal doctor")
    try:
        cfg = load_config()
        check("config.json parses + required keys", True,
              f"{len(cfg['repos'])} repo(s)")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        check("config.json parses + required keys", False, str(exc))
        return 1

    check("gh auth", gh(["auth", "status"]).returncode == 0)
    try:
        CLAUDE_AUTH_MARKER.unlink(missing_ok=True)  # force a fresh auth proof
        preflight(cfg)
        check("claude non-interactive auth (keychain)", True)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        check("claude non-interactive auth (keychain)", False, str(exc)[:200])

    for repo in cfg["repos"]:
        name = repo["name"]
        try:
            labels = gh_json(["label", "list", "--repo", repo["slug"], "--json", "name"])
            have = {l["name"] for l in labels or []}
            check(f"{name}: labels exist", {"scrape-failure", "self-heal"} <= have,
                  "run ./install.sh to create missing labels" if not
                  ({"scrape-failure", "self-heal"} <= have) else "")
        except RuntimeError as exc:
            check(f"{name}: labels exist", False, str(exc)[:150])
        try:
            clone = prepare_clone(repo)
            check(f"{name}: clone + reset", True, str(clone))
            proc = run_cmd(["git", "push", "--dry-run", "origin",
                            repo["default_branch"]], cwd=clone, timeout=60)
            check(f"{name}: push auth (dry-run)", proc.returncode == 0,
                  tail(proc.stderr, 3) if proc.returncode != 0 else "")
            if not fast:
                run_setup(repo, clone)
                check(f"{name}: setup_cmds", True)
                render_verify(repo, clone)
                ok, output = independent_verify(repo, clone)
                check(f"{name}: verify script on unbroken {repo['default_branch']}", ok,
                      output.splitlines()[-1] if output else "")
                reset_clone(clone, repo["default_branch"])
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            check(f"{name}: clone/setup/verify", False, str(exc)[:200])

    print(f"\ndoctor: {'all checks passed' if not failures else f'{len(failures)} FAILURE(S)'}")
    return 0 if not failures else 1


def cmd_status() -> int:
    cfg = load_config()
    for repo in cfg["repos"]:
        print(f"\n{repo['slug']}:")
        label = repo.get("failure_label", "scrape-failure")
        issues = open_failure_issues(repo)
        if not issues:
            print(f"  no open {label} issues")
            continue
        for issue in issues:
            elig = check_eligibility(repo, issue, force=False, announce=False)
            pr = heal_pr_open(repo, issue["number"])
            print(f"  #{issue['number']} {issue['title']!r}: attempts={elig.attempts}, "
                  f"{'PR: ' + pr if pr else elig.reason}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("poll", help="one poller tick (launchd entrypoint)")
    heal_parser = sub.add_parser("heal", help="heal one repo now")
    heal_parser.add_argument("--repo", required=True)
    heal_parser.add_argument("--issue", type=int, default=None)
    heal_parser.add_argument("--force", action="store_true",
                             help="ignore cooldown/attempt limits (verify gates still apply)")
    doctor_parser = sub.add_parser("doctor", help="preflight checks")
    doctor_parser.add_argument("--fast", action="store_true",
                               help="skip setup_cmds + live verify runs")
    sub.add_parser("status", help="show open issues / attempts / PRs")
    args = parser.parse_args()

    setup_logging()
    if args.command == "poll":
        return cmd_poll()
    if args.command == "heal":
        return cmd_heal(args.repo, args.issue, args.force)
    if args.command == "doctor":
        return cmd_doctor(args.fast)
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
