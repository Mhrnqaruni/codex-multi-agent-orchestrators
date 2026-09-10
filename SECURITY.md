# Security status

This experimental repository is undergoing private hardening. No released
version is supported for production or untrusted-project execution. Engines
still have approval/sandbox bypass defaults and content logging. Removing
tracked logs does not repair runtime behavior.

Use the existing private communication channel with the repository owner for
security reports. If GitHub private vulnerability reporting is enabled, use
Security > Report a vulnerability. Otherwise request a private reporting route
without posting exploit details. Never post credentials, session IDs, raw
prompts, personal paths, or private target files in public issues or PRs.

Publication requires role enforcement, command and environment policies,
bounded retries/process cleanup, privacy defaults, and a fresh history audit.
The lightweight tracked-tree guard is one layer, not a security certification.
