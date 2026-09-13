# Configuration Reference

im-align merges three configuration layers. Later layers override earlier layers: user-level configuration -> current repository `.im-align.yaml` -> explicit CLI arguments.

## User-Level Configuration

Path: `~/.config/im-align/config.yaml`; it follows `XDG_CONFIG_HOME` and must be mode `0600`. This is the only place where credentials may be stored:

```yaml
providers:
  feishu:
    app_id: cli_xxx
    app_secret: xxx
    domain: feishu # or larksuite
defaults:
  chat_id: oc_xxx
  backend: opencode # or trae-cli / kiro-cli / kimi
  skill: grill-with-docs
  model: ""
```

Prefer generating this interactively with `python scripts/bridge.py setup`. Do not place secrets manually on the command line.

## Repository-Level Configuration

Path: `.im-align.yaml` at the current git worktree root. It may be committed, but must not contain credentials:

```yaml
chat_id: oc_xxx
backend: opencode
model: ""
skill: grill-with-docs
debounce_seconds: 5
approval_timeout_seconds: 600
turn_timeout_seconds: 300
idle_timeout_seconds: 1800
permission: callback
# Optional; avoids resolving identity each time. open_id must belong to
# the Feishu/Lark app used by the im-align bot.
# This is personal information; confirm team policy before committing.
initiator:
  email: developer@example.com
  open_id: ou_xxx
  name: Developer
```

Other allowed override fields:

- `command` + `args`: override the whole backend argv; both must be configured, and the caller is responsible for all arguments.
- `permission: auto_allow`: automatically selects the allow option provided by the Agent, bypassing manual Feishu/Lark Approval. Use only when the user explicitly accepts the risk.

The Bridge rejects startup when argv contains known dangerous parameters such as `bypass_permissions`, `--yolo`, `trust-all-tools`, or `trust-tools`.

Default backend argv, used when `command` + `args` are omitted: opencode -> `opencode acp`; trae-cli -> `traecli acp serve`; kiro-cli -> `kiro-cli acp --agent-engine v3 --auth-method cli` using the v3 engine, where missing `--auth-method cli` can hang startup; see ADR-0003; kimi -> `kimi acp`, its native ACP server, and logged-out hosts must run `kimi acp --login` first. `model` affects only opencode through configOption and trae-cli through argv injection. kiro-cli has not been measured to provide a model option, and kimi initialize has no configOptions in 0.42.0 tests. If a backend does not support model switching, the Bridge warns and leaves the model unchanged; kimi uses `default_model` from host `config.toml`.

## State

Runtime state lives in `~/.local/state/im-align/`; it follows `XDG_STATE_HOME`:

- `active.json`: current or most recent run.
- `history.jsonl`: terminal-state history.
- `logs/<run_id>.log`: background Bridge logs.
- `lock`: machine-level single-session lock.

State and log permissions are limited to the current user. They should not be copied into the project repository.
