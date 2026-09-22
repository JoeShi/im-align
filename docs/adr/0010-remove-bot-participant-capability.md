# Remove the Bot Participant Capability from Harness and Product

Status: accepted. This ADR supersedes ADR-0006 (allowlisted bot participants) and amends ADR-0009 (which expected the bot Participant Mode to remain selectable as a platform re-probe).

ADR-0009 measured, on 2026-09-16 real-machine runs, that the Feishu/Lark platform never delivers bot-originated group messages to another app's `im.message.receive_v1` long connection — not even when the message mentions the receiving bot. A bot Participant therefore can answer in the Thread yet can never drive a Turn, because the Bridge never sees its messages. The capability built for that identity consequently has zero live consumers: the harness `bot` Participant Mode exists only to re-probe a delivery path that does not exist, and the ADR-0006 allowlist (`im.extra_participant_open_ids`) filters an event stream that never carries bot events. Production keeps the list empty by policy, so removing it changes no production behavior. `as-user` becomes the only participant mode.

## Considered Options

- Keep both facets as dormant re-probe seams: rejected — dead code with credentials, scopes manifests, config surface, and CI secrets that must be maintained and security-reviewed forever, for a capability that cannot work on today's platform.
- Remove the harness bot mode but keep the product allowlist: rejected — the allowlist's only consumer was the harness bot mode; keeping the config field would preserve an unverifiable security surface (a config-visible expansion of who can drive Turns) with no working caller.
- Remove both facets in one decision (chosen): the harness bot Participant Mode and the product bot allowlist were introduced together (ADR-0006) and are consumed together; they are removed together and would be restored together.

## Decision

One ADR covers both facets.

Harness removal:

- `--participant-mode` (with `bot` and `all` values) is deleted from `tests/e2e/smoke_runner.py`; the run matrix is (Scenario, Agent Backend) only.
- `BotParticipantAdapter` is deleted from `tests/e2e/participants.py`; `participant_from_env(env)` builds only the as-user adapter. The Bridge bot open_id resolution (`_resolve_bot_open_id` / `_configured_or_resolved_open_id`, with the `E2E_BRIDGE_BOT_OPEN_ID` override) stays, because the as-user simulator still mentions the Bridge bot.
- The tenant_access_token (app credential) path is removed from `OpenAPIThreadTransport` (`tests/e2e/transports.py`) and from `FeishuOpenAPIVerifier` (`tests/e2e/im_integration.py`); only the user_access_token paths remain.
- The `E2E_SIMULATOR_BOT_*` environment variables, `references/lark-e2e-simulator-scopes.json`, and `docs/test/bot-participant-smoke.md` are deleted; `.env.example` and the CI smoke workflow no longer reference them.

Product removal:

- `im.extra_participant_open_ids` is removed from the configuration schema (`scripts/im_align/config.py`): `DEFAULTS`, the grouped `im` sections, the `_validate_extra_participants` validator, and the grouped output.
- `scripts/bridge.py` no longer passes `extra_participants` to the IM provider.
- `FeishuProvider` (`scripts/im_align/im_providers/feishu.py`) drops the allowlist frozenset; the bot-sender filter becomes an unconditional discard of `sender_type == "bot"` messages, with the discard logged at debug level. Bot messages in the Thread are discarded, period.

## Consequences

- User or repository configs that still carry `im.extra_participant_open_ids` fail loudly at startup with an unsupported-key error. This is intended: silent acceptance of a dead security-relevant field is worse than a clear migration signal. Delete the field to fix.
- The ADR-0009 measurement facts remain valid and are kept: the platform constraint, the thread-container listing behavior, and the card-patching dedup constraint still shape the user-identity transports and verifiers.
- If the platform ever delivers bot-originated events to the Bridge event stream, restore the harness bot mode and the product allowlist together from git history (ADR-0006/ADR-0009/this ADR point at each other), then re-run the bot-mode Backend Smoke matrix to green before treating bot participants as supported.
- The Bridge's default bot-discard remains the standing behavior for group hygiene: unrelated bots' chatter never drives Turns.
