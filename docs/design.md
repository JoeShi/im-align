# im-align Design

See `CONTEXT.md` for terminology and `docs/adr/` for architecture decisions. This document describes the Skill-native architecture after ADR-0004.

## Product Boundaries

im-align moves Agent Skill-driven requirements Alignment into an IM Thread so multiple roles can participate and produce a Spec. The developer starts it from a terminal host Agent. im-align ends when the Spec is written locally; it does not code, commit, push, or manage PRs.

## Components

```text
Host Agent
  `- im-align Skill (select parameters, invoke, poll, hand off results)
       └─ Python Bridge worker
            |- IM Provider: Feishu/Lark WebSocket + OpenAPI
            `- ACP client: stdio -> opencode / trae-cli / kiro-cli / kimi
```

### im-align Skill

The root `SKILL.md` is the only product entry point. It is responsible for:

1. Confirming the topic, Alignment Skill, Agent Backend, and optional model in the current git worktree.
2. Running `setup` on first use after user consent.
3. Calling `start --json` and polling with bounded `wait --timeout 600 --json`.
4. Handing off the Spec, Bridge resume command, and backend-native resume command according to the terminal state.

The Skill itself does not perform Alignment and does not keep a host Turn open for hours.

### Bridge

`scripts/bridge.py` is the stable CLI. The background worker keeps both the Feishu/Lark long connection and the Agent Backend subprocess in one process until `done`, `failed`, `idle_timeout`, or `stopped`. State is written to the XDG state directory and does not rely on an in-project database.

### ACP client

The minimal JSON-RPC client implements only the protocol surface currently needed: initialize, session/new/load/prompt/cancel, session/update, session/request_permission, and fs read/write. opencode, trae-cli, kiro-cli, and kimi share the same implementation; differences are limited to argv and model setup.

kiro-cli uses the v3 engine (`kiro-cli acp --agent-engine v3 --auth-method cli`, ADR-0003): v2 is the legacy engine and will be removed, and in v3 the Agent actively reads Skill files through fs capability, while v2 relies on Skill tools absent from the ACP Session. `--auth-method cli` keeps authentication inside the subprocess; otherwise v3 expects the client to implement `_kiro/auth/getAccessToken` and hangs when it is missing. These constraints were measured on 2.21.4. kiro `[INFO]` logs go to stderr and do not pollute stdio. `_kiro.dev/` extension notifications such as metadata and tool_call_chunk previews are ignored as unknown methods. v3 exits slowly, so close uses terminate then kill as a backstop.

kimi uses its native ACP server (`kimi acp`, ADR-0003): it works directly on logged-in hosts; when logged out, initialize returns terminal authMethods and the user must run `kimi acp --login` first. At Session startup it probes `.kimi-code/AGENTS.md`, `AGENTS.md`, and `agents.md` through fs capability; missing-file errors are acceptable and do not affect later work. initialize returns no configOptions, so ACP cannot switch models per Session. With the 0.42.0 host default, file writes do not emit `session/request_permission`; production and CI hosts must configure ask mode for the Bridge Permission Policy to cover native tool calls.

Even when fs requests carry absolute paths, the client resolves real paths and confines them to the Session cwd. The Bridge does not declare terminal capability.

### IM Provider

v1 supports Feishu/Lark only. The Provider abstraction keeps future Slack/DingTalk surfaces open: event polling, text/card send, reactions, and contacts. Message events use a long connection, so no public ingress is required.

## Startup And Lifecycle Control

`start` must run inside a git worktree and does not accept repository aliases or URLs. Startup does not resolve or bind a Feishu/Lark user identity. Any delivered Thread participant message may drive an Alignment Turn. Thread text cannot stop a Session; lifecycle control belongs to the local `bridge.py stop` command (ADR-0007).

## Session And Thread

One Thread maps to one Session. First Turn:

1. Bridge starts the Feishu/Lark connection and Agent Backend.
2. `session/new` creates the ACP session.
3. A Session root message is sent to the configured group.
4. The Agent receives `/<skill> <topic>` plus the completion contract.

Thread replies immediately receive a best-effort emoji ack. Each reply is formatted as `Name: content` and sent to the Agent as soon as the current Turn is available. `session/prompt` blocks until the Turn ends. The Bridge aggregates only `agent_message_chunk`, then sends one card; long text is split around 6000 characters.

Only replies in the root Thread enter the Session. Because of group mention-message scope limits, participants currently must mention the bot when replying in the Thread.

## Permission Policy And Timeouts

`session/request_permission` is answered immediately and without human interaction. The Bridge matches `options[].kind`, never backend-specific `optionId` values:

- `read`, `search`, `fetch`, and `think` are allowed once.
- `edit` is allowed once only when every extracted target resolves to a file under `agent.spec_root`.
- execution, deletion, movement, unknown kinds, missing targets, paths outside the repository, and symlink escapes are rejected.
- If the requested `allow_once` option is absent, the request is rejected instead of widening the grant.

The Turn watchdog has one fixed deadline and never pauses. Failed tool updates supplement an "operation rejected" explanation because some backends end a Turn silently after rejection. The target host must enable backend ask permissions; a backend that performs native writes without emitting `session/request_permission` cannot be constrained by the Bridge policy. CI therefore runs real backends in isolated worktrees and asserts that git changes remain inside the Spec Root.

## Completion Detection

When finishing, the Agent must:

1. Actually write or update the Spec during this Session.
2. Output `[ALIGNMENT_COMPLETE] <repository-relative-path>` on the final line by itself.

The Bridge skips markers inside fenced code blocks, rejects absolute paths and `../` escapes, requires the target to be a regular file, and checks that ModTime is later than the grace window before this start/resume attempt. Only this validation can transition the Session to `done`.

## State And Resume

State directory:

```text
~/.local/state/im-align/
  lock
  active.json
  history.jsonl
  logs/<run_id>.log
```

State machine: `starting -> active -> done|failed|idle_timeout|stopped`.

- `wait` polls state with a bound. The host Agent has no push channel into the background worker, so one `wait` call blocks inside the CLI process until a terminal state or the timeout instead of burning host turns on a polling loop. The bound caps one tool invocation; on timeout the host Agent decides whether to re-wait or hand control back to the developer. It does not treat human-readable logs as protocol.
- `resume` accepts only failed/idle_timeout/stopped, reuses the original Thread, backend, and ACP session, then calls `session/load`.
- `stop` sends SIGTERM to the worker; the worker cleans up and writes stopped.
- If a process exits abnormally and leaves a non-terminal record, the next startup marks it failed.
- After done, the developer receives `opencode -s <session>` or `traecli resume <session>`, but im-align does not run that command. kiro-cli has no measured native resume command yet, so resume uses the Bridge `resume` subcommand.

## Concurrency Constraints

Multiple WebSocket connections for the same Feishu/Lark app randomly shard events by cluster semantics. v1 uses `flock` to ensure only one Bridge worker per machine; deployers must also avoid using the same app on multiple machines at once. True concurrency requires a single-connection router plus multi-session dispatch; do not remove the lock directly.

## Distribution And Configuration

The whole Skill directory is distributed together: `SKILL.md`, `agents/`, `scripts/`, `references/`, `pyproject.toml`, and `uv.lock`. Runtime dependencies and Python >=3.10 are prepared by `uv`.

User-level configuration stores Feishu/Lark credentials and enforces `0600`; repository `.im-align.yaml` allows only non-secret overrides. See `references/config.md` for the full schema.
