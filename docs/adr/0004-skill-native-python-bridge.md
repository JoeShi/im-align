# Invert Architecture To A Skill-Native Python Bridge

Status: accepted

The identity-chain and Thread-stop portions of this decision are superseded by ADR-0007.

The previous implementation was a precompiled Go single binary plus SQLite plus a resident Feishu/Lark bot. The entry point was on the IM side, and repositories were prepared through repository aliases. New requirements: distribute as an Agent Skill; let the developer trigger it from the terminal in the target repository; keep the Bridge using an independent Agent Backend session for Alignment so the developer can later enter that backend and continue work; keep IM integration extensible.

Decision: use **im-align Skill + independent Python Bridge process inside the Skill**. One side of the Bridge holds the IM Provider long connection, with v1 using the `lark-oapi` Feishu/Lark Provider. The other side drives the Agent Backend with an auditable minimal ACP JSON-RPC client. Dependencies are managed by `uv`, `pyproject.toml`, and `uv.lock`. `start` daemonizes and returns immediately; the host Agent polls with bounded `wait`/`status`; the worker exits after Spec validation completes.

The default Alignment Skill is `grill-with-docs`; users may explicitly override it.

## Considered Options

- **Host Agent aligns inside its own Turn**: rejected because it cannot satisfy independent backend/model requirements or native session handoff, and the host Turn should not hang for hours.
- **Keep Go and add bridge subcommands**: rejected because it still requires precompiled distribution and conflicts with the goal of shipping scripts and a locked runtime in the Skill package. Legacy code moved to `legacy/go/`.
- **Bridge orchestrates send/receive through lark-cli**: smaller implementation but too coupled to Feishu/Lark. The Bridge uses the Provider abstraction instead.
- **Browser OAuth or claim-confirm strong identity gate**: stronger guarantee but high cost. This was initially deferred in favor of a lightweight identity chain; ADR-0007 later removed identity binding entirely.
- **Full ACP Python SDK**: the current protocol surface is small and stable, and measured defensive logic still needs to be preserved. Keep the minimal client and reassess if the protocol surface expands.

## Consequences

- **Narrower repository boundary**: do not accept URLs or clone repositories. Use the launch-time cwd directly, and require it to be a git worktree. ACP fs is also confined to that worktree.
- **Spec is local only**: do not create branches or commit/push. Provide a backend-native resume command at the end, but im-align does not enter coding.
- **Identity chain (superseded by ADR-0007)**: the original repository config -> lark-cli -> git email -> interactive email chain was removed after contact lookup became a startup single point of failure.
- **Stable machine protocol**: the host parses only `--json` output and not logs; background logs are written independently to the XDG state directory.
- **Global single-session lock**: multiple connections for the same Feishu/Lark app randomly shard events. v1 allows one worker per machine, and deployments must not reuse the same app across machines.
- **Distribution unit**: `SKILL.md`, `agents/`, `scripts/`, `references/`, `pyproject.toml`, and `uv.lock` must be installed together.
- The protocol and multi-backend conclusions from ADR-0001 and ADR-0003 remain valid; this ADR revises the git-delivery and independent coding-session wording in ADR-0002.
- Legacy Go code stays in `legacy/go/` until Python cold start, both backend ACP smoke checks, and the full Feishu/Lark path pass, but it is not the current product entry point.
