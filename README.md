# Alice — Digital Individual Research

Alice is an open research project exploring how an LLM-based digital individual can maintain continuity, choose goals, act in an online environment, and revise beliefs from evidence and feedback.

The project is experimental software. It does not establish consciousness, reliable self-directed learning, or production readiness. A successful test or a single natural action is evidence only for the behavior that was actually tested.

## Project status

Development is currently paused. The repository contains several subsystems at different maturity levels, including online reading, a QQ integration, persistent memory, and a cognitive loop. Some integrations depend on accounts, local services, provider APIs, platform access, or credentials that are not included here. There is no supported one-command setup for the full system.

The cognitive architecture aims to keep two related cycles connected:

1. A runtime cycle that reads state, chooses an action or rest, records the outcome, and schedules another opportunity.
2. A learning cycle that compares an expected result with observed evidence, updates a bounded and retractable interpretation, and checks whether later choices change.

See the public [cognition overview](docs/cognition/OVERVIEW.md) for the architecture and its evidence limits. Operator runbooks and dated experiment records are local research material and are not part of the public source snapshot.

## Repository layout

- `alice/cognition/` — persistent state, decisions, inquiry, feedback, and self-model mechanisms.
- `alice/agent/`, `alice/engine/`, `alice/life/`, `alice/memory/`, `alice/persona/` — reading, actions, memory, and persona code.
- `alice/qq/` — optional QQ channel integration.
- `alice/surf/` — online exploration and reading workflows.
- `deployment/` — runtime entry points and deployment support.
- `docs/` — curated architecture and research documentation.

## Requirements and setup

The cognition runtime currently targets Python 3.12. Its pinned dependencies are listed in `deployment/cognition/requirements.txt` and `deployment/cognition/requirements.lock`. Other integrations have additional system, account, and provider requirements. Review the relevant subsystem documentation before running anything that can contact external services or write persistent state.

The cognition entry point is `deployment/cognition/run.sh`. It can start model calls and read external sources; do not run it until provider configuration, the state path, and the intended network permissions are understood. The project does not ship credentials or a public demo account.

The optional social-demand filter accepts `ALICE_IGNORED_BOT_QQ_IDS` as a comma-separated list. Supply account identifiers through private deployment configuration; they are not embedded in the source.

## Data and privacy

Runtime data can include conversation text, persona and relationship memories, source excerpts, model inputs and outputs, action receipts, and usage metrics. Depending on the configured feature, data may be sent to an LLM provider, a search provider, QQ, or a public website. Local configuration and local storage do not mean that every operation stays on the device.

Read [Privacy and data handling](PRIVACY.md) before configuring an account. The current code does not provide one complete deletion path across every integration and storage backend.

## Security

Do not commit `.env` files, provider keys, cookies, QQ configuration, databases, runtime ledgers, logs, or captured conversations. See [Security policy](SECURITY.md) to report a vulnerability.

## License

Original project files are licensed under MIT; see [LICENSE](LICENSE). The call audio components identified in [third-party notices](THIRD_PARTY_NOTICES.md) are GPL-3.0-only and remain under that license. Other third-party components retain their own licenses.

## Contributions

The project is not actively maintained at this time. Issues and pull requests may not receive a timely response. Before proposing a change, read the relevant design and evidence-limit documentation in `docs/`.
