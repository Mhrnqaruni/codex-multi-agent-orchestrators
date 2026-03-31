# Codex Multi-Agent Orchestrators

Private tooling for running two different multi-agent workflows on top of the Codex CLI:

- `congress.py` runs a Researcher plus Inspector debate loop to improve a single answer.
- `government.py` runs a persistent Executor plus Inspector pipeline to plan, execute, review, and resume a project phase by phase.
- `test_government_fixes.py` is the regression test suite for `government.py`.

The repository is built around Python's standard library. There are no third-party Python runtime dependencies in the code itself.

## Requirements

- Python 3.10 or newer
- Codex CLI installed and authenticated
- `pytest` if you want to run the test suite with pytest
- `gh` only if you want to publish or manage the repository from the terminal

Developed and tested primarily on Windows PowerShell. The scripts include Unix fallbacks for keyboard handling where possible.

## Repository Contents

```text
congress.py
government.py
test_government_fixes.py
update.md
logs/
```

## Important Runtime Behavior

Both orchestration scripts call Codex through `codex exec` and currently pass:

```bash
--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check
```

Use them only in a trusted workspace.

## Quick Start

1. Verify the Codex CLI is available:

```powershell
codex --version
```

2. Check the script help:

```powershell
python congress.py --help
python government.py --help
```

3. Run the test suite:

```powershell
pytest -q test_government_fixes.py
python test_government_fixes.py
```

## Script Reference

### `congress.py`

Purpose:

- Best for one-off questions, research tasks, code design requests, and answer refinement.
- Starts a fresh Codex session for every agent call.
- Stops when the Inspector returns `VERDICT: APPROVED` or when the round limit is reached.

Command syntax:

```powershell
python congress.py
python congress.py --query="How do I implement a binary search tree?"
python congress.py --max-rounds=5
python congress.py --timeout=900
python congress.py --workdir="C:\MyProject"
python congress.py --codex-bin="C:\Path\To\codex.cmd"
python congress.py --approval="--dangerously-bypass-approvals-and-sandbox"
```

Command-line options:

- `--max-rounds=N` sets the maximum debate rounds. Default: `3`
- `--timeout=N` sets the silence timeout in seconds. Default: `600`
- `--codex-bin=PATH` points to a specific Codex executable
- `--workdir=PATH` sets the working directory passed to Codex. Default: current directory
- `--query="..."` runs one non-interactive task and exits
- `--approval=FLAG` overrides the approval flag passed to Codex
- `--help` prints usage

Interactive mode behavior:

- `quit`, `exit`, or `q` exits the tool
- `logs` opens the local `logs/` folder
- `rounds N` changes the current max rounds
- entering a line ending with `\` continues input on the next line

Between-agent transition controls:

- `C` continues immediately
- `P` pauses and waits for your command
- `O` finalizes the current researcher output early
- `Q` quits the current session

Files written by `congress.py`:

```text
logs/<session_id>/
  final_output.txt
  master.log
  researcher.log
  inspector.log
  session.json
  rounds/
    round_<n>_researcher.txt
    round_<n>_inspector.txt
