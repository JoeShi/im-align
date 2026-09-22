# Configuration Reference

im-align merges configuration in this priority order: built-in defaults -> user-level defaults -> current repository `.im-align.yaml` -> explicit CLI arguments. Later layers override earlier layers at the field level.

## User-Level Configuration

Path: `~/.config/im-align/config.yaml`; it follows `XDG_CONFIG_HOME` and must be mode `0600`. This is the only place where credentials may be stored:

```yaml
providers:
  work:
    type: feishu # or lark
    app_id: cli_xxx
    app_secret: xxx
    default_chat_id: oc_fallback
defaults:
  im:
    provider: work
  agent:
    backend: opencode # or trae-cli / kiro-cli / kimi
    skill: grill-with-docs
    model: ""
    spec_root: docs/specs # optional; defaults per Skill (docs/adr for grill-with-docs, otherwise docs/specs), see ADR-0005
    command_alias: safe-opencode
  timeouts:
    turn_timeout_seconds: 300
    idle_timeout_seconds: 1800
commands:
  safe-opencode:
    backend: opencode
    command: opencode
    args: ["acp"]
```

Prefer generating this interactively with `python scripts/bridge.py setup`. Do not place secrets manually on the command line.

Provider keys such as `work` are local names. `type: feishu` means domestic Feishu, and `type: lark` means international Lark. There is no user-facing `domain` field.

## Repository-Level Configuration

Path: `.im-align.yaml` at the current git worktree root. It may be committed, but must not contain credentials:

```yaml
im:
  provider: work
  chat_id: oc_xxx
agent:
  backend: opencode
  model: ""
  skill: grill-with-docs
  # Optional (ADR-0005): the only repository-relative directory where the
  # Bridge permits Spec writes. Defaults per Skill: docs/adr for grill-with-docs,
  # otherwise docs/specs. Must not be absolute,
  # equal to '.', or contain '..' components.
  spec_root: docs/specs
  command_alias: safe-opencode
timeouts:
  turn_timeout_seconds: 300
  idle_timeout_seconds: 1800
```

Repository config is strict: old flat fields, the removed `approval` section, `providers`, `app_id`, `app_secret`, `agent.command`, and `agent.args` are rejected. The removed legacy `initiator` field is ignored so existing repositories can upgrade without a migration step.

## Resolution Rules

- Provider selection: CLI `--provider`, then repository `im.provider`, then user `defaults.im.provider`, then single-provider auto-selection.
- Chat selection: CLI `--chat`, then repository `im.chat_id`, then selected provider `default_chat_id`.
- Agent Backend selection: CLI `--backend`, then repository `agent.backend`, then user `defaults.agent.backend`, then the built-in default.
- Command alias selection: CLI `--command-alias`, then repository `agent.command_alias`, then user `defaults.agent.command_alias`; if empty, built-in backend argv is used.
- Spec Root selection: repository `agent.spec_root`, then user `defaults.agent.spec_root`, then the per-Skill default (`docs/adr` for grill-with-docs, otherwise the built-in `docs/specs`).

## Safety Boundaries

- The Permission Policy allows read/search/fetch/think once and allows edits once only when every resolved real target is a file inside `agent.spec_root` (ADR-0005). Execute, delete, move, unknown, outside-root, and unverifiable operations are rejected.
- Bot messages are discarded unconditionally (ADR-0010): the platform never delivers bot-originated messages to the Bridge event stream (ADR-0009), so no allowlist exists.
- Thread messages cannot stop a Session. Use the local `bridge.py stop` command for lifecycle control (ADR-0007).
- The Agent Backend must be configured to ask for native restricted operations. A Backend that bypasses ACP permission requests is outside the Bridge enforcement boundary.
- Command aliases are trusted executable plans and may exist only in user-level `commands`.
- A command alias must declare the same `backend` as the final resolved `agent.backend`.
- Command alias argv is checked for known dangerous parameters such as `bypass_permissions`, `--yolo`, `trust-all-tools`, and `trust-tools`.

Default backend argv, used when `agent.command_alias` is omitted: opencode -> `opencode acp`; trae-cli -> `traecli acp serve`; kiro-cli -> `kiro-cli acp --agent-engine v3 --auth-method cli` using the v3 engine, where missing `--auth-method cli` can hang startup; see ADR-0003; kimi -> `kimi acp`, its native ACP server, and logged-out hosts must run `kimi acp --login` first. `model` affects only opencode through configOption and trae-cli through argv injection. kiro-cli has not been measured to provide a model option, and kimi initialize has no configOptions in 0.42.0 tests. If a backend does not support model switching, the Bridge warns and leaves the model unchanged; kimi uses `default_model` from host `config.toml`.

## State

Runtime state lives in `~/.local/state/im-align/`; it follows `XDG_STATE_HOME`:

- `active.json`: current or most recent run.
- `history.jsonl`: terminal-state history.
- `logs/<run_id>.log`: background Bridge logs.
- `lock`: machine-level single-session lock.

State and log permissions are limited to the current user. They should not be copied into the project repository.
