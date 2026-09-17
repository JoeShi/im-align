"""Participant identity adapters for Feishu-touching harness layers.

Each run selects one explicit identity. Adapters hide identity-specific
credentials and expose only the Bridge allowlist contribution plus the Thread
transport and verifier used by the scripted or LLM-driven Participant.
"""

from dataclasses import dataclass
import json
import urllib.request

from .transports import OpenAPIThreadTransport
from .im_integration import FeishuOpenAPIVerifier


class ParticipantConfigError(ValueError):
    """The selected participant mode is missing required configuration."""


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
    mode: str = "as-user"

    @property
    def extra_participant_open_ids(self) -> list:
        return []

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
        """Read-only Thread checks under the user identity (as-user mode)."""
        return FeishuOpenAPIVerifier(
            user_access_token=self._user_access_token,
            root_message_id=root_message_id,
        )


@dataclass(frozen=True)
class BotParticipantAdapter:
    """User Simulator identity backed by a dedicated customer bot app."""

    _app_id: str
    _app_secret: str
    _simulator_open_id: str
    _bridge_bot_open_id: str
    mode: str = "bot"

    @property
    def extra_participant_open_ids(self) -> list:
        return [self._simulator_open_id]

    @property
    def bridge_bot_open_id(self) -> str:
        return self._bridge_bot_open_id

    def open_thread(self, chat_id: str, root_message_id: str = "", **transport_options):
        return OpenAPIThreadTransport(
            chat_id=chat_id,
            root_message_id=root_message_id,
            app_id=self._app_id,
            app_secret=self._app_secret,
            mention_open_id=self._bridge_bot_open_id,
            **transport_options,
        )

    def open_verifier(self, root_message_id: str = ""):
        """Read-only Thread checks under the dedicated simulator app (bot mode).

        The simulator app carries im:message.group_msg; the Bridge app does
        not, so the Bridge credentials can never serve as the verifier.
        """
        return FeishuOpenAPIVerifier(
            app_id=self._app_id,
            app_secret=self._app_secret,
            root_message_id=root_message_id,
        )


def participant_from_env(mode: str, env, urlopen=None):
    """Build the explicitly selected Participant Adapter from environment data."""
    if mode not in ("as-user", "bot"):
        raise ParticipantConfigError(f"unsupported participant mode: {mode}")
    urlopen = urlopen or urllib.request.urlopen
    bridge_bot_open_id = _configured_or_resolved_open_id(
        env,
        "IM_ALIGN_E2E_BRIDGE_BOT_OPEN_ID",
        "IM_ALIGN_E2E_FEISHU_APP_ID",
        "IM_ALIGN_E2E_FEISHU_APP_SECRET",
        urlopen,
    )
    if mode == "as-user":
        return AsUserParticipantAdapter(
            _user_access_token=_required(env, "IM_ALIGN_E2E_USER_ACCESS_TOKEN"),
            _bridge_bot_open_id=bridge_bot_open_id,
        )
    if mode == "bot":
        return BotParticipantAdapter(
            _app_id=_required(env, "IM_ALIGN_E2E_SIMULATOR_APP_ID"),
            _app_secret=_required(env, "IM_ALIGN_E2E_SIMULATOR_APP_SECRET"),
            _simulator_open_id=_configured_or_resolved_open_id(
                env,
                "IM_ALIGN_E2E_SIMULATOR_OPEN_ID",
                "IM_ALIGN_E2E_SIMULATOR_APP_ID",
                "IM_ALIGN_E2E_SIMULATOR_APP_SECRET",
                urlopen,
            ),
            _bridge_bot_open_id=bridge_bot_open_id,
        )
    raise AssertionError("unreachable participant mode")
