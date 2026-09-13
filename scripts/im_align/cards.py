"""Feishu/Lark Card 1.0 JSON builders, ported from legacy/go/internal/lark/message.go."""

import json


def simple_card(template, title, md):
    return json.dumps(
        {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md}}],
        },
        ensure_ascii=False,
    )


def result_card(text):
    return simple_card("wathet", "🤖 Agent", text)


def approval_button(name, kind, approval_id):
    btn_type = "default"
    if kind.startswith("allow"):
        btn_type = "primary"
    if kind.startswith("reject"):
        btn_type = "danger"
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": name},
        "type": btn_type,
        "value": {
            "im_align": "approval",
            "req_id": approval_id,
            "kind": kind,
        },
    }


def approval_card(approval_id, req, approval_wait_seconds):
    """Body shows toolCall.title, kind, and rawInput.

    Buttons come from options and match by kind semantics, not optionId.
    rawInput is truncated above 800 runes.
    """
    raw = json.dumps(req.raw_input, ensure_ascii=False, indent=2)
    if len(raw) > 800:
        raw = raw[:800] + "...(truncated)"
    md = (
        f"**Operation**: {req.title}\n"
        f"**Type**: {req.kind}\n"
        f"**Arguments**:\n```\n{raw}\n```\n\n"
        f"Only the Session Initiator can approve. The request is cancelled after {approval_wait_seconds} seconds."
    )
    buttons = []
    for opt in req.options:
        buttons.append(approval_button(opt.name or opt.kind, opt.kind, approval_id))
    return json.dumps(
        {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "yellow",
                "title": {"tag": "plain_text", "content": "🔐 Approval Request"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": md}},
                {"tag": "action", "actions": buttons},
            ],
        },
        ensure_ascii=False,
    )
