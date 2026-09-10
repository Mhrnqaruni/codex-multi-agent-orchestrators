# Codex Multi-Agent Orchestrators

Python workflows that coordinate drafting and reviewing around the Codex CLI.
The project records progress, checks requested outputs, and handles interrupted
or failed agent calls without treating partial responses as completed work.

**Private alpha · noncommercial source license · Python 3.11+ · no third-party
Python runtime dependencies**

This is an independent project, not an official OpenAI product. This alpha is
maintained privately while publication requirements are resolved. **Do not make this repository's
existing history public.** See the [release gates](docs/READINESS.md).

Start with the [offline quickstart](#try-it-without-a-codex-account). Live agent
workflows are experimental, not a verified production feature. This README
describes the current implementation, not a promise that every retained workflow
works end to end.

## What the project does

For developers studying stateful agent orchestration and explicit review steps,
this project offers two workflows. It is not currently suitable for unattended
production use, client data, or running untrusted projects on a personal account.

| Workflow | Intended use | Main artifacts |
|---|---|---|
| Congress | Iteratively create and review explicitly requested files | Deliverables, review assessments, output hashes, controlled result status, resumable state |
| Government | Plan and execute a project in reviewed phases | Master plan, phase plans, execution reports, review files, resumable phase state |

For example, Congress could draft and review a requested `design.md`; Government
could break a fictional application into a master plan and reviewed phases.
These are intended use cases, not claims of validated live results. Both engines
share execution policy, process handling, and structured response validation.

```text
Request → dedicated edit worktree → Researcher / Executor
                    ↑                       ↓
              revision request ← read-only Inspector
                                            ↓
                              output and review checks
                                            ↓
                           verification gate (host tests blocked)
                                            ↓
                              result or resumable stop
```

Separate roles are not a guarantee of independent judgment. Reviewers using the
same model/provider may share errors. An agent's verdict is not proof that a
project is correct or secure.

## Try it without a Codex account

Open a terminal in the root of an authorized checkout of **this tool repository**
(the directory containing `pyproject.toml`). Use Python 3.11 or newer.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m codex_orchestrators demo
.\.venv\Scripts\python.exe -m codex_orchestrators policy --role review
```

Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m codex_orchestrators demo
.venv/bin/python -m codex_orchestrators policy --role review
```

These instructions do not require virtual-environment activation. Subsequent
commands use the same environment explicitly. Installation may download the build
backend; **the demo and policy commands themselves use no network, credentials,
or model calls**. No package has been published to PyPI.

The demo exercises synthetic retry classification, JSONL response validation,
role-policy construction, and candidate-change detection. It reports
`live_model_calls: 0`. It is a reproducible boundary demonstration, **not a live
agent demo, complete engine simulation, or model-quality benchmark**.

Expected demo output, defined by [the demo implementation](codex_orchestrators/demo.py):

```json
{
  "demo": "offline-contracts",
  "live_model_calls": 0,
  "simulated_calls": 3,
  "checks": {
    "transient_failure_classified": true,
    "candidate_change_detected": true,
    "structured_response_validated": true,
    "review_policy_read_only": true,
    "post_review_drift_invalidates_evidence": true
  },
  "scope": "Boundary demonstration; not an end-to-end engine run or model benchmark."
}
```

The demo changes a temporary synthetic file, constructs a fictional review
response, and detects a later file change. It removes its temporary directory;
it does not leave a generated project to inspect. These checks demonstrate shared
components, not complete engine approval invalidation. The `policy` command prints
configured arguments; it does not launch Codex or prove sandbox enforcement.

## Prerequisites and commands

| Requirement | Offline demo / policy | Experimental live engines |
|---|---|---|
| Python | 3.11+ | 3.11+ |
| Git | Not required | Required for worktree preparation and branch checks |
| Codex CLI | Not required | Native executable reporting exactly `codex-cli 0.153.4` |
| Authentication | None | Already configured in the dedicated execution environment |
| Model capacity / cost | No model calls | Can consume paid account capacity; no dollar-budget enforcement |

The automated matrix targets Windows and Ubuntu with Python 3.11–3.13. macOS
and newer Python versions are not validated targets. The exact Codex version is
an adapter compatibility restriction, **not evidence of a verified live run**.
Windows `.cmd`, `.bat`, and `.ps1` launchers are rejected. API-key environment
variables are not forwarded. See [compatibility](docs/COMPATIBILITY.md).

### Does `python congress2.py` still work?

The filename remains a compatibility launcher for the shared Congress engine;
it does not bypass installation, CLI, or worktree requirements. Starting it from
a normal clone is **not sufficient to run a task**: the engine requires a separate
target Git worktree on an `orchestrators/<run-name>` branch.

| Source-checkout launcher | Installed command | Purpose |
|---|---|---|
| `congress.py` or `congress2.py` | `codex-congress` | Draft/review requested outputs |
| `government.py` | `codex-government` | Phased planning/execution |
| `python -m codex_orchestrators` | `codex-orchestrators` | Offline demo, policy, worktree preparation, metadata cleanup |

Inspect command help without starting an agent, from the tool repository root:

```powershell
.\.venv\Scripts\python.exe -m codex_orchestrators --help
.\.venv\Scripts\codex-congress.exe --help
.\.venv\Scripts\codex-government.exe --help
```

On Linux, the installed commands are `.venv/bin/codex-congress` and
`.venv/bin/codex-government`. Retained engine help describes legacy options; it
does not override the restrictions below. See [migration notes](docs/USAGE.md#migration-from-root-scripts).

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

## Experimental live workflow preparation

Live orchestration remains experimental and has not passed the publication
safety gate. Do not use production data, client repositories, or privileged
accounts. The [compatibility page](docs/COMPATIBILITY.md) records exactly what
has and has not been verified.

There are three different locations:

- **Tool checkout:** this repository and its Python environment.
- **Synthetic source repository:** a separate, clean Git repository containing
  only fictional material, with at least one commit.
- **Target worktree:** a new sibling directory where the engine would edit files.
  It shares Git history with the synthetic source; it is not an OS security sandbox.

The following optional PowerShell setup creates an empty synthetic repository
and a local initial commit. Run it from the tool checkout, only if the named
sibling directory does not already exist. Nothing is uploaded. The placeholder
identity is scoped to this synthetic commit, not your Git configuration:

```powershell
if (Test-Path "../orchestrators-example-source") { throw "Choose a new source directory first." }
git init "../orchestrators-example-source"
if ($LASTEXITCODE -ne 0) { throw "Git initialization failed." }
git -C "../orchestrators-example-source" -c user.name="Synthetic Example" -c user.email="example@example.invalid" -c commit.gpgsign=false -c core.hooksPath=NUL commit --allow-empty -m "Initialize synthetic example"
if ($LASTEXITCODE -ne 0) { throw "Initial commit failed; inspect the synthetic repository." }
```

Live engine constructors require a dedicated Git worktree on an
`orchestrators/<run-name>` branch. The helper below prepares one from a clean
synthetic source checkout; it does not launch an agent, commit, push, merge,
or delete anything:

```powershell
.\.venv\Scripts\python.exe -m codex_orchestrators prepare-worktree --source "../orchestrators-example-source" --destination "../orchestrators-example-run" --branch orchestrators/demo
```

The helper refuses dirty sources, existing destinations, nested destinations,
submodules, project Codex configuration, and root attributes/filter setup that
needs separate review. See [usage and migration](docs/USAGE.md).

The destination must not exist and the new branch must be unused. Run names use
lowercase letters, digits, and hyphens, starting with a letter or digit (1–61
characters). Successful preparation prints the base commit and branch name.
It changes Git worktree/branch metadata, not the original checkout's files.

Preparation **does not authorize or validate live execution**. Before an agent
run, an explicitly authorized isolated synthetic validation must establish actual
sandbox behavior. For that later validation, Congress's `--workdir` selects the
target worktree and `--codex-bin` selects the native executable. Noninteractive
Congress also requires both `--query` and explicit `--outputs`; `--resume` uses
retained state. Do not point these options at this tool checkout or a real client
repository. A verified end-to-end live walkthrough is still a release requirement.

## Outputs and result interpretation

Congress retains requested deliverables, review/verification artifacts, and
`congress_state.json`. Government retains phase documents and `.government`
state. Exact artifacts depend on the workflow; see [privacy](docs/PRIVACY.md).

Interpret results as workflow evidence, not a blanket quality guarantee:

- **Approved:** the applicable workflow gates accepted the result; this does not
  imply that project tests ran or that the model's judgment was correct.
- **Unapproved / blocked:** required evidence or acceptance is missing. Discovered
  project test/build commands currently remain blocked on the host.
- **Interrupted / resumable:** saved progress may permit continuation; not success.
- **Failed:** a task or execution boundary failed; partial text is not an approval.

Verification `off` and disabling a second Inspector are explicit waivers, not
passing checks. Inspect the saved evidence and every target diff before deciding
what to keep. Nothing automatically merges back into the source repository.

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
.\.venv\Scripts\python.exe -m codex_orchestrators purge --older-than-days 30
```

This does not erase resume state, deliverables, backups, or Codex's own sessions.
On Linux, substitute `.venv/bin/python`. The tool repository's ignore rules do
not apply automatically to a target repository: inspect its `git status` before
committing and keep runtime state private. Read [privacy and retention](docs/PRIVACY.md).

Each workflow instance permits at most 24 agent-call attempts, with a two-hour
elapsed-time budget checked before calls. **This is not a hard shutdown deadline
for the whole workflow:** interactive waits may extend its lifetime. Other default
limits are ten minutes per call, 1 MiB input, and 2 MiB combined subprocess output.
A recovery operation allows its initial attempt plus three retries, subject to
the shared budget. These are **not dollar spend limits**; an explicit new/resumed
instance receives a new budget.

## Engineering highlights and known gaps

| Design choice | Implementation / evidence |
|---|---|
| One Congress implementation behind the old filenames | [Architecture](docs/ARCHITECTURE.md); shared package rather than competing script copies |
| Host-built role permissions, not model-selected authority | [Policy](codex_orchestrators/policies.py) and [adapter](codex_orchestrators/adapter.py) |
| Structured completion checks instead of trusting partial prose | [Event validation](codex_orchestrators/events.py) |
| Bounded recovery and subprocess resources | [Recovery](codex_orchestrators/recovery.py), [process handling](codex_orchestrators/processes.py) |
| Metadata diagnostics separated from sensitive workflow content | [Metadata](codex_orchestrators/metadata.py), [privacy model](docs/PRIVACY.md) |
| Synthetic failure checks and package inspection | [Testing scope and limitations](docs/TESTING.md) |

Remaining work includes full engine end-to-end characterization, immutable review
snapshots, actual live sandbox validation, private state consolidation, an external
isolated verifier, persistent budgets, and further decomposition of the large
engines. Public history also requires a separate privacy-cleared publication
decision. See the complete [readiness checklist](docs/READINESS.md).

## Troubleshooting

| Symptom | Next step |
|---|---|
| Module or command not found | Use the interpreter/entry point inside the environment installed in the quickstart |
| CLI / execution-policy failure | Check exact native version, authentication environment, worktree branch, and forbidden project configuration; do not add bypass flags |
| Worktree preparation refused | Check clean source, initial commit, unused branch, absent destination, and unsupported root files |
| Verification blocked | Keep the blocked status; host execution is intentionally unavailable pending an isolated verifier |
| Budget exhausted | Review private saved work; a new instance resets the budget, so manage account spending separately |

See [usage and troubleshooting](docs/USAGE.md) for recovery, migration, and
candidate-change failures. Keep diagnostic context free of private prompts and
project content when reporting a problem.

## Development and evidence

Use the environment created above, from the tool checkout. These commands are
for contributors, not prerequisites for trying the offline demo:

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check codex_orchestrators tests tools
.\.venv\Scripts\python.exe tools/check_public_tree.py
.\.venv\Scripts\python.exe -m build --no-isolation
```

On Linux, substitute `.venv/bin/python`. Use a topic branch/worktree and follow
[the contribution workflow](CONTRIBUTING.md); do not commit directly to `main`.
The full formatting, distribution-inspection, and audit sequence is in
[testing](docs/TESTING.md).

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
