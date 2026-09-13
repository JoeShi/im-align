# Python Static And Unit Verification

This file describes baseline verification after ADR-0004. See `docs/steering/e2e-test.md` for real-machine paths.

## Every Code Change

```sh
uv sync --frozen
uv run --frozen python -m py_compile \
  scripts/bridge.py \
  scripts/im_align/*.py \
  scripts/im_align/acp/*.py \
  scripts/im_align/im_providers/*.py
uv run --frozen python scripts/bridge.py --help
git diff --check
```

Also use temporary `XDG_CONFIG_HOME` and `XDG_STATE_HOME` to verify no-credential and no-run error paths without polluting the developer's real state.

## Test Conventions

Use Python standard-library `unittest` for new tests unless a concurrency or async behavior truly needs an additional framework. Use `tempfile.TemporaryDirectory()` for temporary directories. Set mtime explicitly for time-sensitive logic instead of relying on real waiting. Use fake boundaries for Feishu/Lark and ACP integration; do not send messages to real groups from unit tests.

Prioritize coverage for:

- Configuration layering, 0600 mode, and dangerous argv.
- Worktree checks and identity fallback.
- ACP framing, message aggregation, permission kind, rejection explanations, and repository path boundaries.
- Watchdog pause/resume.
- Completion-signal fenced-code handling, path escape rejection, and freshness.
- Atomic state writes, single-session lock, and resume.
- Session orchestration with fake Provider + fake ACP.

This repository does not yet have a coverage baseline. Do not reuse legacy Go numbers. Set thresholds from real reports after the Python test suite exists; do not claim coverage is complete because zero tests passed.
