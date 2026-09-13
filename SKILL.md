---
name: im-align
description: Move pre-coding requirements alignment into a Feishu/Lark Thread so multiple roles can participate, then let an independent Agent Backend produce a local Spec. Use when the user wants requirements alignment in a Feishu/Lark group, invites multiple people into requirements discussion, asks for remote grill-with-docs/grill-me clarification, or mentions im-align.
---

# im-align

Use the Python Bridge bundled with this Skill to start an independent Agent Backend in the current git repository and bridge the Alignment process into a Feishu/Lark Thread. This Skill is responsible **only for pre-coding Alignment and writing the Spec**; it does not code, commit, push, or create PRs.

## Hard Boundaries

- The current working directory must be the git worktree the user wants to align. Do not accept remote URLs, clone repositories, or switch to another repository.
- Feishu/Lark App Secret may be written only to user-level `~/.config/im-align/config.yaml`. It must not appear in replies, command arguments, repository files, or log excerpts.
- The default permission policy is `callback`. Do not proactively recommend `auto_allow`; do not bypass Feishu/Lark Approval cards unless the user explicitly chooses it.
- Run at most one Alignment on a machine at a time. If a run already exists, use `status` or `wait`; do not start a second Bridge.
- After Alignment completes, report only the Spec and continuation commands. Do not start coding.

## Locate The Bridge

Treat the directory containing this file as `SKILL_DIR`. Run every command from the user's current repository and use absolute paths:

```sh
uv run --project "$SKILL_DIR" python "$SKILL_DIR/scripts/bridge.py" <command>
```

Do not assume the Skill is installed under the repository's `scripts/` directory. If the system has no `uv`, tell the user to install uv and stop; do not fall back to system Python or ad-hoc dependency installation.

## Workflow

### 1. Collect Minimal Parameters

Determine these values from the current conversation and repository context:

- `topic`: the topic to align in this Session.
- `skill`: default to `grill-with-docs`; change it only when the user explicitly specifies another Skill.
- `backend`: default to the configured value; first-time setup defaults to `opencode`.
- `model`: optional; when omitted, keep the backend's current model.
- `chat_id`: prefer the configured default; use `--chat` only when the user provides a temporary override.
- `initiator`: usually resolve automatically from same-app lark-cli, git email, or an interactive terminal prompt. open_id values from other apps cannot be reused; do not ask the user to provide one manually without reason.

If the topic is unclear, ask only for the topic. Do not create extra questions when the other values have safe defaults.

### 2. First-Time Setup

First run a side-effect-free status check:

```sh
uv run --project "$SKILL_DIR" python "$SKILL_DIR/scripts/bridge.py" status --json
```

`run not found` does not mean configuration is missing. If startup reports missing Feishu/Lark credentials, explain that `setup` will write App ID/App Secret under the user directory, then run interactive setup only after explicit user consent:

```sh
uv run --project "$SKILL_DIR" python "$SKILL_DIR/scripts/bridge.py" setup
```

See `references/feishu-setup.md` for Feishu/Lark app scopes and long-connection setup, and `references/config.md` for configuration fields.

### 3. Start

Pass user-provided text safely as one shell argument; do not build commands by string concatenation:

```sh
uv run --project "$SKILL_DIR" python "$SKILL_DIR/scripts/bridge.py" start "$TOPIC" --json
```

Append `--skill`, `--chat`, `--backend`, `--model`, and `--initiator` only when the user specified them. After success, immediately tell the user the run_id, how to participate in the Feishu/Lark group by replying in the new Thread and mentioning the bot, and that the Bridge is running in the background.

### 4. Bounded Wait

Wait at most 600 seconds per call so one host Agent tool invocation does not hang indefinitely:

```sh
uv run --project "$SKILL_DIR" python "$SKILL_DIR/scripts/bridge.py" wait "$RUN_ID" --timeout 600 --json
```

If `wait_timed_out=true` and state is still `starting` or `active`, briefly report that the run is still in progress and continue with another bounded wait if appropriate. Do not read logs instead of `status`; read the tail of `log_path` from JSON only when state is `failed` and the error is insufficient, and redact first.

### 5. Handle Terminal States

- `done`: confirm that `cwd/spec_path` exists; report the Spec path, Feishu/Lark Thread root message ID, and `backend_resume_command`. Stop there and do not code.
- `idle_timeout`: explain that the Session paused due to inactivity; provide `bridge_resume_command` and ask whether to resume.
- `failed`: report `error` and the log path; after fixing the environment issue, use `resume` instead of creating a new run that loses the original Thread.
- `stopped`: explain that the run was stopped by the initiator or terminal; use `resume` if work should continue.

Resume command:

```sh
uv run --project "$SKILL_DIR" python "$SKILL_DIR/scripts/bridge.py" resume "$RUN_ID" --json
```

Stop command:

```sh
uv run --project "$SKILL_DIR" python "$SKILL_DIR/scripts/bridge.py" stop "$RUN_ID" --json
```

## Completion Criteria

Call Alignment complete only when the Bridge returns `state=done` and `spec_path` points to a regular file newly written by this Session inside the current repository. Natural-language "done" messages in Feishu/Lark or historical Spec files in the repository do not count.
