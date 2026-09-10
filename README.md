# Codex Multi-Agent Orchestrators

Durable Python workflows around the Codex CLI: create a deliverable, review it,
track evidence, and recover from interrupted work without treating partial
output as success.

**Alpha · source available for noncommercial use · Python 3.11+ · no third-party
Python runtime dependencies**

This is an independent project, not an official OpenAI product. The current
candidate is undergoing private release review. **Do not make this repository's
existing history public.** See the [release gates](docs/READINESS.md).

## What the project does

| Workflow | Intended use | Main artifacts |
|---|---|---|
| Congress | Iteratively create and review explicitly requested files | Deliverables, review assessments, output hashes, controlled result status, resumable state |
| Government | Plan and execute a project in reviewed phases | Master plan, phase plans, execution reports, review files, resumable phase state |

Congress is the canonical successor to the original scripts. It retains output
contracts, state migration, locking, task classification, a second review stage,
and evidence-based approval gates. Government retains its distinct phased
workflow; both use the same policy and process boundary.

```text
Request → dedicated edit worktree → Researcher / Executor
                    ↑                       ↓
              revision request ← read-only Inspector
                                            ↓
                           output / review / verification gates
                                            ↓
                              approved, blocked, or resumable stop
```

Separate roles are not a guarantee of independent judgment. Reviewers using the
same model/provider may share errors. An agent's verdict is not proof that a
project is correct or secure.

## Try it without a Codex account

From an authorized checkout, create a virtual environment and install locally:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m codex_orchestrators demo
.\.venv\Scripts\python.exe -m codex_orchestrators policy --role review
```

On Linux, use `.venv/bin/python` instead. Installation may download the build
backend; **the demo and policy commands themselves use no network, credentials,
or model calls**. No package has been published to PyPI.

The demo exercises synthetic retry classification, JSONL response validation,
role-policy construction, and candidate-change detection. It reports
`live_model_calls: 0`. It is a reproducible boundary demonstration, **not a live
agent demo, complete engine simulation, or model-quality benchmark**.

## Safety defaults

- Writers request `workspace-write`; Inspectors request `read-only`.
- Approval escalation is disabled. There is no full-host-access switch.
- User configuration and custom exec rules are not loaded; project `.codex`
  configuration is rejected by the adapter.
- Generated-command network access and web search are disabled by configuration.
- The launcher receives a documented environment allowlist, not all parent secrets.
- Codex is invoked as a native executable, without a command shell. Windows
  `.cmd`, `.bat`, and `.ps1` launchers are rejected.
- Calls, retry attempts, output size, and process lifetime have ceilings. POSIX
  process groups and Windows kill-on-close jobs clean up descendants.
- Diagnostic files contain counts and event metadata, not raw prompts or responses.
- Discovered target-project test/build commands are **blocked on the host**.
  Naming a script `test` does not authorize it to execute.

These are implemented controls with deterministic tests, not a security
certification. Codex provides the actual command sandbox. A read-only sandbox
does not necessarily hide readable host files, and the CLI needs access to its
authentication. Use a dedicated OS account or disposable VM for untrusted code.
See the [threat model](docs/THREAT_MODEL.md) before any live run.

## Live workflow status

Live orchestration remains experimental and has not passed the publication
safety gate. Do not use production data, client repositories, or privileged
accounts. The [compatibility page](docs/COMPATIBILITY.md) records exactly what
has and has not been verified.

The installed entry points are:

```powershell
.\.venv\Scripts\codex-congress.exe --help
.\.venv\Scripts\codex-government.exe --help
```

Live engine constructors require a dedicated Git worktree on an
`orchestrators/<run-name>` branch. The helper below prepares one from a clean
synthetic source checkout; it does not launch an agent, commit, push, merge,
or delete anything:

```powershell
.\.venv\Scripts\python.exe -m codex_orchestrators prepare-worktree --source <synthetic-repo> --destination <new-sibling-directory> --branch orchestrators/demo
```

The helper refuses dirty sources, existing destinations, nested destinations,
submodules, project Codex configuration, and root attributes/filter setup that
needs separate review. See [usage and migration](docs/USAGE.md).

## State, privacy, and limits

Diagnostic metadata is stored under `%LOCALAPPDATA%/codex-orchestrators` on
Windows or `$XDG_STATE_HOME/codex-orchestrators` on Linux (falling back to
`~/.local/state`). Each diagnostic run is capped at 1,000 events.

**Resume state and requested reports are different:** retained workflows still
write sensitive state and working documents into their dedicated worktrees.
They may contain requests, project content, paths, or session identifiers. They
are not safe to publish merely because diagnostics are metadata-only.

Preview diagnostic cleanup; add `--apply` only after reviewing the count:

```powershell
python -m codex_orchestrators purge --older-than-days 30
```

This does not erase resume state, deliverables, backups, or Codex's own sessions.
Read [privacy and retention](docs/PRIVACY.md).

Default ceilings are 24 agent calls and two hours per workflow instance, ten
minutes per call, 1 MiB input, and 2 MiB combined subprocess output. A recovery
operation allows its initial attempt plus three retries. These are **not dollar
spend limits**, and an explicit new/resumed instance receives a new budget.

## Development and evidence

```powershell
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
python -m ruff check codex_orchestrators tests tools
python tools/check_public_tree.py
python -m build --no-isolation
```

Tests use synthetic files and fake process responses, without model credentials.
Coverage includes command policy, JSONL failures, bounded recovery, environment
filtering, metadata leakage, process cleanup, candidate drift, isolated worktree
creation, and publication guards. Historical Government regression checks remain
while deeper engine characterization continues. See [testing](docs/TESTING.md)
for exact evidence and limitations; a green suite is not full-engine certification.

## Project map

- [Architecture and design decisions](docs/ARCHITECTURE.md)
- [Usage, migration, and troubleshooting](docs/USAGE.md)
- [Compatibility](docs/COMPATIBILITY.md)
- [Threat model](docs/THREAT_MODEL.md) and [security reporting](SECURITY.md)
- [Privacy and retention](docs/PRIVACY.md)
- [Readiness checklist](docs/READINESS.md)
- [Contribution workflow](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## License

The [Codex Orchestrators Noncommercial Source License 1.0](LICENSE) permits
noncommercial study and an explicit hiring-evaluation exception. It does **not**
grant permission for paid services, client work, internal business automation,
or commercial product development. This is **source-available software, not
OSI-approved open source**. Third-party tools retain their own licenses.

The custom terms have not been independently reviewed by a lawyer and cannot
physically prevent misuse. Read [licensing details](docs/LICENSING.md) before
sharing or relying on the commercial restriction.
