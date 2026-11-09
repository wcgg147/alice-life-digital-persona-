# First public release checklist

This checklist describes the current candidate and the remaining work before a public announcement.

## Prepared

- [x] Exclude operator paths, known account identifiers, private operator documents, runtime state, and deployment secrets from the current snapshot.
- [x] Create a new parentless initial history. The original local history remains untouched; never push its `main`, all branches, or all tags.
- [x] Keep credentials, cookies, databases, runtime ledgers, logs, review packets, generated evidence, and local environment files out of the candidate.
- [x] Exclude unrelated product-line documents and local editor/agent artifacts.
- [x] Select MIT for original project files and record the call audio GPL-3.0-only exception in `THIRD_PARTY_NOTICES.md`.
- [x] Scan all 206 snapshot files for workstation paths, email-shaped values, known QQ identifiers, and high-confidence credential formats; no matches remained.
- [x] Run `npm audit --package-lock-only --audit-level=high`; the pinned JavaScript dependency reports 0 vulnerabilities. The lockfile package name and registry URL were normalized for public use.
- [x] Query OSV for all 18 pinned Python packages; no advisories were returned for the locked versions.

## Before publishing

- [x] Review licenses for all 18 pinned Python dependencies and the pinned `playwright-core` dependency; record versions, SPDX identifiers, and sources in `THIRD_PARTY_NOTICES.md`.
- [ ] Complete an owner review of the exact source snapshot, third-party notices, and tests.
- [ ] Replace the project-alias commit email with the maintainer's chosen GitHub noreply address if account attribution is desired.
- [ ] Make the repository public only after the checks above are complete.

## Prepared snapshot

The candidate is branch `codex/alice-public-release`, a single root commit with no parent history. It has no Git remote and has not been pushed. Push only this branch to a new public repository; do not push all refs from the original checkout.

The original checkout still contains local runbooks and historical research records that were excluded from this candidate. Do not copy them in without review. Test fixtures use synthetic account and group identifiers; review any future fixture changes before publication.

The OSV and npm audit results are point-in-time checks and do not guarantee that every vulnerability source is covered. `pip-audit` was not installed. The license inventory was assembled from pinned package metadata and upstream license sources; recheck it whenever the lockfiles change.
