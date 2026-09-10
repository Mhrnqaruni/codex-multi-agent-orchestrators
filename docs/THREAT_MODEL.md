# Threat model

## Assets and trust

Protected assets include the user's original checkout, unrelated host files,
credentials, provider account, money/time, private prompts, and review evidence.
Repository text, model output, package scripts, generated files, and external
content are untrusted. A request authorizes a task, not unlimited host access.

## Controls and remaining risk

| Threat | Implemented control | Remaining limitation |
|---|---|---|
| Prompt requests stronger permissions | Host-owned enum and fixed argv | Prompt injection can still distort task content within allowed authority |
| Inspector edits target | Codex read-only policy; host captures report; drift rejects response | Actual sandbox denial is not live-validated; no immutable snapshot yet |
| Writer overwrites original checkout | Dedicated worktree/branch required by engine constructors | A worktree is not a filesystem or credential sandbox |
| Target `test` script runs on host | Host verification runner always blocks project commands | Codex's own tools can execute code within its sandbox; no granular shell-command allowlist |
| Parent environment exposes unrelated tokens | Explicit launcher variable allowlist; shell inherits none by configuration | Filesystem credentials and Codex authentication remain relevant |
| CLI launches a shell wrapper | Native executable required; batch/PowerShell launchers rejected | User-selected executable must itself be trusted |
| Infinite retries/output/processes | Calls, time, input/output caps; process groups/jobs | Dollar cost is not measured; budgets reset on explicit new instance |
| Partial output is treated as approval | JSONL completion/error validation; no failed output forwarded | Retained engine result/state paths still need broader failure testing |
| Logs disclose prompts/source | Metadata-only diagnostics; raw content modes rejected | Resume state, reports, terminal output and provider session storage remain sensitive |
| History leaks private material | Verified private backup; tree guard and dedicated scans | Original private ancestry still contains private artifacts |

## Process cleanup is not sandboxing

On POSIX, children share a new process group; cleanup sends SIGTERM and then
SIGKILL. On Windows, the trusted CLI is assigned to a kill-on-close job immediately
after launch. Assignment is not atomic with process creation. A hostile native
executable or deliberately escaping descendant is outside this cleanup guarantee.
The Windows path currently uses job termination without a separate graceful
shutdown protocol. Do not advertise this module as an OS security sandbox.

## Network and tool boundaries

Normal recovery no longer probes vendor hosts. Package-registry and TCP preflight
checks do not contact destinations; they report unverified availability. Codex
itself contacts its configured provider to operate. The fixed configuration asks
Codex to disable generated-command network access and web search, but this has not
been tested as an end-to-end guarantee against every plugin/provider/OS setup.

For untrusted repositories, use an isolated VM/account with no unrelated secrets,
minimal credentials, explicit provider spend limits, and no privileged external
integrations. Do not run this alpha against production or client data.

## Evidence required before publication

Actual read-only write-denial tests, outside-workspace and network-denial tests,
hostile project-configuration tests, immutable-candidate review, state/path race
tests, and complete failure/resume workflows remain required. Fake tests prove
the command/policy contracts we construct, not the behavior of the provider.

Reference: [official Codex security guidance](https://learn.chatgpt.com/docs/security).
