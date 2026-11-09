[简体中文](OVERVIEW.md) | [English](OVERVIEW.en.md)

# Cognitive architecture overview

Alice is an experimental LLM-based digital individual project. The cognition code explores a persistent loop connecting goals, actions, evidence, feedback, and later choices. It does not establish consciousness or reliable long-term learning.

## Main parts

- `state.py` stores scoped episodes, goals, action records, and evidence in a persistent ledger.
- `environment.py` describes currently available observations and actions.
- `decision.py` validates proposed actions against the visible and authorized action set.
- `loop.py` coordinates one bounded decision opportunity and records its result.
- Inquiry, consolidation, social, and self-model modules maintain source-bound interpretations, unresolved questions, feedback, and capability limits.

The intended runtime cycle is: read current state, choose an action or rest, execute only a validated action, record the outcome, and schedule another opportunity. The intended learning cycle compares a recorded expectation with observed feedback, updates a bounded interpretation, and checks whether later behavior changes.

## Evidence limits

Automated checks establish software behavior for their fixtures. Isolated model trials, public-source reads, and a small number of natural actions are separate forms of evidence. None alone establishes durable learning, broad transfer, consciousness, or production reliability. Historical details and operator-specific paths are intentionally kept out of this overview.

## Data boundary

The cognition ledger may contain prompts, model responses, source excerpts, and action results. Provider calls can send request content outside the local machine. Review [Privacy and data handling](../../PRIVACY.en.md) before configuring services or real accounts.
