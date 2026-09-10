# Architecture

## Boundaries

| Module | Responsibility |
|---|---|
| `cli.py`, `demo.py` | Offline inspection, explicit worktree preparation, synthetic demonstration, metadata cleanup |
| `policies.py` | Enumerated roles, exact argv, environment filtering, workspace preflight |
| `compatibility.py` | Fail-closed native CLI version check |
| `processes.py` | Bounded pipe transport, wall timeout, cancellation, descendant cleanup |
| `events.py` | Codex JSONL envelopes, final messages, failure/completion semantics |
| `adapter.py` | Connect policy, budget, process, protocol, and candidate checks |
| `recovery.py` | Permanent/transient classification and workflow-instance budgets |
| `metadata.py` | Content-free, size-bounded diagnostic events |
| `workspace.py` | Candidate fingerprints, host-owned review paths, explicit worktree creation |
| `congress_config.py` | Defaults and controlled state/review vocabulary |
| `congress_models.py` | Results, task classification, verification, review and issue models |
| `congress_prompts.py` | Versioned role templates; never the authority boundary |
| `congress_engine.py` | Retained Congress orchestration, persistence, review and approval gates |
| `government_engine.py` | Retained Government phased orchestration and state |

There are no third-party Python runtime dependencies. The native Codex CLI is
an external dependency, not bundled or silently installed.

## Decisions

1. **One Congress engine.** Legacy filenames are compatibility shims; fixes apply
   to the package implementation. The original implementation is preserved only
   in private Git history/backup.
2. **Extract boundaries before rewriting orchestration.** Permissions, processes,
   events, and privacy are shared services. The large retained engines are not
   yet fully decomposed state machines. This is a deliberate migration boundary,
   not a claim that the monolith problem has been solved.
3. **No generic permission strings.** Role selection is an enum. Untrusted text
   cannot add configuration switches to the host-built argv.
4. **Final responses are protocol data.** Human stderr is not a session-ID
   protocol. Missing completion, malformed events, or error events cannot produce
   a successful response, even if partial text says `VERDICT: APPROVED`.
5. **Read-only review uses host capture.** The Inspector returns its review.
   Government writes only a host-selected phase review path. Congress uses its
   existing managed review artifact writers.
6. **Candidate drift invalidates review.** Fingerprints include relative paths,
   file modes, and content, with file-count/byte limits. Before/after checks detect
   drift, but are not an immutable snapshot or protection against a malicious
   concurrent same-user process that changes and restores a file.
7. **Fail closed on automatic verification.** Discovery remains useful evidence,
   but command execution is blocked pending an isolated verifier integration.
8. **Flat package during migration.** Explicit setuptools package selection keeps
   root compatibility scripts out of the wheel. Fresh wheel tests must run away
   from the checkout to avoid import-shadowing mistakes.

## Failure semantics

A workflow may finish approved, unapproved, blocked, interrupted/resumable, or
failed. No clean exit from a subprocess alone proves task completion. Congress's
existing result gates evaluate output contracts and review/verification evidence.
Government stops on terminal adapter errors and does not reinterpret partial
rate-limit output as success.

The process boundary caps each invocation. A logger-owned `RunBudget` counts
calls across retries and continuations for that workflow instance. It is not
persisted across explicit restart/resume and does not enforce provider spending.

## Not implemented yet

Full engine state-machine decomposition, a single secure state repository,
immutable reviewer snapshots, externally sandboxed verification execution, and
cross-instance budgets are still release-critical follow-up work. See
[readiness](READINESS.md); do not infer these guarantees from module names.
