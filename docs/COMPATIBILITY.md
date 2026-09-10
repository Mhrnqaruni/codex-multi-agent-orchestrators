# Compatibility

## Python and operating systems

The package requires Python 3.11 or newer. The automated matrix targets Python
3.11, 3.12, and 3.13 on Windows and Ubuntu. A configured matrix is not a passing
matrix: check the exact candidate's GitHub Actions results and the readiness
report. macOS and newer Python releases are not validated targets yet.

Runtime dependencies: Python standard library only, plus a separately installed
native Codex CLI for live operation. Git is needed for worktree preparation.
The offline demo does not need Codex or Git.

## Codex

The adapter currently accepts exactly **`codex-cli 0.153.4`**, the native command
contract inspected during hardening. It runs `--version` with bounded output/time
before starting an agent. An unsupported version fails closed rather than
silently guessing. This is an inspected compatibility target, not a claim of
successful live sandbox validation.

Required behavior includes `exec`, explicit-session `exec resume`, JSONL events,
`--ignore-user-config`, `--ignore-rules`, and fixed sandbox/approval configuration.
Session IDs come from `thread.started`; final text comes from completed
`agent_message` items; an error or missing `turn.completed` is not success.

Windows shell launchers (`.cmd`, `.bat`, `.ps1`) are intentionally unsupported.
Select the native executable supplied with the CLI installation. No launcher is
downloaded, rewritten, or installed by this project. Authentication must already
be configured in the selected dedicated environment; API-key environment
variables are not inherited.

To support another version: inspect its help and official documentation, record
synthetic JSONL fixtures, run the policy/process tests, validate actual sandbox
denials in an isolated environment, and review the version change. Never loosen
the version check solely to make a failing live run start.

References: [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference)
and [Codex security](https://learn.chatgpt.com/docs/security).
