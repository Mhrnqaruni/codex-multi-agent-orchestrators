# Changelog

## 0.1.0a1 — unreleased

- Select one canonical Congress engine and retain compatibility entry points.
- Extract policy, process, JSONL parsing, budget, metadata, and workspace helpers.
- Use read-only review and workspace-write editing without approval escalation.
- Reject shell launchers and arbitrary approval flags; filter inherited environment.
- Bound calls, retries, output, process lifetime, and descendant cleanup.
- Reject partial/error responses as successful results; invalidate changed reviews.
- Block discovered project commands instead of running them on the host.
- Replace raw diagnostic content with bounded metadata events.
- Add packaging, offline contract demo, policy explanation, and metadata purge preview.
- Correct legacy tests that previously printed failures without failing pytest.
- Remove private runtime artifacts from the candidate tree, not historical commits.
- Add noncommercial source-available terms. No public release is authorized by this file.
