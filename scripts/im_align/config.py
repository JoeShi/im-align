"""Merge and validate user credentials, repository config, and CLI overrides."""

import os
import stat

import yaml

BACKEND_OPENCODE = "opencode"
BACKEND_TRAE_CLI = "trae-cli"
BACKEND_KIRO_CLI = "kiro-cli"
BACKEND_KIMI = "kimi"
POLICY_CALLBACK = "callback"
POLICY_AUTO_ALLOW = "auto_allow"

TIMEOUT_KEYS = (
    "debounce_seconds",
    "approval_timeout_seconds",
    "turn_timeout_seconds",
    "idle_timeout_seconds",
)

DEFAULTS = {
    "provider": "feishu",
    "chat_id": "",
    "backend": BACKEND_OPENCODE,
    "model": "",
    "skill": "grill-with-docs",
    "command": "",
    "args": [],
    "debounce_seconds": 5,
    "approval_timeout_seconds": 600,
    "turn_timeout_seconds": 300,
    "idle_timeout_seconds": 1800,
    "permission": POLICY_CALLBACK,
}

GROUPED_SECTION_KEYS = {
    "im": {
        "provider": "provider",
        "chat_id": "chat_id",
    },
    "agent": {
        "backend": "backend",
        "model": "model",
        "skill": "skill",
        "command": "command",
        "args": "args",
    },
    "timeouts": {key: key for key in TIMEOUT_KEYS},
    "approval": {
        "mode": "permission",
    },
}

REPO_ALLOWED_KEYS = {
    "chat_id",
    "backend",
    "model",
    "skill",
    "command",
    "args",
    "debounce_seconds",
    "approval_timeout_seconds",
    "turn_timeout_seconds",
    "idle_timeout_seconds",
    "permission",
    "initiator",
    "im",
    "agent",
    "timeouts",
    "approval",
}

DANGEROUS_ARGV_SUBSTRINGS = (
    "bypass_permissions",
    "--yolo",
    "permission_mode=yolo",
    # kiro-cli no-approval switches. Short option -a is too broad for substring
    # blocking, so host-level ~/.kiro/agents/*.json allowedTools remains the backstop.
    "trust-all-tools",
    "trust-tools",
)


class ConfigError(Exception):
    pass


def user_config_dir():
    root = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
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


def _normalize_grouped_layer(data, source):
    """Accept grouped config while reusing the existing flat validation keys."""
    normalized = {}
    for key, value in data.items():
        if key == "initiator":
            if value is not None and not isinstance(value, dict):
                raise ConfigError(f"{source}.initiator must be a mapping")
            normalized[key] = value
            continue
        if key not in GROUPED_SECTION_KEYS:
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
        allowed = GROUPED_SECTION_KEYS[key]
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
    if "initiator" in cfg and cfg["initiator"] is not None:
        if not isinstance(cfg["initiator"], dict):
            raise ConfigError("initiator must be a mapping")
        grouped["initiator"] = cfg["initiator"]
    grouped["im"] = {
        "provider": cfg["provider"],
        "chat_id": cfg["chat_id"],
    }
    grouped["agent"] = {
        "backend": cfg["backend"],
        "model": cfg.get("model", ""),
        "skill": cfg["skill"],
        "command": cfg.get("command", ""),
        "args": list(cfg.get("args", [])),
    }
    grouped["timeouts"] = {key: cfg[key] for key in TIMEOUT_KEYS}
    grouped["approval"] = {
        "mode": cfg["permission"],
    }
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

    bad = set(repo) - REPO_ALLOWED_KEYS
    if bad:
        raise ConfigError(
            f"repository-level .im-align.yaml contains unsupported keys: {', '.join(sorted(bad))}; "
            f"credentials may exist only in {user_config_path()}"
        )

    defaults = _read_section(user.get("defaults"))
    defaults = _normalize_grouped_layer(defaults, "defaults")
    repo = _normalize_grouped_layer(repo, "repository-level .im-align.yaml")
    cli_overrides = _normalize_grouped_layer(cli_overrides or {}, "CLI overrides")

    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in defaults.items() if v is not None})
    merged.update({k: v for k, v in repo.items() if v is not None})
    merged.update({k: v for k, v in cli_overrides.items() if v is not None})

    providers = user.get("providers") or {}
    feishu = _read_section(providers.get("feishu"))
    merged["feishu_app_id"] = feishu.get("app_id", "")
    merged["feishu_app_secret"] = feishu.get("app_secret", "")
    merged["feishu_domain"] = feishu.get("domain", "feishu")
    validate(merged)
    return _with_grouped_sections(merged)


def validate(cfg):
    if cfg["provider"] != "feishu":
        raise ConfigError(f"unsupported IM Provider: {cfg['provider']}; v1 supports only feishu")
    if not cfg["feishu_app_id"] or not cfg["feishu_app_secret"]:
        raise ConfigError(f"missing Feishu/Lark credentials; run `bridge.py setup` first to write {user_config_path()}")
    if cfg["feishu_domain"] not in ("feishu", "larksuite"):
        raise ConfigError("providers.feishu.domain must be feishu or larksuite")
    if not isinstance(cfg["chat_id"], str) or not cfg["chat_id"]:
        raise ConfigError("default group chat_id is not configured; use defaults.chat_id or --chat")
    if cfg["backend"] not in (BACKEND_OPENCODE, BACKEND_TRAE_CLI, BACKEND_KIRO_CLI, BACKEND_KIMI):
        raise ConfigError(f"unsupported agent.backend: {cfg['backend']}")
    if not isinstance(cfg["skill"], str) or not cfg["skill"].strip():
        raise ConfigError("skill must be a non-empty string")
    if cfg["permission"] not in (POLICY_CALLBACK, POLICY_AUTO_ALLOW):
        raise ConfigError("permission must be callback or auto_allow")
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
        # `kimi acp --login` device-code login first. Approval behavior is
        # controlled by host ~/.kimi-code/config.toml permission, like
        # opencode.json; argv-level --yolo is intercepted here.
        argv = ["kimi", "acp"]
    else:
        raise ConfigError(f"unsupported agent.backend: {backend}")
    for token in argv:
        for bad in DANGEROUS_ARGV_SUBSTRINGS:
            if bad in token:
                raise ConfigError(f"backend argv contains dangerous parameter {bad!r}; refusing startup because Approval is a safety boundary")
    return argv
