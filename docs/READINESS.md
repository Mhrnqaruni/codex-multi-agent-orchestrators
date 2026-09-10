# Implementation status

Base: `0b44ea008db47c050889a7d3e9c186cc5480444d`.
Integration: `integration/public-readiness`.
First topic: `hardening/remove-private-artifacts`.

## This change

- Removed 40 real session artifacts and the private incident memo from the
  topic tree; the offline backup and original private history preserve them.
- Added ignores and a tested staged-tree publication guard.
- Replaced unsafe onboarding with inspection-only instructions.
- Added noncommercial licensing, contribution and security documentation.

## Remaining blockers

- Old history still contains private content and personal author metadata.
- Engines still have unsafe authority defaults, content logging, and incomplete
  canonical-engine coverage.
- Role separation, command/environment policy, bounded retries, process-tree
  cleanup, packaging, locking, Windows/Linux CI, fake agents, and demo remain.
- No live-run safety, reviewer write denial, cross-platform pass, or production
  readiness is claimed here.
- Custom licensing terms are not lawyer-reviewed.

Main and visibility stay unchanged. Follow the remaining topics in the owner's
plan after review of this PR. Do not publish this branch or its ancestry. Build
public history separately from reviewed files after all release gates pass.
