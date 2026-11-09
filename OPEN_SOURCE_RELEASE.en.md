[简体中文](OPEN_SOURCE_RELEASE.md) | [English](OPEN_SOURCE_RELEASE.en.md)

# Open-source snapshot notes

This repository is public on GitHub. Development is currently paused, and the software remains experimental. Publication does not establish production readiness or a commitment to long-term maintenance.

## Public scope

- Git history starts with a prepared root snapshot and does not include the original local repository history, branches, or tags.
- Local configuration, credentials, cookies, runtime databases, ledgers, logs, chat records, and operator-only materials were excluded.
- QQ call audio components and other third-party dependencies retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.en.md).

## Checks performed

- The public snapshot was scanned for workstation paths, known account identifiers, and high-confidence credential formats. No matches were found. Pattern scans cannot prove that a repository contains no sensitive information.
- `npm audit --package-lock-only --audit-level=high` reported no vulnerabilities.
- OSV returned no advisories for the 18 Python package versions in the lockfile.
- Project tests were not run as part of this open-source preparation.

These vulnerability checks reflect results from 2026-11-09 and may not cover every database or future disclosure. Repeat them when dependencies change.

## Known limitations

- There is no supported one-command setup for the full system. Integrations may need accounts, local services, system components, and provider configuration.
- Some runtime paths send data to external model or search providers, QQ, or websites. Read [privacy and data handling](PRIVACY.en.md) before configuring them.
- There are no maintained release branches or guaranteed security fixes. Report issues according to the [security policy](SECURITY.en.md).
