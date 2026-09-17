"""Merge and validate user credentials, repository config, and CLI overrides."""

import os
import stat
from pathlib import PurePosixPath

import yaml

BACKEND_OPENCODE = "opencode"
BACKEND_TRAE_CLI = "trae-cli"
BACKEND_KIRO_CLI = "kiro-cli"
BACKEND_KIMI = "kimi"
PROVIDER_FEISHU = "feishu"
PROVIDER_LARK = "lark"

PROVIDER_TYPE_DOMAINS = {
    PROVIDER_FEISHU: "feishu",
    PROVIDER_LARK: "larksuite",
}
TIMEOUT_KEYS = (
    "turn_timeout_seconds",
    "idle_timeout_seconds",
)

DEFAULTS = {
    "provider": "",
    "chat_id": "",
    "extra_participant_open_ids": [],
    "backend": BACKEND_OPENCODE,
    "model": "",
    "skill": "grill-with-docs",
    "spec_root": "docs/specs",
    "command_alias": "",
    "command": "",
    "args": [],
    "turn_timeout_seconds": 300,
    "idle_timeout_seconds": 1800,
}

GROUPED_SECTION_KEYS = {
    "im": {
        "provider": "provider",
        "chat_id": "chat_id",
        "extra_participant_open_ids": "extra_participant_open_ids",
    },
    "agent": {
        "backend": "backend",
        "model": "model",
        "skill": "skill",
        "spec_root": "spec_root",
        "command_alias": "command_alias",
    },
    "timeouts": {key: key for key in TIMEOUT_KEYS},
}

USER_ALLOWED_KEYS = {
    "providers",
    "defaults",
    "commands",
}

USER_DEFAULT_ALLOWED_SECTIONS = {
    "im": {
        "provider": "provider",
        "extra_participant_open_ids": "extra_participant_open_ids",
    },
    "agent": GROUPED_SECTION_KEYS["agent"],
    "timeouts": GROUPED_SECTION_KEYS["timeouts"],
}

REPO_GROUPED_SECTION_KEYS = {
    "im": GROUPED_SECTION_KEYS["im"],
    "agent": {
        "backend": "backend",
        "model": "model",
        "skill": "skill",
        "spec_root": "spec_root",
        "command_alias": "command_alias",
    },
    "timeouts": GROUPED_SECTION_KEYS["timeouts"],
}

REPO_ALLOWED_KEYS = {
    "im",
    "agent",
    "timeouts",
}

DANGEROUS_ARGV_SUBSTRINGS = (
    "bypass_permissions",
    "--yolo",
    "permission_mode=yolo",
    # kiro-cli permission-bypass switches. Short option -a is too broad for substring
    # blocking, so host-level ~/.kiro/agents/*.json allowedTools remains the backstop.
    "trust-all-tools",
    "trust-tools",
)


class ConfigError(Exception):
    pass


def normalize_spec_root(value):
    """Validate and normalize the writable directory from agent.spec_root."""
    if not isinstance(value, str):
        raise ConfigError("agent.spec_root must be a string")
    value = value.strip()
    if not value:
        raise ConfigError("agent.spec_root must be a non-empty repository-relative directory")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or parsed == PurePosixPath("."):
        raise ConfigError(
            "agent.spec_root must be a repository-relative subdirectory without "
            f"'..' components: {value!r}"
        )
    return parsed.as_posix()


def _validate_extra_participants(value):
    """Validate im.extra_participant_open_ids: list of non-empty ou_ strings.

    Empty list (the default) preserves the current behavior where every bot
    message is discarded (ADR-0006).
    """
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError("im.extra_participant_open_ids must be an array of strings")
    bad = [v for v in value if not v.startswith("ou_")]
    if bad:
        raise ConfigError(
            "im.extra_participant_open_ids entries must be non-empty open_id values "
            f"starting with 'ou_': {bad!r}"
        )
    return list(value)


def user_config_dir():
    # Keep Bridge configuration isolation independent from Agent Backend
    # configuration. Smoke harnesses may set this without hiding opencode's
    # model/provider and Skill configuration from its child process.
    root = os.environ.get(
        "IM_ALIGN_CONFIG_HOME",
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    )
    return os.path.join(root, "im-align")


