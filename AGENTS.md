# AGENTS.md

This file is for coding Agents maintaining im-align. Use `CONTEXT.md` as the terminology source, `docs/adr/` for architecture decisions, and `docs/design.md` for the full interaction model and lifecycle.

## Project Overview

im-align is an IM ↔ Coding Agent alignment tool distributed as an Agent Skill. The host Agent triggers the root `SKILL.md` inside the developer's current git repository. The Skill starts an independent Python Bridge; one side connects to a Feishu/Lark Thread, and the other side drives opencode, trae-cli, kiro-cli, or kimi through ACP. The final Spec is written back to the current repository.

Scope is strictly limited to pre-coding work: no coding, no branch creation, no commit or push, and no PR management.

## Do

- Use Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, ...) for commit messages when running git commands.
- Use Mermaid syntax for architecture and process diagrams.

## Don't

## Current Architecture

- **Entry**: root `SKILL.md`; the default Alignment Skill is `grill-with-docs`.
- **Runtime**: Python >=3.10 managed by `uv` plus root `pyproject.toml`/`uv.lock`; no system-level pip install is required.
- **Bridge CLI**: `scripts/bridge.py` provides `setup/start/wait/status/resume/stop`; `start` and `resume` launch an independent background worker.
- **Agent integration**: `scripts/im_align/acp/` is a minimal ACP JSON-RPC client; all four backends share the same implementation.
- **IM integration**: `scripts/im_align/im_providers/`; v1 supports Feishu/Lark only, receiving message events through a long connection.
- **State**: `~/.local/state/im-align/active.json + history.jsonl + logs/`; SQLite is no longer used.

## Key Files

- `SKILL.md` - complete host Agent orchestration protocol and safety boundaries.
- `scripts/bridge.py` - lifecycle entry point, daemonization, and stable JSON output.
- `scripts/im_align/orchestrator.py` - Thread events, debouncing, Turns, and completion cleanup.
- `scripts/im_align/permission_policy.py` - deterministic ACP decisions derived from the Spec Root.
- `scripts/im_align/acp/client.py` - ACP framing, event aggregation, permission requests, and repository-scoped fs capability.
- `scripts/im_align/completion.py` - completion marker and current-Session Spec validation.
- `scripts/im_align/config.py` - user-level credentials, repository-level non-secret configuration, and argv safety validation.
- `references/` - Feishu/Lark and configuration references distributed with the Skill.
- `docs/test/test-layers.md` - authoritative run guide for the four test layers (commands, credentials, skip semantics, CI mapping).
- `docs/steering/unit-test.md` - test writing conventions.
- `docs/steering/e2e-test.md` - developer self-test feedback loop (run L1 + L3, surface skips) plus historical real-machine notes.
- `docs/e2e-harness-design.md` - implementation contract for the four-layer harness in `tests/e2e/`; supersedes manual-only e2e procedures for layers 2-4.
- `docs/e2e-records.md` - historical verification records, appended by date; status-like content belongs there, not in the testing guide files.

## Development And Verification

Run at least the following after every Python code change:

```sh
uv sync --frozen
uv run --frozen python -m py_compile scripts/bridge.py scripts/im_align/*.py scripts/im_align/acp/*.py scripts/im_align/im_providers/*.py tests/e2e/*.py tests/unit/*.py
uv run --frozen python scripts/bridge.py --help
git diff --check
```

When changing Session, ACP, Permission Policy, completion detection, or the Feishu/Lark Provider behavior, run the matching smoke checks from `docs/steering/e2e-test.md`. A passing legacy Go build is not acceptance evidence for the Python implementation.

## Code And Terminology

- Comments, documentation, configuration descriptions, and user-facing errors must be English; identifiers remain English.
- Use the terms in `CONTEXT.md`: Alignment, im-align Skill, Alignment Skill, Bridge, Session, Thread, Turn, Agent Backend, Spec, Spec Root, Permission Policy.
- Comments should explain why and record measured constraints; do not restate obvious code behavior.
- Errors should preserve causal chains; background failures go to logs, while CLI errors stay concise for users.
- The Skill prompt owns selection and invocation protocol; deterministic work belongs in scripts, not copied into `SKILL.md`.

## Non-Negotiable Defensive Logic

- Aggregate only `agent_message_chunk`; discard `agent_thought_chunk`, which is about 95% of the stream.
- Match Permission Policy decisions to `options[].kind`, never to backend-specific `optionId` values. Prefer `allow_once`; reject when the requested semantic option is unavailable.
- Permission decisions must be immediate and fail closed. Do not add human interaction or pause the Turn watchdog.
- After rejection, the Agent may be silent in opencode tests, while kiro-cli produces explanatory text. Synthesize a visible explanation from failed `tool_call_update`. Rejection reasons appear in different measured locations, so `_extract_tool_error` must keep probing `rawOutput.error`, `rawOutput.message`, and `content[]` text blocks.
- `[ALIGNMENT_COMPLETE]` must be on a line by itself, ignored inside fenced code blocks, restricted to repository-relative paths, and validated against a file ModTime from this attempt.
- ACP fs capability must resolve symlinks and keep the real path inside the current repository.
- A Session is bound to the Agent Backend that created it; resume must not switch backend or Thread.
- Multiple long connections for the same Feishu/Lark app randomly shard events. Do not remove the machine-level single-session lock unless a single-connection multi-session router is implemented first.

## Security

- App Secret may exist only in user-level `~/.config/im-align/config.yaml`, which must have mode `0600`; repository `.im-align.yaml` must not contain credentials.
- The Permission Policy allows read/search/fetch/think once, allows an edit once only when all resolved targets are files inside `agent.spec_root`, and rejects execute/delete/move/unknown or unverifiable operations (ADR-0005).
- Bot messages in the Thread are discarded unconditionally: the platform never delivers bot-originated messages to the Bridge event stream (ADR-0009), and the ADR-0006 allowlist was removed with the harness bot mode (ADR-0010).
- Startup must reject argv containing dangerous parameters such as `bypass_permissions`, `--yolo`, `trust-all-tools`, or `trust-tools`. kiro-cli short option `-a` cannot be blocked by substring safely, so host-level `allowedTools` in `~/.kiro/agents/*.json` remains the backstop.
- Do not accept repository URLs and do not clone; the runtime scope is the cwd git worktree at launch.
- Thread messages never stop a Session. Lifecycle control belongs to the local `bridge.py stop` command (ADR-0007).
- kiro-cli loads user-level `~/.kiro` resources, including agents, skills, and MCP servers. Production hosts should redirect the whole environment with `KIRO_HOME`. Its default ask behavior was measured in 2.21.4, unlike opencode which needs `opencode.json` ask rules in the target repository.
- kimi loads user-level `~/.kimi-code`, including `config.toml`, skills, and MCP. Permission behavior is controlled by `permission` in `config.toml`: the 0.42.0 default allows file writes without asking. Production hosts must explicitly configure ask mode and review skills/MCP, otherwise the Bridge Permission Policy cannot see or constrain native tool calls.
- Legacy local `im-align.yaml`/`im-align.db` files from the Go implementation may contain historical sensitive data. Never commit them.

## Agent skills

### Issue tracker

Issues and specs live as GitHub issues in JoeShi/im-align (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.
