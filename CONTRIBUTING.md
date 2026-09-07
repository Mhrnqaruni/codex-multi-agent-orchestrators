# Contributing during private hardening

Keep `main` unchanged. Use an isolated topic worktree based on
`integration/public-readiness`, and submit small PRs to that integration branch.
Owner approval is required before merges; final integration and publication
are separate decisions.

Verify a confidential offline backup first. Record the base SHA and dirty state;
never reset or absorb another person's changes. Never upload bundles, private
sessions, credentials, or internal project context.

Run `python -m pytest -q` and `python tools/check_public_tree.py` before staging.
After staging, run `python tools/check_public_tree.py --staged` and
`git diff --cached --check`. The staged check examines Git blobs rather than
trusting working files. Include exact commands/results in the PR.

Document scope, SHAs, plan items, tests, remaining risks, license/dependency
effects, and revert instructions. Use fictional temporary fixtures. Live Codex
runs and real-project execution are excluded from normal tests; the engines
do not yet enforce the proposed safety model.

By submitting contributions, you represent that you may license them under
this repository's noncommercial license. No copyright assignment or separate
commercial relicensing permission is implied. Identify third-party material
and its license before adding it.
