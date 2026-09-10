# Usage and migration

## Supported offline commands

`python -m codex_orchestrators --help` lists the package commands.

- `demo`: synthetic boundary checks, zero model calls.
- `policy --role review`: print effective argv and limitations without execution.
- `prepare-worktree`: explicitly create one isolated branch/worktree from a clean source.
- `purge --older-than-days 30`: preview eligible diagnostic metadata cleanup.
- `purge --older-than-days 30 --apply`: remove only that eligible metadata.

The policy command is an explanation/dry run of the shared agent boundary, not a
complete engine execution plan. It does not inspect or approve target commands.

## Live workflows: release-review only

Do not start a live workflow until an authorized isolated synthetic test has
established sandbox behavior. Live runs can use paid account capacity. Never
use production credentials, personal repositories containing secrets, or client
data for validation.

When approved, prepare a dedicated worktree with the package helper. Its branch
must be `orchestrators/<run-name>`. A normal clone, dirty primary checkout, or
arbitrary branch is not a supported live target. Review every resulting diff in
that worktree; nothing automatically merges back to the source.

The engine help commands document their retained flags:

```text
codex-congress --help
codex-government --help
```

Congress requires explicit requested output files for a noninteractive query.
Its verification modes still control evidence/gating, but discovered host
commands are blocked in every mode. Choosing `off` is an explicit verification
waiver, not a passing test result. Disabling the second Inspector is likewise
a waiver; it does not mean two reviews occurred.

## Migration from root scripts

| Previous invocation/import | New entry point |
|---|---|
| `python congress.py` | `codex-congress` |
| `python congress2.py` | `codex-congress` |
| `python government.py` | `codex-government` |

Root scripts remain source-checkout compatibility shims. The wheel installs the
package and console commands, not three competing engines. Internal underscore
helpers are not a stable public API.

Behavior changes are intentional: no bypass/custom approval switch; native CLI
only; required worktree isolation; content logging disabled; bounded calls and
retries; failed partial output never counts as success; Inspector reports are
captured by the host; target commands no longer execute directly.

Do not migrate real historical session files into tests or public examples.
Preserve a private copy before trying any existing state migration. Congress's
retained migration logic is not a promise that every historical Government or
Congress run can resume under the changed permission model.

## Troubleshooting

| Symptom | Meaning and next action |
|---|---|
| Execution policy/transport failure | Check native CLI version, dedicated worktree, project config, size/link limits; do not add bypass flags |
| Verification blocked | No host authorization to run target code; use external isolated validation and preserve the blocked status |
| Budget exhausted | Stop/review saved work; a new instance resets its budget, so apply provider spend limits too |
| Candidate changed during review | Discard that review and investigate concurrent writes before reviewing again |
| Malformed/incomplete event stream | CLI compatibility or failed turn; do not parse partial prose as approval |
| Content logging rejected | Use metadata-only mode; do not weaken privacy controls to debug a real secret-bearing project |

Rollback consists of reviewing/reverting the topic changes or abandoning an
isolated run while keeping its evidence private. The tool does not remove Git
worktrees or branches automatically. Never reset the original checkout to clean
up an agent run.