def user_config_path():
    return os.path.join(user_config_dir(), "config.yaml")


def state_dir():
    root = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(root, "im-align")


def repo_config_path(cwd):
    return os.path.join(cwd, ".im-align.yaml")


def _read_yaml(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"configuration file {path} must be a mapping at the top level")
    return data


def _read_section(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("configuration section must be a mapping")
    return value


def _read_providers(value):
    providers = _read_section(value)
    normalized = {}
    for key, provider in providers.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError("provider keys must be non-empty strings")
        if not isinstance(provider, dict):
            raise ConfigError(f"providers.{key} must be a mapping")
        bad = set(provider) - {"type", "app_id", "app_secret", "default_chat_id"}
        if bad:
            bad_keys = ", ".join(f"providers.{key}.{name}" for name in sorted(bad))
            raise ConfigError(f"user config contains unsupported provider keys: {bad_keys}")
        provider_type = provider.get("type")
        if provider_type not in PROVIDER_TYPE_DOMAINS:
            raise ConfigError(f"providers.{key}.type must be feishu or lark")
        app_id = provider.get("app_id")
        app_secret = provider.get("app_secret")
        if not isinstance(app_id, str) or not app_id:
            raise ConfigError(f"providers.{key}.app_id must be a non-empty string")
        if not isinstance(app_secret, str) or not app_secret:
            raise ConfigError(f"providers.{key}.app_secret must be a non-empty string")
        default_chat_id = provider.get("default_chat_id", "")
        if default_chat_id is not None and not isinstance(default_chat_id, str):
            raise ConfigError(f"providers.{key}.default_chat_id must be a string")
        normalized[key] = {
            "type": provider_type,
            "app_id": app_id,
            "app_secret": app_secret,
            "default_chat_id": default_chat_id or "",
        }
    return normalized


def _read_commands(value):
    commands = _read_section(value)
    normalized = {}
    for alias, plan in commands.items():
        if not isinstance(alias, str) or not alias.strip():
            raise ConfigError("command alias keys must be non-empty strings")
        if not isinstance(plan, dict):
            raise ConfigError(f"commands.{alias} must be a mapping")
        bad = set(plan) - {"backend", "command", "args"}
        if bad:
            bad_keys = ", ".join(f"commands.{alias}.{name}" for name in sorted(bad))
            raise ConfigError(f"user config contains unsupported command keys: {bad_keys}")
        backend = plan.get("backend")
        command = plan.get("command")
        args = plan.get("args", [])
        if backend not in (BACKEND_OPENCODE, BACKEND_TRAE_CLI, BACKEND_KIRO_CLI, BACKEND_KIMI):
            raise ConfigError(f"commands.{alias}.backend is not a supported Agent Backend")
        if not isinstance(command, str) or not command:
            raise ConfigError(f"commands.{alias}.command must be a non-empty string")
        if args is None:
            args = []
        if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
            raise ConfigError(f"commands.{alias}.args must be an array of strings")
        backend_argv(backend, command=command, args=args)
        normalized[alias] = {
            "backend": backend,
            "command": command,
            "args": list(args),
        }
    return normalized


def _resolve_provider_key(configured_key, providers):
    if configured_key is not None and not isinstance(configured_key, str):
        raise ConfigError("im.provider must be a string")
    if configured_key:
        if configured_key not in providers:
            raise ConfigError(f"configured IM provider {configured_key!r} does not exist in user config providers")
        return configured_key
    if len(providers) == 1:
        return next(iter(providers))
    if providers:
        raise ConfigError("multiple IM providers are configured; select one with defaults.im.provider, .im-align.yaml im.provider, or --provider")
    raise ConfigError(f"missing Feishu/Lark credentials; run `bridge.py setup` first to write {user_config_path()}")


def _resolve_command_alias(alias, backend, commands):
    if not alias:
        return "", []
    if alias not in commands:
        raise ConfigError(f"configured command alias {alias!r} does not exist in user config commands")
    plan = commands[alias]
    if plan["backend"] != backend:
        raise ConfigError(f"command alias {alias!r} declares backend {plan['backend']!r}, but agent.backend resolved to {backend!r}")
    return plan["command"], list(plan["args"])


def _normalize_grouped_layer(data, source, grouped_sections, legacy_flat_keys=()):
    """Accept grouped config while reusing the existing flat validation keys."""
    normalized = {}
    legacy_flat_keys = set(legacy_flat_keys)
    for key, value in data.items():
        if key not in grouped_sections:
            if key not in legacy_flat_keys:
                raise ConfigError(f"{source} contains unsupported key: {key}")
            if key in normalized:
                raise ConfigError(
                    f"{source} defines {key!r} more than once; use either the grouped field or the legacy flat field"
                )
            normalized[key] = value
            continue
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ConfigError(f"{source}.{key} must be a mapping")
        allowed = grouped_sections[key]
        bad = set(value) - set(allowed)
        if bad:
            bad_keys = ", ".join(f"{key}.{name}" for name in sorted(bad))
            raise ConfigError(f"{source} contains unsupported grouped keys: {bad_keys}")
        for grouped_key, flat_key in allowed.items():
            if grouped_key not in value or value[grouped_key] is None:
                continue
            if flat_key in normalized:
                raise ConfigError(
                    f"{source} defines {flat_key!r} more than once; use either the grouped field or the legacy flat field"
                )
            normalized[flat_key] = value[grouped_key]
    return normalized


def _with_grouped_sections(cfg):
    grouped = {
        "feishu_app_id": cfg["feishu_app_id"],
        "feishu_app_secret": cfg["feishu_app_secret"],
        "feishu_domain": cfg["feishu_domain"],
    }
    grouped["im"] = {
        "provider": cfg["provider"],
        "type": cfg["provider_type"],
        "chat_id": cfg["chat_id"],
        "extra_participant_open_ids": list(cfg.get("extra_participant_open_ids") or []),
    }
    grouped["agent"] = {
        "backend": cfg["backend"],
        "model": cfg.get("model", ""),
        "skill": cfg["skill"],
        "spec_root": cfg["spec_root"],
        "command_alias": cfg.get("command_alias", ""),
        "command": cfg.get("command", ""),
        "args": list(cfg.get("args", [])),
    }
    grouped["timeouts"] = {key: cfg[key] for key in TIMEOUT_KEYS}
    return grouped


def _check_user_config_mode(path):
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return
    if mode & 0o077:
        raise ConfigError(f"credential file {path} must have mode 0600; current mode is {mode:04o}")


def load(cwd, cli_overrides=None):
    """Compose final config; None values in CLI overrides are treated as unset."""
    _check_user_config_mode(user_config_path())
    user = _read_yaml(user_config_path())
    repo = _read_yaml(repo_config_path(cwd))
    # Pre-ADR-0007 repositories may retain this field. It has no runtime
    # meaning and must not prevent those repositories from starting.
    repo.pop("initiator", None)
    raw_cli_overrides = dict(cli_overrides or {})

    bad_user = set(user) - USER_ALLOWED_KEYS
    if bad_user:
        raise ConfigError(f"user config contains unsupported keys: {', '.join(sorted(bad_user))}")

    bad = set(repo) - REPO_ALLOWED_KEYS
    if bad:
        raise ConfigError(
            f"repository-level .im-align.yaml contains unsupported keys: {', '.join(sorted(bad))}; "
            f"credentials may exist only in {user_config_path()}"
        )

    defaults = _read_section(user.get("defaults"))
    defaults = _normalize_grouped_layer(defaults, "defaults", USER_DEFAULT_ALLOWED_SECTIONS)
    repo = _normalize_grouped_layer(repo, "repository-level .im-align.yaml", REPO_GROUPED_SECTION_KEYS)
    cli_overrides = _normalize_grouped_layer(
        raw_cli_overrides,
        "CLI overrides",
        GROUPED_SECTION_KEYS,
        legacy_flat_keys=set(DEFAULTS) - {"args", "command"} | {"command", "args"},
    )

    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in defaults.items() if v is not None})
    merged.update({k: v for k, v in repo.items() if v is not None})
    merged.update({k: v for k, v in cli_overrides.items() if v is not None})

    providers = _read_providers(user.get("providers"))
    commands = _read_commands(user.get("commands"))
    provider_key = _resolve_provider_key(merged.get("provider", ""), providers)
    provider = providers[provider_key]
    merged["provider"] = provider_key
    merged["provider_type"] = provider["type"]
    if not merged.get("chat_id"):
        merged["chat_id"] = provider["default_chat_id"]
    merged["feishu_app_id"] = provider["app_id"]
    merged["feishu_app_secret"] = provider["app_secret"]
    merged["feishu_domain"] = PROVIDER_TYPE_DOMAINS[provider["type"]]
    merged["command"], merged["args"] = _resolve_command_alias(
        merged.get("command_alias", ""),
        merged["backend"],
        commands,
    )
    validate(merged)
    return _with_grouped_sections(merged)


