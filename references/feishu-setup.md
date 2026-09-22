# Feishu/Lark Custom App Setup

v1 uses a Feishu/Lark long connection to receive message events. No public callback URL is required.

## Creation And Scopes

1. Create a custom enterprise app in the Feishu/Lark Open Platform and add bot capability.
2. In permission management, batch-import `lark-scopes.json` from this directory, apply for scopes, and wait for admin approval.
3. In event settings, choose long-connection event receiving and add `im.message.receive_v1`.
4. Create and publish a version, then add the bot to the group used for Alignment.
5. Get the group `chat_id` (`oc_...`) and run Bridge `setup` to save App ID/App Secret and the default group.

Supported automation uses the test user's OAuth access token (ADR-0009/ADR-0010):
historically a dedicated customer-simulator bot app was tried for the Backend
Smoke User Simulator, but the platform never delivered its messages to the
Bridge event stream, so both the harness bot mode and that app are gone.

## Scope Usage

- `im:message`, `im:message:send_as_bot`: send messages, Thread cards, and emoji acknowledgements.
- `im:message.send_as_user` (user scope): the test user sends Thread messages as a participant; without it the platform rejects sends with `230027` (measured 2026-09-22).
- `im:message:readonly` (user scope): the test user reads chat history while polling for Bridge replies.
- `im:message.reactions:read`: verify that Backend Smoke and Full e2e replies received the Bridge acknowledgement.
- `im:message.group_at_msg:readonly`: receive group messages that mention the bot.
- `im:message.group_at_msg.include_bot:readonly`: a sensitive scope documented by the platform for some bot-message cases. im-align does not require or recommend it for supported automation; ADR-0009 records that it was not validated to solve bot-originated event delivery.
- `im:message.p2p_msg:readonly`: receive direct messages.
- `im:chat:read`: read group information.
- `contact:contact.base:readonly`: display participant names.

## Test User OAuth And CI Rotation

Supported Feishu-touching automation uses a dedicated test user. Complete one interactive OAuth grant for that user under the same `E2E_BRIDGE_FEISHU_APP_ID` used by the workflow, with the message-history, reply, and reaction permissions needed by the harness. Obtain both the access token and refresh token through the authorized CLI's supported credential export or the app's OAuth callback handling; do not print tokens or copy encrypted credential-store files into the repository.

Local runs export the fresh access token as `E2E_SIMULATOR_USER_ACCESS_TOKEN`.

### Extracting The Local Token From The lark-cli Credential Store

The repository ships `extract-lark-token.sh`, which automates the recipe below
against the local store (`E2E_SIMULATOR_USER_ACCESS_TOKEN="$(./extract-lark-token.sh)"`,
`--token refresh` for the refresh token, `--list` to inspect expiry times).

When the interactive grant was completed with lark-cli, the resulting tokens are kept in its local credential store instead of needing a new OAuth round trip. On macOS the store lives at `~/Library/Application Support/lark-cli/` and contains:

- `master.key.file` — a 32-byte raw AES-GCM key (binary, not hex text).
- `cli_<appId>_<userOpenId>.enc` — one file per app and authorized user, holding `nonce (12 bytes) || AES-GCM ciphertext` of a JSON document with `accessToken`, `refreshToken`, `expiresAt`, `refreshExpiresAt` (epoch milliseconds), `scope`, and `grantedAt`.

The user access token expires in about 2 hours and the refresh token in about 30 days; the file's `expiresAt`/`refreshExpiresAt` fields show whether a re-login (`lark-cli auth login`) is due. Extract the token for a specific app and user without printing surrounding noise (measured 2026-09-16):

```bash
cd ~/Library/Application\ Support/lark-cli && uv run --with cryptography --frozen python - <<'EOF'
import json, pathlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

d = pathlib.Path(".")
key = AESGCM((d / "master.key.file").read_bytes())
raw = (d / "cli_<appId>_<userOpenId>.enc").read_bytes()
data = json.loads(key.decrypt(raw[:12], raw[12:], None))
print(data["accessToken"])  # or data["refreshToken"] for CI seeding
EOF
```

