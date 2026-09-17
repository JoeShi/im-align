# Use ACP As The Unified Agent Abstraction

Status: accepted. The earlier Trae exclusion was superseded by ADR-0003; the implementation language changed to Python in ADR-0004.

Multi-Agent support could use one adapter per vendor, support only one vendor, or unify on ACP. We choose ACP because the protocol natively covers `session/update`, `session/request_permission`, and `session/load`, which map directly to the three core needs: aggregated output, deterministic permission enforcement, and Session resume.

The current Python Bridge uses an auditable minimal NDJSON/JSON-RPC client and implements only the protocol surface this product uses. It does not depend on any backend-private SDK. All supported backends share this implementation.

## Considered Options

- **Write one adapter per vendor**: every new backend would redo output, permission, resume, and file capability behavior, making drift likely.
- **Support only one vendor**: this would not satisfy the goal of choosing Agent Backends and later entering their native sessions.
- **Adopt the full Python ACP SDK directly**: the abstraction is broader, but the current protocol surface is small and key measured defensive logic still needs wrapping. Keep the minimal client first; reassess replacement if the protocol surface or maintenance cost changes.

## Consequences

- Current backends are opencode (`opencode acp`), trae-cli (`traecli acp serve`), kiro-cli (`kiro-cli acp --agent-engine v3 --auth-method cli`, ADR-0003), and kimi (`kimi acp`, native ACP server).
- New backends must verify framing, session/new/load, model setup, permission option kind, post-rejection tool updates, and fs capability.
- Aggregate only `agent_message_chunk` and discard the large volume of thought events.
- Session resume depends on the backend correctly implementing `session/load`; backend session IDs are not interchangeable.
- ACP fs capability allows access only to resolved real paths inside the Session cwd and does not declare terminal capability.
