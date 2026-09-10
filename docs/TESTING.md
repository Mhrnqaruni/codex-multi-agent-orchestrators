# Testing and verification

## Reproduce locally

Use a virtual environment, then:

```text
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
python -m ruff check codex_orchestrators tests tools
python -m ruff format --check codex_orchestrators tests tools
python tools/check_public_tree.py
python -m build --no-isolation
python tools/check_dist.py
python -m pip_audit --require-hashes -r requirements-dev.txt --progress-spinner off
```

Stage intended package additions before `check_dist.py`, because it compares
wheel members against tracked package files. It also compares file bytes and
checks license metadata and the absence of third-party runtime requirements.
The dependency audit queries public advisory services; it is not an offline test.

`requirements-dev.txt` pins and hashes the development dependency graph. Update
deliberately using pip-tools with `--generate-hashes --allow-unsafe
--no-emit-index-url --no-emit-trusted-host --strip-extras`, then install/audit/test
the new lock. Do not copy private index credentials into the repository.

## What the tests prove

- Exact edit/review authority, unsupported role/launcher rejection, filtered environment.
- Structured success/error/malformed events and session-ID spoof resistance.
- Canonical Congress bounded recovery and permanent failure handling.
- Bounded process output, timeout, cancellation and descendant cleanup.
- Candidate drift, hard-link rejection, host-selected review artifact paths.
- Metadata content exclusion, event limits and scoped cleanup.
- Clean-source worktree preparation and dirty-source refusal.
- Staged/worktree publication guard behavior with fictional secret-like fixtures.
- Retained Government parsing/state/regression behavior.

Historical tests previously used a helper that printed failures without making
pytest fail. That helper now asserts. Some historical Government tests still
inspect source structure; they are supplementary checks, not evidence that full
live behavior is correct.

## What the tests do not prove

Actual Codex sandbox write/network denials, immutable reviewer snapshots,
concurrent hostile filesystem safety, every legacy state migration, end-to-end
phase completion, model review quality, token/dollar budgets, or production
reliability. The offline demo is deliberately narrower than a full engine run.

## CI and secrets

The workflow runs Windows/Ubuntu and Python 3.11–3.13, builds/inspects the wheel,
then installs it in a separate environment and runs commands outside the checkout.
Actions are pinned by SHA; checkout does not persist Git credentials; jobs have
read-only repository permissions. No live Codex account or repository secret is
needed. No raw logs or package artifacts are uploaded automatically.

The CI secret job scans the candidate tree. It intentionally does **not** certify
the old private history. A dedicated full-history scan is a separate mandatory
publication gate; do not suppress historical private data to make that gate green.

Ruff currently enforces the adopted correctness rule set and consistent format.
Comprehensive static typing and deeper state-machine coverage remain follow-up
work, not completed gates.
