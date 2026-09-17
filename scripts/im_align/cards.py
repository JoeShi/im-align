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
