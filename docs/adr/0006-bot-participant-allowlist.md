# Allowlisted Bot Participants for CI Customer Simulation

Status: accepted.

The Bridge discards every bot message in the Thread (`FeishuProvider._on_message` drops `sender_type == "bot"`) because group bot traffic is mostly other bots' chatter. The Backend Smoke layer, however, needs a non-human actor to play the customer in CI: there is no user OAuth flow in CI to mint a `user_access_token` for the User Simulator. We decided to add an explicit allowlist — `im.extra_participant_open_ids` — whose bot open_ids pass the defensive filter and drive Turns like a human participant. The default is empty, so production behavior is unchanged.

## Considered Options

- Keep the bot filter absolute and use a `user_access_token` for the simulator: rejected for CI — obtaining and refreshing a user token needs an interactive OAuth grant, which CI secrets cannot hold without building a refresh-token custody story.
- Auto-allow any bot in the chat: rejected — it removes the filter entirely; any bot added to a group would gain the ability to steer the Alignment Turn.
- Allowlist of bot open_ids (chosen): a deliberate, config-visible expansion. The allowlisted bot can drive Turns like any delivered human participant.

## Consequences

- The allowlist exists at both user defaults (`defaults.im.extra_participant_open_ids`) and repository level (`.im-align.yaml` `im.extra_participant_open_ids`); entries must be non-empty `ou_` open_id strings.
- An allowlisted bot's messages are processed exactly like a human participant's: emoji ack, queued Turn, completion detection.
- The list must point only at a dedicated second CI app ("customer bot") whose credentials live in CI secrets; allowlisting a general-purpose bot widens Thread write access to that bot's owner.
- Backend Smoke and Full e2e select an explicit Participant Mode (`as-user` or `bot`). The harness resolves bot open_ids from each app's credentials; explicit open_id environment overrides remain available. In `bot` mode it writes only the selected simulator open_id into the isolated Bridge configuration. Both modes mention the Bridge bot and assert reply ack plus a subsequent Turn.
- The dedicated simulator app has its own test-only scope manifest,
  `references/lark-e2e-simulator-scopes.json`. It needs
  `im:message.group_msg` to poll Agent questions and
  `im:message:send_as_bot` to answer them. These broader read permissions are
  not added to the production Bridge app.
- Lifecycle control remains local to the Bridge CLI (ADR-0007); no Thread participant can stop a Session through message text.
- Platform delivery constraint (measured 2026-09-16): the Feishu/Lark platform does not push other bots' messages to `im.message.receive_v1` even when they mention the bot. An allowlisted bot only reaches the Bridge when the app also holds the sensitive scope `im:message.group_at_msg.include_bot:readonly`; without it the allowlist filters an empty stream. See `references/feishu-setup.md`.
