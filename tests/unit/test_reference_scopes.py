"""Packaged Feishu scope manifests cover each runtime identity."""

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ScopeManifestTests(unittest.TestCase):
    def test_e2e_simulator_bot_can_read_and_reply_in_group(self):
        manifest = json.loads(
            (ROOT / "references" / "lark-e2e-simulator-scopes.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertTrue(
            {
                "im:message.group_msg",
                "im:message:send_as_bot",
            }.issubset(set(manifest["scopes"]["tenant"]))
        )


if __name__ == "__main__":
    unittest.main()
