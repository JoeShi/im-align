# Feishu/Lark Custom App Setup

v1 uses Feishu/Lark long connections to receive message events and card callbacks. No public callback URL is required.

## Creation And Scopes

1. Create a custom enterprise app in the Feishu/Lark Open Platform and add bot capability.
2. In permission management, batch-import `lark-scopes.json` from this directory, apply for scopes, and wait for admin approval.
3. In event and callback settings, choose long-connection event receiving and add `im.message.receive_v1`.
4. In callback settings, choose long-connection callback receiving and add `card.action.trigger`. Approval buttons do not work without it.
5. Create and publish a version, then add the bot to the group used for Alignment.
6. Get the group `chat_id` (`oc_...`) and run Bridge `setup` to save App ID/App Secret and the default group.

## Scope Usage

- `im:message`, `im:message:send_as_bot`: send messages, Thread cards, and emoji acknowledgements.
- `im:message.group_at_msg:readonly`: receive group messages that mention the bot.
- `im:message.p2p_msg:readonly`: receive direct messages.
- `im:chat:read`: read group information.
- `contact:contact.base:readonly`: display participant names.
- `contact:user.id:readonly`: resolve the initiator open_id under the im-align bot app by email. Feishu/Lark open_id values are app-scoped, so values returned by `lark-cli whoami` for another app cannot be reused.

## Known Delivery Limit

With only `im:message.group_at_msg:readonly`, ordinary Thread replies that do not mention the bot are not delivered to the Bridge. Participants must mention the bot when replying in the Thread. If the tenant permits the sensitive scope for receiving all group-chat messages, this limitation can be removed; the code supports both event shapes.

## Safety Checks

- App Secret is stored only in user-level configuration, and the file mode must be `0600`.
- With `permission: callback`, only the initiator open_id resolved at run startup can approve write operations or stop the Session.
- The target repository should enable the backend's ask permission policy. For example, opencode can configure `{"permission":{"edit":"ask","bash":"ask"}}` in `opencode.json`; otherwise the backend may not send Approval requests. trae-cli and kiro-cli ask by default; kiro-cli 2.21.4 was measured to trigger Approval on file writes. Their no-approval tool allowlists are host-level configuration, such as `allowedTools` in `~/.kiro/agents/*.json` for kiro-cli, and should not be injected through argv. kimi is governed by `permission` in host-level `~/.kimi-code/config.toml`; 0.42.0 was measured to allow file writes without Approval by default. Set host permission to ask mode before expecting Approval cards with kimi.
- Multiple WebSocket connections for the same Feishu/Lark app randomly shard events. In v1, do not start runs with the same app from multiple machines or processes at the same time.
