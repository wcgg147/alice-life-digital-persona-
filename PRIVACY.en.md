[简体中文](PRIVACY.md) | [English](PRIVACY.en.md)

# Privacy and data handling

Alice can process personal and conversational data. This document describes the current software boundaries; it is not a claim that every path has been independently audited or that the project is suitable for sensitive or production use.

## Data the software may handle

Depending on enabled features and configuration, the software may read or store:

- messages, account identifiers, group identifiers, and delivery receipts;
- persona, relationship, and interaction memories;
- model prompts and responses, search results, and excerpts from public pages;
- action history, source references, usage counts, and diagnostic metrics.

Local databases and logs may therefore contain personal or sensitive material. Protect them as private data and do not publish them as examples.

## External services

When enabled, configured LLM and search providers receive the inputs required for their requests. QQ integration communicates with QQ through the configured connector. Reading features request pages from external websites. Each service has its own retention, access, and privacy terms; review those terms and configure only services you intend to use.

The project does not promise that prompts, messages, or excerpts remain on the local device. Avoid sending sensitive personal data to services unless you have reviewed and accepted the applicable terms and settings.

## Storage and deletion

Credentials and runtime state are expected to be supplied locally and kept outside version control. The cognition ledger has owner-scoped export, revoke, and delete operations. These operations do not delete all QQ channel data, persona database records, provider-side data, backups, or every other store used by the project. There is not yet a single end-to-end export or deletion mechanism across all integrations.

Before using real accounts, identify each enabled store and its backup location. Do not assume deleting one database removes copies held elsewhere.

## Safe use

- Use test accounts and synthetic conversations during evaluation.
- Keep configuration, credentials, cookies, local databases, runtime logs, and captured evidence private.
- Disable integrations you do not need and grant only the permissions required for the feature.
- Review the data sent to model and search providers before enabling real conversations.
- Do not treat a public website's availability as permission to bypass its access controls or terms.
