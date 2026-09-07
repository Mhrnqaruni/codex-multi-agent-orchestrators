# Codex Multi-Agent Orchestrators

Python experiments in durable agent workflows: creation, adversarial review,
verification, and recovery around the Codex CLI.

**Status: private hardening in progress. Not approved for public release or
production use.** Retained engines still have unsafe execution defaults,
content logging, and incomplete test coverage. Do not run them on real projects
or authenticated accounts at this stage. See [readiness status](docs/READINESS.md).

## Implementations

| File | Purpose | Status |
|---|---|---|
| `congress2.py` | Stateful Researcher/Inspector, output contracts, locks, verification | Candidate canonical engine; needs characterization and security work |
| `congress.py` | Earlier answer/review workflow | Legacy reference until parity is established |
| `government.py` | Persistent Executor/Inspector project phases | Experimental; needs shared policy and process controls |

Runtime dependencies are Python standard library only. Python 3.10+ is the
existing intended baseline; this change is locally tested on Python 3.11.
A supported cross-platform matrix is still pending.

## Safe local inspection

These commands need no Codex account and launch no live agents. Use an isolated
checkout and install pytest in a virtual environment for development:

```powershell
python congress.py --help
python congress2.py --help
python government.py --help
python -m pytest -q
python tools/check_public_tree.py
```

The original 25 tests cover legacy Congress and Government regressions; they do
not establish the safety of `congress2.py`. Publication guard tests check tracked
artifact boundaries and obvious secret patterns, not complete history or PII.
Reproducible dependency locking and packaging remain scheduled work.

## Data and history

Real session logs and the private incident memo were removed from this branch's
tracked tree. Existing private history still contains them. Keep it private;
the public edition needs reviewed allowlisted files and clean history, unless
an all-ref rewrite is separately approved.

Ignoring runtime data does not change the engines' current collection or
retention behavior. Metadata-only logging and safe state storage are pending.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for branches, review, and privacy checks,
and [SECURITY.md](SECURITY.md) for current limitations and reporting.

## License

Source available for noncommercial use under the
[Codex Orchestrators Noncommercial Source License 1.0](LICENSE).
Commercial use—including internal business automation, paid services,
consulting, and commercial product development—is not granted. Hiring review
of source and synthetic examples is expressly permitted.
See [licensing scope](docs/LICENSING.md).

This independent project is not affiliated with or endorsed by OpenAI.
