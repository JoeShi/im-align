"""Shared lifecycle and Participant round-trip helpers for Feishu test layers."""

import time

try:
    from .message_protocol import bridge_identity_matches, card_title
except ImportError:  # Direct script entry for IM Integration.
    from message_protocol import bridge_identity_matches, card_title


_HARNESS_ONLY_ENV_PREFIXES = (
    "E2E_BRIDGE_",
    "E2E_SIMULATOR_",
    "E2E_EVALUATOR_",
    "E2E_RUNS_DIR",
)


def bridge_worker_env(base_env, config_home, state_home) -> dict:
    """Build a Bridge environment without forwarding harness-only secrets."""
    env = {
        name: value
        for name, value in base_env.items()
        if not name.startswith(_HARNESS_ONLY_ENV_PREFIXES)
    }
    env["IM_ALIGN_CONFIG_HOME"] = str(config_home)
    env["XDG_STATE_HOME"] = str(state_home)
    return env


def _message_create_time(message: dict) -> float:
    try:
        return int(message.get("create_time", "0")) / 1000
    except (TypeError, ValueError):
        return 0.0


def participant_round_trip_assertions(
    reply_receipts: list,
    thread_messages: list,
    *,
    root_message_id: str,
    bridge_bot_open_id: str,
    acked_message_ids: set,
    bridge_app_id: str = "",
) -> dict:
    """Prove that Participant replies were visible, acknowledged, and drove a Turn."""
    reply_ids = {
        receipt.get("message_id", "")
        for receipt in reply_receipts
        if receipt.get("message_id", "")
    }
    messages = [
        message
        for message in thread_messages
        if message.get("message_id") == root_message_id
        or message.get("root_id") == root_message_id
    ]
    by_id = {message.get("message_id", ""): message for message in messages}
    participant_replied = bool(reply_ids) and reply_ids.issubset(by_id)
    participant_reply_acked = bool(reply_ids) and reply_ids.issubset(acked_message_ids)

    reply_times = []
    for receipt in reply_receipts:
        message_id = receipt.get("message_id", "")
        create_time = float(receipt.get("create_time") or 0.0)
        if not create_time and message_id in by_id:
            create_time = _message_create_time(by_id[message_id])
        if create_time:
            reply_times.append(create_time)
    latest_reply_time = max(reply_times, default=0.0)
    participant_drove_turn = bool(latest_reply_time) and any(
        bridge_identity_matches(
            (message.get("sender") or {}).get("id"),
            (message.get("sender") or {}).get("id_type") or (message.get("sender") or {}).get("sender_type", ""),
            bridge_bot_open_id, bridge_app_id,
        )
        and message.get("msg_type") == "interactive"
        and card_title((message.get("body") or {}).get("content")) in ("🤖 Agent", "⏳ Agent Is Thinking")
        and _message_create_time(message) > latest_reply_time
        for message in messages
    )
    return {
        "participant_replied": participant_replied,
        "participant_reply_acked": participant_reply_acked,
        "participant_drove_turn": participant_drove_turn,
    }


def wait_for_root_message(
    read_status,
    *,
    timeout_seconds: float,
    sleep=time.sleep,
    now=time.monotonic,
) -> str:
    """Wait until a background Bridge exposes the Session's Thread root."""
    deadline = now() + timeout_seconds
    while now() < deadline:
        status = read_status()
        root_message_id = status.get("root_message_id", "")
        if root_message_id:
            return root_message_id
        if status.get("state") in ("done", "failed", "idle_timeout", "stopped"):
            raise RuntimeError(
                f"Bridge reached {status.get('state')} before creating a Thread root"
            )
        sleep(1.0)
    raise RuntimeError("timed out waiting for Bridge Thread root")


def run_guarded_actor(outcome: dict, run, stop_session) -> None:
    """Make an actor thread failure visible and unblock the Bridge wait."""
    try:
        outcome["state"] = run()
    except Exception as e:
        outcome["error"] = e
        try:
            stop_session()
        except Exception as stop_error:
            e.add_note(f"failed to stop Session after Participant failure: {stop_error}")
