# Privacy and retention

There are three separate data stores. Only the first was replaced during this
hardening slice; do not conflate their guarantees.

| Store | Contents | Location and handling |
|---|---|---|
| Diagnostic metadata | Event names, timestamps, counts, return codes, durations | OS application-state directory; max 1,000 events/run; manual purge |
| Workflow state and artifacts | Requests, deliverables, review content, paths, session identifiers | Dedicated target worktree; sensitive; migration to one controlled private state store is pending |
| Codex session/auth data | Provider-managed session context and authentication | Codex's configured home; governed separately by Codex/provider settings |

## Metadata

Windows: `%LOCALAPPDATA%/codex-orchestrators/diagnostics`.
Linux: `$XDG_STATE_HOME/codex-orchestrators/diagnostics`, with a
`~/.local/state` fallback. Runs use random directory names, not provider session IDs.
POSIX directories/files request user-only permissions. Windows relies on inherited
profile-directory ACLs; `chmod` is not a full Windows ACL hardening mechanism.

`python -m codex_orchestrators purge --older-than-days 30` reports eligible runs.
Add `--apply` to remove only known `events.jsonl` files and their otherwise-empty
run folders. Unknown files and linked paths are not removed. Cleanup is explicit,
not a background service, so old metadata persists until you invoke it.

## Workflow data

Congress retains `congress_state.json`, lock/history/review/verification files,
round artifacts, and requested deliverables. Government retains `.government`
state and phase documents. Ignore rules in this tool repository do not magically
apply to a separate target repository. Review target `git status` before every
commit and never include runtime state in a public patch.

Do not put secrets in requests or source files. Regex masking cannot reliably
identify arbitrary personal or proprietary information. Treat terminal output,
saved reports, crash evidence, and old backups as confidential too.

Retention across workflow state, diagnostic counts, and provider sessions is not
yet centrally enforced. A complete purge/export policy and hardened state store
remain publication blockers. Never delete the original private backup during
hardening; it is the recovery source for removed historical material.
