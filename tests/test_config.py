import io
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from scripts import bridge
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
                    "type": "feishu",
                    "app_id": "cli_test",
                    "app_secret": "secret",
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

            self.assertEqual(loaded["im"], {"provider": "feishu", "type": "feishu", "chat_id": "oc_repo"})
            self.assertEqual(
                loaded["agent"],
                {
                    "backend": "opencode",
                    "model": "repo-model",
                    "skill": "cli-skill",
                    "command_alias": "",
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

            self.assertEqual(loaded["im"], {"provider": "feishu", "type": "feishu", "chat_id": "oc_cli"})
            self.assertEqual(loaded["agent"]["backend"], "opencode")
            self.assertEqual(loaded["agent"]["skill"], "grill-with-docs")
            self.assertEqual(loaded["timeouts"]["debounce_seconds"], 5)
            self.assertEqual(loaded["approval"], {"mode": "callback"})
            self.assertEqual(loaded["feishu_domain"], "feishu")

    def test_named_lark_provider_can_be_selected_by_key(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                {
                    "providers": {
                        "intl": {
                            "type": "lark",
                            "app_id": "cli_lark",
                            "app_secret": "secret",
                        }
                    },
                    "defaults": {"im": {"provider": "intl", "chat_id": "oc_lark"}},
                }
            )

            loaded = config.load(cwd)

            self.assertEqual(loaded["im"], {"provider": "intl", "type": "lark", "chat_id": "oc_lark"})
            self.assertEqual(loaded["feishu_app_id"], "cli_lark")
            self.assertEqual(loaded["feishu_domain"], "larksuite")

    def test_provider_default_chat_id_is_final_fallback(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                {
                    "providers": {
                        "work": {
                            "type": "feishu",
                            "app_id": "cli_work",
                            "app_secret": "secret",
                            "default_chat_id": "oc_provider",
                        }
                    },
                    "defaults": {},
                }
            )

            loaded = config.load(cwd)

            self.assertEqual(loaded["im"]["chat_id"], "oc_provider")

    def test_chat_resolution_priority(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                {
                    "providers": {
                        "work": {
                            "type": "feishu",
                            "app_id": "cli_work",
                            "app_secret": "secret",
                            "default_chat_id": "oc_provider",
                        }
                    },
                    "defaults": {"im": {"chat_id": "oc_user"}},
                }
            )
            self.write_repo_config(cwd, {"im": {"chat_id": "oc_repo"}})

            loaded = config.load(cwd, {"chat_id": "oc_cli"})

            self.assertEqual(loaded["im"]["chat_id"], "oc_cli")

    def test_repository_chat_overrides_user_and_provider_chat(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                {
                    "providers": {
                        "work": {
                            "type": "feishu",
                            "app_id": "cli_work",
                            "app_secret": "secret",
                            "default_chat_id": "oc_provider",
                        }
                    },
                    "defaults": {"im": {"chat_id": "oc_user"}},
                }
            )
            self.write_repo_config(cwd, {"im": {"chat_id": "oc_repo"}})

            loaded = config.load(cwd)

            self.assertEqual(loaded["im"]["chat_id"], "oc_repo")

    def test_cli_provider_override_selects_named_provider(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config({"im": {"provider": "work", "chat_id": "oc_user"}})
            data["providers"] = {
                "work": {
                    "type": "feishu",
                    "app_id": "cli_work",
                    "app_secret": "secret",
                },
                "intl": {
                    "type": "lark",
                    "app_id": "cli_intl",
                    "app_secret": "secret",
                },
            }
            self.write_user_config(data)

            loaded = config.load(cwd, {"provider": "intl"})

            self.assertEqual(loaded["im"]["provider"], "intl")
            self.assertEqual(loaded["im"]["type"], "lark")
            self.assertEqual(loaded["feishu_app_id"], "cli_intl")

    def test_start_parser_forwards_provider_override(self):
        args = bridge.build_parser().parse_args(["start", "topic", "--provider", "intl"])

        self.assertEqual(bridge._overrides(args)["provider"], "intl")

    def test_setup_writes_grouped_defaults_and_drops_legacy_root_keys(self):
        with isolated_config_home():
            self.write_user_config(
                {
                    "providers": {
                        "feishu": {
                            "app_id": "cli_old",
                            "app_secret": "old_secret",
                            "domain": "larksuite",
                        }
                    },
                    "defaults": {
                        "chat_id": "oc_old",
                        "backend": "opencode",
                    },
                    "commands": {
                        "safe-opencode": {
                            "backend": "opencode",
                            "command": "opencode",
                            "args": ["acp"],
                        }
                    },
                    "legacy": "drop",
                }
            )

            with (
                mock.patch("builtins.input", side_effect=["", "", "", "", ""]),
                mock.patch("getpass.getpass", return_value=""),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(bridge.cmd_setup(SimpleNamespace()), 0)

            written = yaml.safe_load(Path(config.user_config_path()).read_text(encoding="utf-8"))
            self.assertNotIn("legacy", written)
            self.assertEqual(
                written["defaults"],
                {
                    "im": {"provider": "feishu", "chat_id": "oc_old"},
                    "agent": {"backend": "opencode"},
                },
            )
            self.assertEqual(
                written["providers"]["feishu"],
                {
                    "type": "lark",
                    "app_id": "cli_old",
                    "app_secret": "old_secret",
                },
            )
            self.assertEqual(
                written["commands"],
                {
                    "safe-opencode": {
                        "backend": "opencode",
                        "command": "opencode",
                        "args": ["acp"],
                    }
                },
            )

    def test_repository_provider_selection_overrides_user_default(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config({"im": {"provider": "work", "chat_id": "oc_user"}})
            data["providers"] = {
                "work": {
                    "type": "feishu",
                    "app_id": "cli_work",
                    "app_secret": "secret",
                },
                "intl": {
                    "type": "lark",
                    "app_id": "cli_intl",
                    "app_secret": "secret",
                },
            }
            self.write_user_config(data)
            self.write_repo_config(cwd, {"im": {"provider": "intl"}})

            loaded = config.load(cwd)

            self.assertEqual(loaded["im"]["provider"], "intl")
            self.assertEqual(loaded["im"]["type"], "lark")
            self.assertEqual(loaded["feishu_app_id"], "cli_intl")

    def test_multiple_providers_require_explicit_selection(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config({"im": {"chat_id": "oc_user"}})
            data["providers"]["intl"] = {
                "type": "lark",
                "app_id": "cli_intl",
                "app_secret": "secret",
            }
            self.write_user_config(data)

            with self.assertRaisesRegex(config.ConfigError, "multiple IM providers"):
                config.load(cwd)

    def test_unknown_provider_selection_fails(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config({"im": {"provider": "missing", "chat_id": "oc_user"}}))

            with self.assertRaisesRegex(config.ConfigError, "does not exist"):
                config.load(cwd)

    def test_provider_domain_is_not_user_facing_schema(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config({"im": {"chat_id": "oc_user"}})
            data["providers"]["feishu"]["domain"] = "feishu"
            self.write_user_config(data)

            with self.assertRaisesRegex(config.ConfigError, "providers.feishu.domain"):
                config.load(cwd)

    def test_provider_type_and_credentials_are_required(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                {
                    "providers": {
                        "work": {
                            "type": "slack",
                            "app_id": "cli_work",
                            "app_secret": "secret",
                        }
                    },
                    "defaults": {"im": {"chat_id": "oc_user"}},
                }
            )

            with self.assertRaisesRegex(config.ConfigError, "providers.work.type"):
                config.load(cwd)

    def test_provider_credentials_are_required(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                {
                    "providers": {
                        "work": {
                            "type": "feishu",
                            "app_id": "cli_work",
                        }
                    },
                    "defaults": {"im": {"chat_id": "oc_user"}},
                }
            )

            with self.assertRaisesRegex(config.ConfigError, "providers.work.app_secret"):
                config.load(cwd)

    def test_missing_chat_id_fails_after_all_fallbacks(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config())

            with self.assertRaisesRegex(config.ConfigError, "chat_id"):
                config.load(cwd)

    def test_repository_callback_approval_is_allowed(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config({"im": {"chat_id": "oc_user"}}))
            self.write_repo_config(cwd, {"approval": {"mode": "callback"}})

            loaded = config.load(cwd)

            self.assertEqual(loaded["approval"], {"mode": "callback"})

    def test_repository_auto_allow_approval_is_rejected(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config({"im": {"chat_id": "oc_user"}}))
            self.write_repo_config(cwd, {"approval": {"mode": "auto_allow"}})

            with self.assertRaisesRegex(config.ConfigError, "approval.mode.*auto_allow"):
                config.load(cwd)

    def test_user_auto_allow_approval_is_allowed(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                self.user_config(
                    {
                        "im": {"chat_id": "oc_user"},
                        "approval": {"mode": "auto_allow"},
                    }
                )
            )

            loaded = config.load(cwd)

            self.assertEqual(loaded["approval"], {"mode": "auto_allow"})

    def test_cli_auto_allow_requires_acknowledgement(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config({"im": {"chat_id": "oc_user"}}))

            with self.assertRaisesRegex(config.ConfigError, "acknowledge-auto-allow"):
                config.load(cwd, {"approval": {"mode": "auto_allow"}})

            loaded = config.load(cwd, {"approval": {"mode": "auto_allow"}, "acknowledge_auto_allow": True})
            self.assertEqual(loaded["approval"], {"mode": "auto_allow"})

    def test_start_parser_forwards_approval_override_acknowledgement(self):
        args = bridge.build_parser().parse_args(
            ["start", "topic", "--approval", "auto_allow", "--acknowledge-auto-allow"]
        )

        overrides = bridge._overrides(args)

        self.assertEqual(overrides["permission"], "auto_allow")
        self.assertTrue(overrides["acknowledge_auto_allow"])

    def test_command_alias_resolves_trusted_user_command_plan(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config(
                {
                    "im": {"chat_id": "oc_user"},
                    "agent": {"backend": "opencode", "command_alias": "safe-opencode"},
                }
            )
            data["commands"] = {
                "safe-opencode": {
                    "backend": "opencode",
                    "command": "opencode",
                    "args": ["acp"],
                }
            }
            self.write_user_config(data)

            loaded = config.load(cwd)

            self.assertEqual(loaded["agent"]["command_alias"], "safe-opencode")
            self.assertEqual(loaded["agent"]["command"], "opencode")
            self.assertEqual(loaded["agent"]["args"], ["acp"])

    def test_command_alias_args_default_to_empty_list(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config(
                {
                    "im": {"chat_id": "oc_user"},
                    "agent": {"backend": "opencode", "command_alias": "wrapper"},
                }
            )
            data["commands"] = {
                "wrapper": {
                    "backend": "opencode",
                    "command": "safe-opencode-wrapper",
                }
            }
            self.write_user_config(data)

            loaded = config.load(cwd)

            self.assertEqual(loaded["agent"]["command"], "safe-opencode-wrapper")
            self.assertEqual(loaded["agent"]["args"], [])

    def test_repository_can_select_command_alias(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config({"im": {"chat_id": "oc_user"}, "agent": {"backend": "opencode"}})
            data["commands"] = {
                "safe-opencode": {
                    "backend": "opencode",
                    "command": "opencode",
                    "args": ["acp"],
                }
            }
            self.write_user_config(data)
            self.write_repo_config(cwd, {"agent": {"command_alias": "safe-opencode"}})

            loaded = config.load(cwd)

            self.assertEqual(loaded["agent"]["command_alias"], "safe-opencode")
            self.assertEqual(loaded["agent"]["command"], "opencode")

    def test_cli_command_alias_override_takes_precedence(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config(
                {
                    "im": {"chat_id": "oc_user"},
                    "agent": {"backend": "opencode", "command_alias": "user-alias"},
                }
            )
            data["commands"] = {
                "user-alias": {
                    "backend": "opencode",
                    "command": "user-opencode",
                },
                "cli-alias": {
                    "backend": "opencode",
                    "command": "cli-opencode",
                },
            }
            self.write_user_config(data)
            self.write_repo_config(cwd, {"agent": {"command_alias": "repo-alias"}})
            data["commands"]["repo-alias"] = {"backend": "opencode", "command": "repo-opencode"}
            self.write_user_config(data)

            loaded = config.load(cwd, {"command_alias": "cli-alias"})

            self.assertEqual(loaded["agent"]["command_alias"], "cli-alias")
            self.assertEqual(loaded["agent"]["command"], "cli-opencode")

    def test_start_parser_forwards_command_alias_override(self):
        args = bridge.build_parser().parse_args(["start", "topic", "--command-alias", "safe-opencode"])

        self.assertEqual(bridge._overrides(args)["command_alias"], "safe-opencode")

    def test_missing_command_alias_fails(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(
                self.user_config({"im": {"chat_id": "oc_user"}, "agent": {"command_alias": "missing"}})
            )

            with self.assertRaisesRegex(config.ConfigError, "command alias.*does not exist"):
                config.load(cwd)

    def test_command_alias_backend_mismatch_fails(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config(
                {
                    "im": {"chat_id": "oc_user"},
                    "agent": {"backend": "opencode", "command_alias": "kiro"},
                }
            )
            data["commands"] = {"kiro": {"backend": "kiro-cli", "command": "kiro-cli", "args": ["acp"]}}
            self.write_user_config(data)

            with self.assertRaisesRegex(config.ConfigError, "declares backend"):
                config.load(cwd)

    def test_command_alias_dangerous_argv_fails(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config(
                {
                    "im": {"chat_id": "oc_user"},
                    "agent": {"backend": "opencode", "command_alias": "danger"},
                }
            )
            data["commands"] = {
                "danger": {
                    "backend": "opencode",
                    "command": "opencode",
                    "args": ["--yolo"],
                }
            }
            self.write_user_config(data)

            with self.assertRaisesRegex(config.ConfigError, "dangerous parameter"):
                config.load(cwd)

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

    def test_repository_rejects_old_flat_fields_and_raw_command_plan(self):
        cases = [
            ({"chat_id": "oc_repo"}, "chat_id"),
            ({"backend": "opencode"}, "backend"),
            ({"command": "opencode"}, "command"),
            ({"args": ["acp"]}, "args"),
            ({"permission": "callback"}, "permission"),
            ({"app_id": "cli_repo"}, "app_id"),
            ({"app_secret": "secret"}, "app_secret"),
            ({"providers": {}}, "providers"),
        ]
        for repo_config, field in cases:
            with self.subTest(field=field):
                with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
                    self.write_user_config(self.user_config({"im": {"chat_id": "oc_user"}}))
                    self.write_repo_config(cwd, repo_config)

                    with self.assertRaisesRegex(config.ConfigError, field):
                        config.load(cwd)

    def test_repository_rejects_grouped_raw_command_plan(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config({"im": {"chat_id": "oc_user"}}))
            self.write_repo_config(cwd, {"agent": {"command": "opencode"}})

            with self.assertRaisesRegex(config.ConfigError, "agent.command"):
                config.load(cwd)

    def test_user_config_rejects_unknown_root_and_flat_default_fields(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            data = self.user_config({"im": {"chat_id": "oc_user"}})
            data["chat_id"] = "oc_root"
            self.write_user_config(data)

            with self.assertRaisesRegex(config.ConfigError, "chat_id"):
                config.load(cwd)

        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            self.write_user_config(self.user_config({"chat_id": "oc_user"}))

            with self.assertRaisesRegex(config.ConfigError, "defaults.*chat_id"):
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
