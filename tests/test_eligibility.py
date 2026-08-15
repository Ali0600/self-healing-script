"""Attempt accounting: a run that never evaluated the repo is not an attempt on it.

Run with:  /usr/bin/python3 -m unittest discover -s tests -v

Stdlib only, deliberately — selfheal.py imports nothing outside the standard library so it can
run under launchd on the system interpreter, and a test suite that needed a venv would be the
first thing to rot.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import selfheal  # noqa: E402


def _issue(*verdicts, age_h: int = 48, number: int = 1):
    """An issue carrying one self-heal marker comment per verdict, all `age_h` hours old.

    Old on purpose: a recent marker trips the cooldown branch first, which would make every
    case below read "not eligible" for a reason that has nothing to do with what is under test.
    """
    ts = (selfheal.now_utc() - dt.timedelta(hours=age_h)).isoformat()
    return {
        "number": number,
        "comments": [
            {"body": f'<!-- self-heal {{"v": 1, "attempt": {i + 1}, '
                     f'"ts": "{ts}", "verdict": "{v}"}} -->'}
            for i, v in enumerate(verdicts)
        ],
    }


REPO = {"name": "grocery-helper", "slug": "o/r", "max_attempts": 2, "cooldown_hours": 12}


class NeverEvaluatedIsNotAnAttempt(unittest.TestCase):
    def setUp(self):
        # check_eligibility shells out to `gh pr list`; nothing here is about PRs.
        patcher = mock.patch.object(selfheal, "heal_pr_open", return_value=None)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_the_2026_08_09_incident_does_not_repeat(self):
        """Two AGENT_ERRORs exhausted the budget and blamed a repo that was already fixed.

        Both runs died because the healer's own OAuth session had expired — neither read a
        line of the target repo. Counting them retired the budget without a single real
        attempt, then told a human the repo needed them.
        """
        elig = selfheal.check_eligibility(REPO, _issue("AGENT_ERROR", "AGENT_ERROR"),
                                          force=False, announce=False)
        self.assertTrue(elig.ok, f"should still be eligible, got: {elig.reason}")
        self.assertEqual(elig.attempts, 0, "a run that never evaluated is not an attempt")

    def test_real_attempts_still_exhaust_the_budget(self):
        """The correction must not become 'never give up' — GAVE_UP is a real verdict."""
        elig = selfheal.check_eligibility(REPO, _issue("GAVE_UP", "GAVE_UP"),
                                          force=False, announce=False)
        self.assertFalse(elig.ok)
        self.assertIn("exhausted", elig.reason)

    def test_a_mixed_history_counts_only_the_real_attempts(self):
        """The discriminator is the verdict, not the marker count."""
        elig = selfheal.check_eligibility(
            REPO, _issue("AGENT_ERROR", "GAVE_UP", "SETUP_FAILED"),
            force=False, announce=False)
        self.assertTrue(elig.ok, f"1 real attempt of 2 remains, got: {elig.reason}")
        self.assertEqual(elig.attempts, 1)

    def test_a_persistently_broken_healer_stops_and_blames_itself(self):
        """Not counting them cannot mean polling forever — but the escalation must name the
        HEALER. The whole failure in August was a true sentence about the wrong subject."""
        posted = []
        with mock.patch.object(selfheal, "post_issue_comment",
                               side_effect=lambda r, n, b: posted.append(b)), \
             mock.patch.object(selfheal, "notify"):
            elig = selfheal.check_eligibility(
                REPO, _issue(*(["AGENT_ERROR"] * 4)), force=False, announce=True)
        self.assertFalse(elig.ok)
        self.assertIn("healer", elig.reason)
        self.assertEqual(len(posted), 1)
        self.assertIn("could not RUN", posted[0])
        self.assertNotIn("a human needs to look at this one", posted[0])


class ProvenAuthExpires(unittest.TestCase):
    """The preflight was one-shot: once the marker existed it was never re-proven, so the
    check built to catch a dead credential went blind the moment it first passed."""

    def _marker(self, body):
        import tempfile
        d = tempfile.mkdtemp()
        p = Path(d) / "claude-auth-ok"
        if body is not None:
            p.write_text(body, encoding="utf-8")
        return mock.patch.object(selfheal, "CLAUDE_AUTH_MARKER", p)

    def test_a_marker_within_its_ttl_is_trusted(self):
        fresh = (selfheal.now_utc() - dt.timedelta(hours=1)).isoformat()
        with self._marker(fresh):
            self.assertFalse(selfheal.claude_auth_stale())

    def test_a_marker_past_its_ttl_is_re_proven(self):
        old = (selfheal.now_utc()
               - dt.timedelta(hours=selfheal.CLAUDE_AUTH_TTL_H + 1)).isoformat()
        with self._marker(old):
            self.assertTrue(selfheal.claude_auth_stale())

    def test_a_missing_or_unreadable_marker_fails_toward_re_proving(self):
        with self._marker(None):
            self.assertTrue(selfheal.claude_auth_stale())
        with self._marker("not a timestamp"):
            self.assertTrue(selfheal.claude_auth_stale())


if __name__ == "__main__":
    unittest.main()
