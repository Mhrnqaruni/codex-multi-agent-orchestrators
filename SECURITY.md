# Security policy

This alpha is undergoing private hardening. No version is supported for
production or untrusted-project use. Permission bypass defaults have been
removed, but the [threat model](docs/THREAT_MODEL.md) and
[release gates](docs/READINESS.md) document important remaining limitations.

Use the established private channel with the repository owner to report a
vulnerability. If GitHub private vulnerability reporting is enabled, use
**Security → Report a vulnerability**. Otherwise request a private reporting
route without posting exploit details publicly. No response-time SLA is offered.

Provide affected version/commit, synthetic reproduction, impact, and a proposed
mitigation if known. Do not send live credentials, private source, raw sessions,
personal paths, or real prompts in public issues or pull requests.

Never use the full private Git history as a public security artifact. The
publication guard and secret scanner are defense-in-depth checks, not proof that
all PII, secrets, or exploitable behaviors have been eliminated.
