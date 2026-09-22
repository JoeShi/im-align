"""Structural card and identity fields used by Feishu harness assertions."""

import json


def card_title(raw) -> str:
    try:
        card = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return ""
    if not isinstance(card, dict):
        return ""
    # Message reads normalize Card 1.0 headers to a top-level title string.
    header = card.get("header")
    header = header if isinstance(header, dict) else {}
    title = card.get("title") or header.get("title", "")
    if isinstance(title, dict):
        title = title.get("content", "")
    return title if isinstance(title, str) else ""


def bridge_identity_matches(identifier, identity_type, open_id, app_id="") -> bool:
    if identity_type in ("app", "app_id"):
        return bool(app_id) and identifier == app_id
    if identity_type in ("", "user", "open_id"):
        return bool(open_id) and identifier == open_id
    return False
