# Bot Participant Platform Re-Probe

This is a diagnostic procedure for the blocked `bot` Participant Mode retained by ADR-0009 and ADR-0006. It is not a supported acceptance gate. The current measured result is that the simulator bot can post a visible Thread reply, but the platform does not deliver that bot-originated event to the Bridge long connection; therefore no emoji acknowledgement or subsequent Turn appears.

## When To Run

Run this only when Feishu/Lark announces or appears to have changed bot-to-bot event delivery. Routine L2, L3, nightly, and supported Full e2e runs use `as-user`.

## Prerequisites

- The Bridge app and a separate simulator app are both members of the dedicated test group.
- The simulator app has `references/lark-e2e-simulator-scopes.json`, including `im:message.group_msg` for Thread polling and `im:message:send_as_bot` for replies.
- The isolated test configuration allowlists only the simulator bot open_id through `im.extra_participant_open_ids`.
- No other Bridge run holds the machine lock.

Do not broaden the production-facing Bridge app with `im:message.group_at_msg.include_bot:readonly` merely to make this diagnostic pass. ADR-0009 records that this sensitive scope was not validated to solve bot-originated delivery.

## Automated Re-Probe

Run one tuple explicitly:

```sh
uv run --frozen python -m tests.e2e.smoke_runner \
  tests/e2e/scenarios/todo-greenfield \
  --backend kiro-cli \
  --participant-mode bot
```

The current expected evidence is:

- `participant_replied: true` — the simulator identity can list its posted reply in the actual Thread;
- `participant_reply_acked: false` — the Bridge never received the event and added no reaction;
- `participant_drove_turn: false` — no later Bridge-authored Turn follows the reply.

A non-green verdict with exactly this boundary is the expected diagnostic result; do not convert it to a passing or skipped product verdict.

## Re-Enable Criteria

Treat bot mode as supported only after both conditions hold:

1. A direct probe on the Bridge app long connection observes the bot-originated `im.message.receive_v1` event for a Thread reply that mentions the Bridge bot.
2. The Backend Smoke bot tuple passes all deterministic assertions, especially reply visibility, acknowledgement, and the later Bridge-authored Turn.

If both pass, update ADR-0009 and the supported CI matrix in the same change. Until then, keep the default-empty production allowlist as defense in depth and keep nightly automation on `--participant-mode as-user`.