```

Notes:

- Researcher output is truncated before being embedded back into later prompts if it exceeds the internal prompt-size guard.
- Rate-limit errors are detected from Codex stderr and stop the session cleanly instead of retrying forever.

### `government.py`

Purpose:

- Best for turning a project specification or plan into a resumable, phase-based implementation workflow.
- Keeps persistent Executor and Inspector sessions and stores their session IDs in `.government/state.json`.
- Creates plans, reviews, execution reports, approved copies, and resumable state on disk.

Command syntax:

```powershell
python government.py
python government.py C:\MyProject
python government.py spec.md
python government.py --source=spec.md
python government.py --source=spec.md --workdir=C:\MyProject
python government.py --source=spec.md --instructions="Target Python 3.12 only"
python government.py --auto-continue=0
```

Command-line options:

- `--source=FILE` sets the project specification or plan file
- `--workdir=DIR` sets the working directory
- `--instructions="..."` adds extra instructions for the Executor workflow
- `--max-review-rounds=N` sets the soft-stop review limit per loop. Default: `7`
- `--timeout=N` sets silence timeout in seconds. Default: `600`
- `--auto-continue=N` sets post-phase auto-continue seconds. Default: `300`; `0` disables it
- `--codex-bin=PATH` points to a specific Codex executable
- `--help` prints usage

Start modes:

- `python government.py` starts interactive mode and asks for the working directory and source file
- `python government.py <directory>` opens an existing workspace or starts there
- `python government.py <file>` treats the positional argument as the source file
- `python government.py --source=<file>` starts directly from a known spec file

High-level workflow:

1. Initialize the persistent Executor session
2. Initialize the persistent Inspector session
3. Create and review `master_plan.md`
4. For each phase, create and review `phase_<n>/plan.md`
5. Execute the phase and review `phase_<n>/exec_report.md`
6. Save a short phase summary into state and `project_status.md`
7. Stop, continue, abort, or extend depending on user choices

Files and folders written by `government.py`:

```text
<workdir>/
  .government/
    state.json
    logs/
    pause
  master_plan.md
  master_plan_review.md
  project_status.md
  phase_<n>/
    plan.md
    plan_review.md
    plan_approved.md
    exec_report.md
    exec_review.md
    exec_report_approved.md
    .archive/
```

What each generated file means:

- `master_plan.md` is the top-level phase breakdown for the whole project
- `master_plan_review.md` is the Inspector's review of the master plan
- `project_status.md` is the current summary of completed and active phases
- `phase_<n>/plan.md` is the detailed implementation plan for one phase
- `phase_<n>/plan_review.md` is the Inspector's review of that phase plan
- `phase_<n>/plan_approved.md` is the approved copy of the phase plan
- `phase_<n>/exec_report.md` is the Executor's implementation and verification report
- `phase_<n>/exec_review.md` is the Inspector's review of the execution report
- `phase_<n>/exec_report_approved.md` is the approved execution report
- `phase_<n>/.archive/` stores versioned snapshots before a file is revised
- `.government/state.json` is the durable source of truth for resume state, sessions, and counters

Pause and resume controls:

- press `P` while an agent is running to queue an interactive pause
- create `<workdir>\.government\pause` to force a filesystem-based pause
- at the soft-stop prompt, choose `c` to continue, `s` to skip, or `a` to abort
- at the phase checkpoint, choose `c` to continue, `d` to mark the project done, or `a` to abort
- when a previous workspace already contains `.government/state.json`, the launcher offers resume, extend, start-fresh, or quit choices depending on the saved state

Resilience features:

- persistent session IDs are captured from Codex startup stderr headers
- expired sessions fall back to fresh sessions
- network interruptions are retried with recovery logic
- rate-limit and billing errors are detected and handled separately from session errors
- resume logic restores work at the master-plan, phase-plan, or execution loop level

### `test_government_fixes.py`

Purpose:

- Verifies `government.py` control-flow and recovery logic without needing the Codex CLI
- Covers state persistence, resume behavior, verdict parsing, session ID capture, phase counting, pause handling, and regression fixes

Usage:

```powershell
pytest -q test_government_fixes.py
python test_government_fixes.py
```

Behavior:

- `pytest -q test_government_fixes.py` is the cleanest automation path
- `python test_government_fixes.py` runs the same checks in a standalone terminal-friendly format and exits non-zero on failure

## Supporting Files

### `update.md`

Operational incident note describing a March 2026 rate-limit and session-fallback failure mode in `government.py`, including the intended remediation order and test scenarios.

### `logs/`

Historical `congress.py` run artifacts from previous sessions. These are not required to execute the scripts, but they show the logging structure and example outputs produced by the debate workflow.

## Troubleshooting

- If a script prints `Codex CLI not found`, install Codex or pass `--codex-bin=...`
- If `government.py` is resumed with expired session IDs, it should automatically start new sessions and continue
- If a generated review file is missing its `VERDICT` line, `government.py` explicitly asks the Inspector to repair it
- If no final output is produced by `congress.py`, inspect the corresponding `logs/<session_id>/` directory

## Validation Performed In This Repository

The repository currently validates cleanly with:

```powershell
pytest -q test_government_fixes.py
python test_government_fixes.py
python congress.py --help
python government.py --help
```
