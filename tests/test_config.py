import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import yaml

from scripts.im_align import config


@contextmanager
def isolated_config_home():
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as root:
        os.environ["XDG_CONFIG_HOME"] = root
        try:
            yield Path(root)
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def write_yaml(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)


class ConfigLoadTests(unittest.TestCase):
    def write_user_config(self, data, mode=0o600):
        path = Path(config.user_config_path())
        write_yaml(path, data, mode)
        return path

    def write_repo_config(self, cwd, data):
        write_yaml(Path(cwd) / ".im-align.yaml", data)

    def user_config(self, defaults=None):
        return {
            "providers": {
                "feishu": {
                    "app_id": "cli_test",
                    "app_secret": "secret",
                    "domain": "feishu",
                }
            },
            "defaults": defaults or {},
        }

    def test_grouped_layers_merge_and_expose_grouped_sections(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                self.user_config(
                    {
                        "im": {"chat_id": "oc_user"},
                        "agent": {"backend": "opencode", "skill": "user-skill"},
                        "timeouts": {
                            "debounce_seconds": 7,
                            "approval_timeout_seconds": 800,
                        },
                        "approval": {"mode": "callback"},
                    }
                )
            )
            self.write_repo_config(
                cwd,
                {
                    "im": {"chat_id": "oc_repo"},
                    "agent": {"model": "repo-model"},
                    "timeouts": {"turn_timeout_seconds": 901},
                },
            )

            loaded = config.load(
                cwd,
                {
                    "skill": "cli-skill",
                    "timeouts": {"idle_timeout_seconds": 3600},
                },
            )

            self.assertEqual(loaded["im"], {"provider": "feishu", "chat_id": "oc_repo"})
            self.assertEqual(
                loaded["agent"],
                {
                    "backend": "opencode",
                    "model": "repo-model",
                    "skill": "cli-skill",
                    "command": "",
                    "args": [],
                },
            )
            self.assertEqual(
                loaded["timeouts"],
                {
                    "debounce_seconds": 7,
                    "approval_timeout_seconds": 800,
                    "turn_timeout_seconds": 901,
                    "idle_timeout_seconds": 3600,
                },
            )
            self.assertEqual(loaded["approval"], {"mode": "callback"})
            self.assertNotIn("chat_id", loaded)
            self.assertNotIn("skill", loaded)
            self.assertNotIn("turn_timeout_seconds", loaded)
            self.assertNotIn("permission", loaded)

    def test_built_in_defaults_are_exposed_as_grouped_sections(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config())

            loaded = config.load(cwd, {"chat_id": "oc_cli"})

            self.assertEqual(loaded["im"], {"provider": "feishu", "chat_id": "oc_cli"})
            self.assertEqual(loaded["agent"]["backend"], "opencode")
            self.assertEqual(loaded["agent"]["skill"], "grill-with-docs")
            self.assertEqual(loaded["timeouts"]["debounce_seconds"], 5)
            self.assertEqual(loaded["approval"], {"mode": "callback"})

    def test_grouped_section_must_be_mapping(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config({"im": {"chat_id": "oc_user"}}))
            self.write_repo_config(cwd, {"timeouts": "soon"})

            with self.assertRaisesRegex(config.ConfigError, "timeouts must be a mapping"):
                config.load(cwd)

    def test_unknown_grouped_key_fails_fast(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config({"im": {"chat_id": "oc_user"}}))
            self.write_repo_config(cwd, {"im": {"thread": "oc_repo"}})

            with self.assertRaisesRegex(config.ConfigError, "im.thread"):
                config.load(cwd)

    def test_same_layer_grouped_and_flat_conflict_fails(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                self.user_config(
                    {
                        "chat_id": "oc_flat",
                        "im": {"chat_id": "oc_grouped"},
                    }
                )
            )

            with self.assertRaisesRegex(config.ConfigError, "chat_id"):
                config.load(cwd)

    def test_user_config_mode_is_enforced_before_loading(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config({"im": {"chat_id": "oc_user"}}), mode=0o644)

            with self.assertRaisesRegex(config.ConfigError, "must have mode 0600"):
                config.load(cwd)


if __name__ == "__main__":
    unittest.main()
