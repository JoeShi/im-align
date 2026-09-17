# ACP Permissions Are Decided Deterministically From The Spec Root

Status: accepted.

An Alignment Session may produce one or more Spec files whose filenames, subdirectories, formats, and file count are decided by the Agent Backend and the Alignment Skill. We decided that the Bridge never asks a person to approve ACP operations: its Permission Policy allows read-only work once, allows edits once only when every resolved target is inside `agent.spec_root`, and rejects every other restricted operation. The default Spec Root is `docs/specs`; user defaults and repository configuration may override it.

## Considered Options

- Predeclare one `agent.spec_path`: rejected because some Alignment Skills produce multiple files and different Agent Backends choose different filenames.
- Auto-allow the first written path: rejected because it unnecessarily restricts a multi-file Spec and cannot express subdirectories.
- Human Approval in the Thread: rejected because Backend Smoke and IM Integration must run unattended in CI, and a human decision makes identical requests nondeterministic.
- Deterministic policy around the configured Spec Root (chosen): it supports one or many Spec files while keeping source directories and command execution outside the permitted surface.

## Consequences

- Every Backend happy path is non-interactive; no permission card or timeout exists.
- The resolved Spec Root and every write target must be repository-relative, resolve symlinks, and remain inside the current repository.
- The resolved Spec Root is stored with the Session and does not change on resume if configuration changes.
- `read`, `search`, `fetch`, and `think` are allowed once. An explicit `edit` is allowed once only when every extracted target is a file inside the Spec Root. Missing targets, unknown kinds, execution, deletion, movement, and all other restricted operations are rejected.
- The Bridge can enforce this rule only when the Agent Backend emits `session/request_permission` or uses the Bridge fs capability. Backend host configuration must use ask mode; a Backend that bypasses ACP permission requests is outside this enforcement boundary and must be caught by isolated-worktree smoke assertions.
- The completion marker continues to identify one primary Spec file under the Spec Root for compatibility, while other Spec files may exist beside it.
