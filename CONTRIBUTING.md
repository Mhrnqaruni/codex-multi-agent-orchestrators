# Contributing during private hardening

Do not commit directly to `main`. Use an isolated topic worktree and submit a
focused PR to the agreed base (`main` or `integration/public-readiness`). Owner
approval is required before merges; integration and publication are separate
decisions. The owner approved consolidating the current private alpha into main
on 2026-09-10; this is not blanket approval for future merges or publication.

Verify a confidential offline backup first. Record the base SHA and dirty state;
never reset or absorb another person's changes. Never upload bundles, private
sessions, credentials, or internal project context.

Run `python -m pytest -q` and `python tools/check_public_tree.py` before staging.
After staging, run `python tools/check_public_tree.py --staged` and
`git diff --cached --check`. The staged check examines Git blobs rather than
trusting working files. Include exact commands/results in the PR.

Document scope, SHAs, plan items, tests, remaining risks, license/dependency
effects, and revert instructions. Use fictional temporary fixtures. Live Codex
runs and real-project execution are excluded from normal tests. Implemented
controls and remaining safety gaps are described in `docs/READINESS.md`.

Install hashed tooling with `python -m pip install --require-hashes -r
requirements-dev.txt`. Run Ruff, pytest, the dependency audit, and wheel
inspection as described in `docs/TESTING.md`. Changes to the CLI adapter require
protocol and permission tests; changes to path/state code require adversarial
fixtures. Do not replace a failed gate with a weaker claim or a skipped check.

By submitting contributions, you represent that you may license them under
this repository's noncommercial license. No copyright assignment or separate
commercial relicensing permission is implied. Identify third-party material
and its license before adding it.