def validate(cfg):
    cfg["spec_root"] = normalize_spec_root(cfg.get("spec_root"))
    cfg["extra_participant_open_ids"] = _validate_extra_participants(
        cfg.get("extra_participant_open_ids")
    )
    if not isinstance(cfg["provider"], str) or not cfg["provider"]:
        raise ConfigError("im.provider must resolve to a non-empty provider key")
    if cfg["provider_type"] not in PROVIDER_TYPE_DOMAINS:
        raise ConfigError("provider type must be feishu or lark")
    if not cfg["feishu_app_id"] or not cfg["feishu_app_secret"]:
        raise ConfigError(f"missing Feishu/Lark credentials; run `bridge.py setup` first to write {user_config_path()}")
    if cfg["feishu_domain"] not in ("feishu", "larksuite"):
        raise ConfigError("providers.feishu.domain must be feishu or larksuite")
    if not isinstance(cfg["chat_id"], str) or not cfg["chat_id"]:
        raise ConfigError(
            "default group chat_id is not configured; use providers.<key>.default_chat_id, repository im.chat_id, or --chat"
        )
    if cfg["backend"] not in (BACKEND_OPENCODE, BACKEND_TRAE_CLI, BACKEND_KIRO_CLI, BACKEND_KIMI):
        raise ConfigError(f"unsupported agent.backend: {cfg['backend']}")
    if not isinstance(cfg["skill"], str) or not cfg["skill"].strip():
        raise ConfigError("skill must be a non-empty string")
    if cfg.get("command") and not isinstance(cfg["command"], str):
        raise ConfigError("command must be a string")
    if not isinstance(cfg.get("args", []), list) or not all(isinstance(v, str) for v in cfg.get("args", [])):
        raise ConfigError("args must be an array of strings")
    if cfg.get("args") and not cfg.get("command"):
        raise ConfigError("args requires command; overriding argv means taking responsibility for the full argv")
    for key in TIMEOUT_KEYS:
        if not isinstance(cfg[key], int) or cfg[key] <= 0:
            raise ConfigError(f"{key} must be a positive integer")


