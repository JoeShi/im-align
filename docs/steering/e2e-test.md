# End-To-End Verification

## Development Feedback Loop

Developing this Skill means closing the loop yourself: after every change to Session, ACP, Permission Policy, completion detection, or Feishu/Lark Provider behavior, the agent must run L1 (unit) and L3 (Backend Smoke), read the result reports, and fix what they report. This is a hard requirement, not an option. Exact commands, prerequisites, and CI mapping per layer live in `docs/test/test-layers.md`; they are not repeated here.

A skip report is not a pass. It signals an incomplete environment — treat it as explicit feedback: either ask for the missing credentials (or backend binaries) so the layer can actually run, or record the limitation as an environment constraint in your handoff. Never let a skipped layer pass silently as if it had run.

The sections below retain the historical real-machine verification methods and the measured per-backend notes gathered before the four-layer harness existed. They remain the record of what was observed on real machines; the operational procedure for layers 2-4 is now `docs/test/test-layers.md`.

## Cold-Start Smoke

```sh
uv sync --frozen
uv run --frozen python scripts/bridge.py --help
```

Use temporary XDG directories to confirm that the CLI loads and error paths are understandable:

```sh
tmpdir=$(mktemp -d)
XDG_CONFIG_HOME="$tmpdir/config" XDG_STATE_HOME="$tmpdir/state" \
  uv run --frozen python scripts/bridge.py status --json
rm -rf "$tmpdir"
```

Expected result: a `run not found` error, not an import or dependency failure.

## ACP Backend Smoke

Measured notes from driving each backend over raw ACP without Feishu/Lark:

- `opencode acp`, `traecli acp serve`, `kiro-cli acp --agent-engine v3 --auth-method cli`, and `kimi acp` can initialize.
- session/new -> prompt -> session/load.
- The Alignment Skill can be reached through `/<skill> <topic>`. kiro-cli has no Skill tool, so the Agent first tries the native kiro Skill registry under user-level `~/.kiro/skills`; after a miss it reads repository `SKILL.md` through fs capability and continues. The first-turn output may include one extra `Load context failed` tool-failure explanation. This is not a Permission Policy rejection and is normal noise.
- Only message chunks are aggregated; kiro `_kiro.dev/` extension notifications and `[INFO]` stderr logs do not interfere with the protocol.
- Permission option kind mapping, cancel, and rejection explanations work. kimi does not trigger request_permission under host default permission; this is expected and documented in `references/feishu-setup.md`.
- opencode session config model, trae-cli argv model, and kiro-cli configOption probing work; when kiro does not provide a model option, warn and do not switch. kimi initialize has no configOptions, so ACP does not switch models.
- fs cannot read or write paths outside cwd. During Session startup, kimi probes `.kimi-code/AGENTS.md`, `AGENTS.md`, and `agents.md` through fs capability; missing-file errors are acceptable.

New backends or ACP client changes must rerun one full set per backend; do not infer compatibility from mocks alone.

## Feishu/Lark Real Machine

Historical record of the manual real-machine procedure used before the four-layer harness. The `bridge.py start`/`wait` flows below are superseded by L2-L4 in `docs/test/test-layers.md` for routine verification; they are kept as the record of what was measured, and remain useful for ad-hoc debugging of a live Session.

### Prerequisites

- Scopes from `references/lark-scopes.json` are approved.
- `im.message.receive_v1` is configured as a long-connection event.
- The bot has joined the test group.
- `bridge.py setup` has written user-level 0600 configuration.
- The current directory is a git worktree with the target Alignment Skill installed.
- Agent Backend is installed and restricted-operation ask rules are enabled. kiro-cli asks by default in the measured version and needs no repository configuration; its user-level `~/.kiro` affects Skill registry and MCP loading, so isolate with `KIRO_HOME` before smoke tests when needed. kimi is governed by `permission` in host `~/.kimi-code/config.toml` and does not ask by default.

### Smoke Level

```sh
uv run --frozen python scripts/bridge.py start \
  "Verify the im-align Feishu/Lark path" --skill grill-with-docs --json
uv run --frozen python scripts/bridge.py wait <run-id> --timeout 600 --json
```

Acceptance:

1. CLI quickly returns run_id and the worker runs in the background.
2. The group receives a new Alignment Session root message.
3. The Thread receives the first Agent card.
4. After the user mentions the bot in the Thread, the message receives an emoji ack.
5. The Agent starts a reply Turn without a debounce delay.
6. `status --json` has populated root_message_id and acp_session_id.

### Full Level

Keep mentioning the bot in the Thread until the Agent writes the Spec and emits the strict completion marker. Acceptance:

- Final state is `done`.
- `spec_path` is inside the launch cwd and is a regular file newly written by this attempt.
- The Thread receives an Alignment Complete card.
- JSON provides a usable backend-native resume command.
- No branch, commit, or push was created.

### Resume And Permission Add-Ons

For state, signal, or ACP load changes: let the Session reach `idle_timeout` or run stop, then `resume`, and confirm the same Thread and session are reused.

For Permission Policy changes: trigger read/search/fetch/think and verify `allow_once`; trigger an Edit inside the Spec Root and verify `allow_once`; trigger Edit outside the root, Bash/execute, delete/move, an unknown kind, and an Edit with no extractable path and verify immediate cancellation. Confirm the Turn watchdog keeps one fixed deadline.

## Cleanup

Stop unfinished workers and confirm `status` is terminal. Delete temporary Specs and test configuration. Redact log excerpts before sharing them.

## Verification Records

Historical verification records, appended by date with run_id, observations, and verdicts, belong in `docs/e2e-records.md`. This file maintains only verification methods and acceptance matrices, not status.