Assign the output directly to `E2E_SIMULATOR_USER_ACCESS_TOKEN` (or paste the refresh token into the GitHub secret). Never commit the store files, echo the tokens into a Thread, or archive them with run artifacts. The CI rotation job consumes `E2E_SIMULATOR_USER_REFRESH_TOKEN`; the local access-token extraction above is only needed when running L2/L3 manually.

CI instead stores:

- `E2E_SIMULATOR_USER_REFRESH_TOKEN`: the current rotating refresh token;
- `E2E_SIMULATOR_GITHUB_SECRETS_PAT`: a fine-grained GitHub PAT limited to this repository with **Secrets: write** permission.

Before every serialized Feishu job, `tests/e2e/refresh_user_token.py` exchanges the refresh token for a fresh user access token through the v2 OIDC token endpoint (device-flow JWT tokens are rejected by the legacy v1 refresh endpoint, measured 2026-09-22), registers both generated values with the Actions log masker, and immediately sends the returned replacement refresh token to `gh secret set` over stdin. Secret persistence is retried with bounded backoff and must succeed before the short-lived access token is written to the current job's `GITHUB_ENV`. Feishu invalidates a refresh token after rotation, so both workflows keep refresh and test execution inside `im-align-feishu-single-session`; parallel refreshes would consume the same token and strand one run. The default workflow `GITHUB_TOKEN` cannot update repository Actions secrets and is not a substitute for the restricted PAT.

Refresh tokens also expire (approximately 30 days). If CI has not run within that window, refresh returns an expiry error, or GitHub secret persistence exhausts all retries after a successful Feishu exchange, repeat the interactive grant and replace `E2E_SIMULATOR_USER_REFRESH_TOKEN` manually. Never persist either user token in Bridge configuration, Session state, logs, or transcript archives.

## Known Delivery Limit

With only `im:message.group_at_msg:readonly`, ordinary Thread replies that do not mention the bot are not delivered to the Bridge. Participants must mention the bot when replying in the Thread. If the tenant permits the sensitive scope for receiving all group-chat messages, this limitation can be removed; the code supports both event shapes.

The platform also never delivered messages sent by other bots to the Bridge's `im.message.receive_v1` stream in the 2026-09-16 probe, even when they mentioned the Bridge bot. The Bridge discards bot messages unconditionally (ADR-0010); the historical ADR-0006 allowlist filtered only events the platform delivers, which was none. Supported L2/L3/L4 automation therefore posts and verifies with `E2E_SIMULATOR_USER_ACCESS_TOKEN` (ADR-0009). Do not infer that adding `im:message.group_at_msg.include_bot:readonly` fixes this boundary; restore the harness bot mode from git history only after a direct long-connection probe observes the event and the bot Backend Smoke tuple is green.

## Safety Checks

- App Secret is stored only in user-level configuration, and the file mode must be `0600`.
- Thread messages cannot stop the Session; lifecycle control belongs to the local `bridge.py stop` command (ADR-0007).
- The target repository should enable the backend's ask permission policy. For example, opencode can configure `{"permission":{"edit":"ask","bash":"ask"}}` in `opencode.json`; otherwise the backend may not send permission requests. trae-cli and kiro-cli ask by default in measured versions. Their no-ask tool allowlists are host-level configuration, such as `allowedTools` in `~/.kiro/agents/*.json` for kiro-cli, and should not be injected through argv. kimi is governed by `permission` in host-level `~/.kimi-code/config.toml`; 0.42.0 was measured to allow file writes without asking by default. Set host permission to ask mode so the Bridge Permission Policy can enforce the Spec Root.
- Multiple WebSocket connections for the same Feishu/Lark app randomly shard events. In v1, do not start runs with the same app from multiple machines or processes at the same time.
