"""Participant identity adapter for Feishu-touching harness layers.

The User Simulator always runs under the dedicated test-user identity: the
platform never delivers bot-originated messages to the Bridge event stream
(ADR-0009), so a bot participant can never drive a Turn and ADR-0010 removed
the bot mode entirely. The adapter hides identity-specific credentials and
exposes only the Thread transport and verifier used by the scripted or
LLM-driven Participant.
"""

from dataclasses import dataclass
import json
import urllib.request

from .transports import OpenAPIThreadTransport
from .verifier import FeishuOpenAPIVerifier


class ParticipantConfigError(ValueError):
    """The participant identity is missing required configuration."""


def _required(env, name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise ParticipantConfigError(f"missing participant configuration: {name}")
    return value


def _open_id(env, name: str) -> str:
    value = _required(env, name)
    if not value.startswith("ou_"):
        raise ParticipantConfigError(f"{name} must be an ou_ open_id")
    return value


def _resolve_bot_open_id(app_id: str, app_secret: str, urlopen) -> str:
    try:
        token_request = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(
                "utf-8"
            ),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(token_request, timeout=30) as response:
            token_payload = json.loads(response.read().decode("utf-8"))
        if token_payload.get("code") != 0:
            raise ParticipantConfigError(
                "cannot resolve bot identity: "
                + token_payload.get("msg", "token request failed")
            )

        info_request = urllib.request.Request(
            "https://open.feishu.cn/open-apis/bot/v3/info/",
            headers={
                "Authorization": "Bearer " + token_payload["tenant_access_token"]
            },
        )
        with urlopen(info_request, timeout=30) as response:
            info_payload = json.loads(response.read().decode("utf-8"))
        open_id = (info_payload.get("bot") or {}).get("open_id", "")
        if info_payload.get("code") != 0 or not open_id.startswith("ou_"):
            raise ParticipantConfigError(
                "cannot resolve bot identity: "
                + info_payload.get("msg", "open_id missing")
            )
        return open_id
    except ParticipantConfigError:
        raise
    except Exception as e:
        raise ParticipantConfigError(f"cannot resolve bot identity: {e}") from e


def _configured_or_resolved_open_id(
    env,
    open_id_name: str,
    app_id_name: str,
    app_secret_name: str,
    urlopen,
) -> str:
    configured = env.get(open_id_name, "")
    if configured:
        return _open_id(env, open_id_name)
    return _resolve_bot_open_id(
        _required(env, app_id_name),
        _required(env, app_secret_name),
        urlopen,
    )


@dataclass(frozen=True)
class AsUserParticipantAdapter:
    """User Simulator identity backed by a user_access_token."""

    _user_access_token: str
    _bridge_bot_open_id: str

    @property
    def bridge_bot_open_id(self) -> str:
        return self._bridge_bot_open_id

    def open_thread(self, chat_id: str, root_message_id: str = "", **transport_options):
        return OpenAPIThreadTransport(
            user_access_token=self._user_access_token,
            chat_id=chat_id,
            root_message_id=root_message_id,
            mention_open_id=self._bridge_bot_open_id,
            **transport_options,
        )

    def open_verifier(self, root_message_id: str = ""):
        """Read-only Thread checks under the user identity."""
        return FeishuOpenAPIVerifier(
            user_access_token=self._user_access_token,
            root_message_id=root_message_id,
        )


def participant_from_env(env, urlopen=None):
    """Build the as-user Participant Adapter from environment data."""
    urlopen = urlopen or urllib.request.urlopen
    bridge_bot_open_id = _configured_or_resolved_open_id(
        env,
        "E2E_BRIDGE_BOT_OPEN_ID",
        "E2E_BRIDGE_FEISHU_APP_ID",
        "E2E_BRIDGE_FEISHU_APP_SECRET",
        urlopen,
    )
    return AsUserParticipantAdapter(
        _user_access_token=_required(env, "E2E_SIMULATOR_USER_ACCESS_TOKEN"),
        _bridge_bot_open_id=bridge_bot_open_id,
    )
