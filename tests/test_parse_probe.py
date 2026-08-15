#!/usr/bin/env python3
"""Tests for the auth-probe judge, run with stdlib unittest (no deps, py3.9).

    python3 tests/test_parse_probe.py

The envelopes below are REAL output captured from `claude -p --output-format json`
on 2026-08-15 — a live expired-OAuth failure and a healthy success — so these
fixtures pin the actual contract, not a guess at it.
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "selfheal", Path(__file__).resolve().parent.parent / "bin" / "selfheal.py"
)
selfheal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(selfheal)

EXPIRED_OAUTH = json.dumps({
    "type": "result",
    "is_error": True,
    "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
    "total_cost_usd": 0,
    "num_turns": 1,
})

SUCCESS = json.dumps({
    "type": "result",
    "is_error": False,
    "result": "OK",
    "total_cost_usd": 0.0032,
    "num_turns": 1,
})

# The stderr that misled the healer for three hours: an unrelated user SessionEnd hook
# blowing its budget while the turn itself was fine.
HOOK_NOISE = (
    'SessionEnd hook [cd "/Users/ah/projects/ai-project-dashboard" && '
    "npx tsx scripts/flag-hook.ts] failed: Hook cancelled"
)


class ParseProbeTest(unittest.TestCase):
    def test_expired_oauth_is_a_failure_quoting_the_envelope(self):
        ok, message = selfheal.parse_probe(EXPIRED_OAUTH, "", 1)
        self.assertFalse(ok)
        self.assertIn("OAuth session expired", message)
        self.assertTrue(selfheal.auth_is_expired(message))

    def test_success_with_nonzero_exit_from_hook_noise_still_passes(self):
        """A cancelled user hook must not read as broken auth (the actual incident)."""
        ok, message = selfheal.parse_probe(SUCCESS, HOOK_NOISE, 1)
        self.assertTrue(ok, "envelope says the turn succeeded; exit code is not the verdict")
        self.assertEqual(message, "OK")

    def test_hook_noise_is_never_reported_as_the_auth_failure(self):
        """Whatever we tell the user, it must not be the innocent hook's text."""
        _, message = selfheal.parse_probe(EXPIRED_OAUTH, HOOK_NOISE, 1)
        self.assertNotIn("flag-hook", message)
        self.assertNotIn("SessionEnd", message)

    def test_plain_success(self):
        ok, message = selfheal.parse_probe(SUCCESS, "", 0)
        self.assertTrue(ok)
        self.assertEqual(message, "OK")

    def test_unparseable_stdout_fails_closed_and_surfaces_stderr(self):
        ok, message = selfheal.parse_probe("not json at all", "spawn ENOENT", 127)
        self.assertFalse(ok)
        self.assertIn("no JSON envelope", message)
        self.assertIn("ENOENT", message)

    def test_empty_output_fails_closed(self):
        ok, _ = selfheal.parse_probe("", "", 0)
        self.assertFalse(ok, "no envelope must never read as healthy")

    def test_auth_expiry_detection_does_not_fire_on_unrelated_errors(self):
        self.assertFalse(selfheal.auth_is_expired("Usage limit reached"))
        self.assertFalse(selfheal.auth_is_expired("probe produced no JSON envelope"))
        self.assertTrue(selfheal.auth_is_expired(
            "Failed to authenticate: OAuth session expired and could not be refreshed"))



class AgentEnvTest(unittest.TestCase):
    """The env builder shared by the auth probe AND every heal session.

    Both call sites use this one function on purpose — if they built env separately,
    the probe could prove an auth that heals don't actually use.
    """

    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_stored_token_is_injected(self):
        env = selfheal.agent_env(token="sk-ant-oat-TESTVALUE")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat-TESTVALUE")

    def test_no_token_means_variable_absent(self):
        """Without a stored token the CLI must fall back to its own keychain session."""
        env = selfheal.agent_env(token=None)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_inherited_token_never_survives_untouched(self):
        """A token from THIS session must not leak into the healer's child process."""
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "inherited-from-parent"
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", selfheal.agent_env(token=None))
        self.assertEqual(
            selfheal.agent_env(token="stored")["CLAUDE_CODE_OAUTH_TOKEN"], "stored")

    def test_inherited_api_key_is_always_stripped(self):
        """An API key outranks subscription auth and would bill per token — never inherit."""
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api-should-not-leak"
        for token in (None, "sk-ant-oat-TESTVALUE"):
            self.assertNotIn("ANTHROPIC_API_KEY", selfheal.agent_env(token=token))

    def test_unrelated_env_is_preserved(self):
        os.environ["PATH"] = "/usr/bin:/bin"
        self.assertEqual(selfheal.agent_env(token=None)["PATH"], "/usr/bin:/bin")


class AuthAdviceTest(unittest.TestCase):
    def test_advice_differs_by_credential_and_names_the_right_command(self):
        with_token = selfheal.auth_expired_advice("sk-ant-oat-x", "OAuth session expired")
        self.assertIn("setup-token", with_token)
        self.assertIn("set-token", with_token)

        without = selfheal.auth_expired_advice(None, "OAuth session expired")
        self.assertIn("re-login", without)
        self.assertNotEqual(with_token, without)

    def test_labels_name_the_active_source(self):
        self.assertIn("token", selfheal.auth_source_label("sk-ant-oat-x"))
        self.assertIn("interactive", selfheal.auth_source_label(None))

if __name__ == "__main__":
    unittest.main(verbosity=2)
