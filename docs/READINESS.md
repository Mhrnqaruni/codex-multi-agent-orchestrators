# Public-readiness gate

**Decision: keep private. This candidate is substantially hardened, but is not
approved for public release or unrestricted live execution.** A professional
README and passing unit tests do not replace the remaining safety gates.

## Implemented in the candidate

- Private logs/incident memo removed from the tracked tree; original backup retained.
- One canonical Congress engine with compatibility shims and installable packaging.
- Shared role policy, filtered environment, native CLI, version preflight and JSONL adapter.
- Bounded retry/call/output/process controls; Windows/POSIX cleanup tests.
- Metadata-only diagnostics and scoped explicit metadata purge.
- Read-only review argv, host-captured Government reviews, candidate-drift rejection.
- Host project-command execution blocked, without pretending blocked checks passed.
- Dedicated edit worktrees required; explicit clean-source preparation helper.
- Real pytest assertions, canonical recovery and security-boundary tests.
- Pinned/hashed development dependencies; build-tool advisory addressed.
- Wheel content/license checks, offline demo, CI matrix and contribution templates.
- Architecture, threat model, privacy, compatibility, usage and testing documentation.
- Custom noncommercial source-available license with a hiring-evaluation exception.

## Still required before the owner's public-release decision

| Gate | Remaining work |
|---|---|
| History/privacy | Create an approved clean public history from allowlisted files, or separately authorize an all-ref rewrite; inspect PII and public identity; scan the resulting full history |
| Live authority | In an explicitly authorized isolated synthetic environment, prove Inspector write denial, writer containment, configuration/plugin restrictions, and network limits with the accepted native CLI |
| State/privacy | Move retained workflow state and scratch artifacts into one access-controlled store; define retention, explicit content consent and migration; test corruption/lock/race cases |
| Review evidence | Use an immutable candidate snapshot and prove post-review approval invalidation through complete workflows |
| Verification | Integrate explicit command approval with an external isolated verifier; currently host commands correctly remain blocked |
| Engine correctness | Complete fake-executable end-to-end Congress/Government/resume/failure characterization; replace remaining source-only checks; further split large methods |
| Budgets | Persist cross-instance budgets and implement a strict whole-workflow deadline including waits; current budgets prevent new calls, not all interactive idle time |
| CI/release | Observe exact-SHA Windows/Linux checks; review artifacts/fresh clone; configure owner-approved protections; authorize any tag/release separately |
| Ownership/license | Confirm all content is publishable; obtain legal review if relying on custom commercial restrictions |

## Branch and publication boundaries

The original `main` remains private and unchanged at the reviewed base. Work is
committed on hardening/refactor branches, never directly on `main`. Local stacked
topics are not automatically accepted integration changes. Review the cumulative
PR and exact candidate results before merging to `integration/public-readiness`.

Do not publish these branches: their ancestry contains the original private
material. No force push, main merge, replacement repository, visibility change,
package upload, release, or live private-target run is authorized by this file.

## CV wording that is supportable now

> Built a Python research/review and phased-agent orchestration project with
> durable workflow state; extracted role-scoped CLI execution, bounded recovery,
> structured event validation, privacy-conscious diagnostics, and deterministic
> failure tests during a security-hardening program.

Describe it as an alpha/private project. Do not claim production security,
independent model judgment, fully verified sandboxing, or public availability.