def backend_argv(backend, model=None, command=None, args=None):
    """Return backend argv; command/args override the built-in defaults as a whole."""
    if command:
        argv = [command] + list(args or [])
    elif backend == BACKEND_OPENCODE:
        argv = ["opencode", "acp"]
    elif backend == BACKEND_TRAE_CLI:
        argv = ["traecli"]
        if model:
            argv += ["-c", f"model={model}"]
        argv += ["acp", "serve"]
    elif backend == BACKEND_KIRO_CLI:
        # v3 engine: v2 is legacy and will be removed. In v3, the Agent reads
        # Skill files through fs capability instead of ACP Session Skill tools,
        # which are absent. Measured on 2.21.4.
        # --auth-method cli keeps auth inside the subprocess; otherwise v3
        # expects _kiro/auth/getAccessToken and hangs without that callback.
        argv = ["kiro-cli", "acp", "--agent-engine", "v3", "--auth-method", "cli"]
    elif backend == BACKEND_KIMI:
        # Native kimi-code ACP server, measured on 0.42.0. Logged-in hosts work
        # directly; logged-out initialize returns terminal authMethods and needs
        # `kimi acp --login` device-code login first. Permission requests are
        # controlled by host ~/.kimi-code/config.toml permission, like
        # opencode.json; argv-level --yolo is intercepted here.
        argv = ["kimi", "acp"]
    else:
        raise ConfigError(f"unsupported agent.backend: {backend}")
    for token in argv:
        for bad in DANGEROUS_ARGV_SUBSTRINGS:
            if bad in token:
                raise ConfigError(f"backend argv contains dangerous parameter {bad!r}; refusing startup because the permission policy is a safety boundary")
    return argv
