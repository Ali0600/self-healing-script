#!/bin/bash
# Drill/test verify script: always fails.
#
# Point a repo's verify_script here to prove the pipeline fails closed — a
# heal whose independent verification fails must never open a PR, no matter
# what the agent claims. (See docs/onboarding.md, "Fire drill".)
echo "VERIFY simulated failure (verify/always-fail.sh)"
exit 1
