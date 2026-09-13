# Verification Records

This file appends historical end-to-end verification records by date, including run_id, observations, and verdicts. Verification methods and acceptance matrices are maintained in `docs/steering/e2e-test.md`; this file records status only and does not define rules.

## Real-Machine Verification Record (2026-09-12)

Using the `im-align-dev` Feishu/Lark test group and opencode 1.18.30, the Skill-native path completed a smoke run:

- After `start`, the background command returned `starting`, then Feishu/Lark WebSocket, `session/new`, the root message, and the first `grill-with-docs` aggregated card all succeeded.
- After the user mentioned the bot in the Thread, the 5-second debounce triggered a second ACP Turn and patched the result back into the same thinking card.
- `stop` reached `stopped` within 15 seconds, cleared PID, sent a stop card to the Thread, and appended terminal state to history.
- `resume` successfully executed `session/load`, reused the original Thread, backend, and ACP session, and stopped normally again.
- Reading the Thread list as the bot identity was denied because sensitive scope `im:message.group_msg` was missing. This is an expected boundary; acceptance uses an authorized user identity for read-only lookup and does not require broader bot scopes.
- This was a path smoke run. Per topic constraints it did not write a Spec, trigger a completion signal, or show Approval cards. Full Spec and Approval scenarios remain covered separately by the matrix in `docs/steering/e2e-test.md`.

## Second Real-Machine Verification Record (2026-09-12)

A second independent two-Turn smoke run used the `im-align-dev` Feishu/Lark test group and opencode 1.18.30:

- Run `run-1789222923-43d432` successfully created the root message, Thread `omt_19c0e8dd140f5cb7`, and ACP session `ses_f6a00b34fffeovWSMGHxWrbS9R`.
- The first `grill-with-docs` question card was sent successfully. After the user identity mentioned the bot in the same Thread, the bot sent an `OK` emoji ack and the debounce triggered a second Turn.
- The second Agent card explicitly confirmed the Feishu/Lark Thread and opencode ACP loop were closed. Under test constraints, no file was written, no tool was executed, and no completion marker was emitted.
- After `stop`, state was `stopped`, PID was cleared, no worker process remained, and the Thread received a stop card. Both `active.json` and `history.jsonl` retained terminal state.
- This run was still a two-Turn path smoke and did not cover the Spec completion signal, Approval cards, or the trae-cli backend.

## Completion Signal And Approval Real-Machine Verification (2026-09-12)

Using the `im-align-dev` Feishu/Lark test group and opencode 1.18.30, the full-level critical path was completed:

- Completion-signal run `run-1789223658-b4e861` actually wrote a unique temporary Spec, and the Agent ended with `[ALIGNMENT_COMPLETE] <path>` on a line by itself. The Bridge validated that the file was under cwd and was a regular file newly written by this Session, then entered `done`; `wait_timed_out=false`, PID was cleared, the Thread received a green `Alignment Complete` card, and history recorded `spec_path`. The temporary Spec was deleted after the test.
- Approval testing used a temporary `opencode.json` setting `bash` to `ask`; a real `pwd` triggered a Thread `Approval Request` card. In allow-path run `run-1789224514-4e7344`, the initiator clicked `Allow once`, the tool executed, the card patched to processed, an Approval record was sent, and the Agent returned the correct cwd.
- In reject-path run `run-1789224684-882e6e`, the initiator clicked `Reject`, the tool did not execute, the card and record both showed rejected-once, and the Bridge synthesized a visible "operation rejected" explanation from the failed tool update.
- The direct ACP harness also confirmed that opencode options use custom optionId values `once/always/reject`, while the Bridge safely maps by `allow_once/allow_always/reject_once` kind. The Turn watchdog is paused while the permission callback runs.
- The first real-machine click exposed a cross-app open_id defect: `lark-cli` and the im-align bot used different Feishu/Lark apps, so the same person had different open_id values. Reusing the wrong value caused "only the Session Initiator can approve." The current code now requires the lark-cli App ID to match the Bridge app; otherwise it falls back to git email and resolves through the Bridge app. The scope template now includes `contact:user.id:readonly`. This repository uses an ignored 0600 `.im-align.yaml` to cache the Bridge-app open_id confirmed by the real callback.
- All Approval runs ended as `stopped`, cleared PID, and left no temporary `opencode.json`, Bridge process, or `opencode acp` process behind.

## kiro-cli Backend Direct ACP Smoke (2026-09-12)

After adding kiro-cli 2.21.4 as the third backend in ADR-0003, the Skill-bundled Python ACP client ran a direct harness smoke in a temporary git directory without Feishu/Lark:

- Default argv `kiro-cli acp --agent-engine v3 --auth-method cli` initialized successfully with protocolVersion 1.
- `session/new` created a Session; one `session/prompt` Turn ended normally with `end_turn` and correct aggregated text.
- `session/load` replay and close with terminate-then-kill backstop worked, covering slow v3 process exit.
- Configuration-layer cases passed: valid kiro-cli backend, explicit `command`/`args` whole-argv override, and startup rejection for `--trust-all-tools` / `--trust-tools`, including equals forms.
- `_extract_tool_error` three-location probe cases passed: opencode `rawOutput.error`, kiro v3 `rawOutput.message`, and kiro v2 `content[]` text blocks, with rawOutput taking precedence.

The full kiro Feishu/Lark Approval-card and completion-signal path was completed later that night on this Bridge; see the `kiro-cli Feishu/Lark Full Real-Machine Verification` and `kiro Approval Wire-Level Probe` sections. The manual-click callback path shares the same card code already verified by opencode, so it was not clicked again.

## kiro-cli Feishu/Lark Full Real-Machine Verification (2026-09-12)

Run `run-1789227267-881b9a` used kiro-cli 2.21.4 v3 engine. The agent used lark-cli to act as the user in the `im-align-dev` group Thread, with initiator `qiaoshi` and `permission: auto_allow`, because interactive card clicks cannot be simulated by CLI; see the wire-level probe for additional evidence:

- Root message -> first grill card with 6 questions -> bot mention reply -> debounced second round with 5 questions -> another reply -> frontier convergence -> `done`, with `spec_path=docs/smoke/kiro-cli-full-e2e.md` inside cwd, a regular file, and ModTime belonging to this attempt. The two reply rounds each took about 3 to 6 minutes.
- Expected kiro-specific noise appeared at the bottom of the first card: `Operation rejected: Load context: grill-with-docs (No skill or auto inclusion steering file found...)`. This is the fallback behavior after the native Skill registry misses. The rejection reason was probed through `rawOutput.message` and synthesized into a visible explanation. It did not block Alignment, because after that tool failure the Agent read repository `SKILL.md` through fs capability and continued.
- Spec writing completed through the kiro Write File tool. Before writing, the Agent probed the target through fs capability and received an expected FileNotFoundError. After writing, the Bridge completion-signal validation passed.
- Cleanup: `.im-align.yaml` `permission: auto_allow` was restored to default callback, the initiator cache was retained, and no bridge/kiro processes remained.

## kiro Approval Wire-Level Probe (2026-09-12)

A direct ACP harness without Feishu/Lark, using a temporary git directory, recorded callback payloads from real kiro v3 `session/request_permission` and exercised both allow and reject paths:

- Real payload: title `Write File`, custom optionId values `accept/always-accept/reject/always-reject`, and standard kind values `allow_once/allow_always/reject_once/reject_always`. The Bridge design of matching by kind holds; using optionId would degrade to cancelled.
- Allow path: returning `PermissionDecision(kind="allow_once")`, from the same source as the `Allow once` button in callback mode, matched `accept`, executed the tool, and wrote the file.
- Reject path: the receipt exception was interpreted by kiro as rejection, the tool did not execute, the rejection reason `The user rejected this tool call.` was extracted through `rawOutput.message` into synthesized explanation, and kiro actively produced explanatory text rather than staying silent. This difference from opencode is recorded as measured behavior.
- Note: the probe callback must return a `PermissionDecision` object rather than a bare outcome dict, because client `_handle_request_permission` dispatches by `decision.kind`.

## kimi Backend Feishu/Lark Full Real-Machine Verification (2026-09-13)

kimi, CLI 0.42.0 with native `kimi acp`, was validated as the fourth backend in two rounds:

**First round run `run-1789239518-ca2eef` (path-level + Approval chain)**: started on the evening of 2026-09-12. Multiple machine sleeps caused repeated Feishu/Lark websocket disconnects while the worker stayed alive and events were delivered late. Measured results:

- Bash triggered `session/request_permission` with title `Bash`; the Approval card rendered normally, and `allow_once` / `allow_always` callbacks were consumed correctly and returned to the Agent.
- The 600-second Approval timeout auto-cancel path passed, with timeout card plus cancelled result.
- Write File did not trigger Approval under the host default `permission`; it was allowed directly. This matches the AGENTS.md safety section: kimi Approval behavior is decided by `permission` in host `~/.kimi-code/config.toml`. Production hosts must explicitly configure ask mode, otherwise write-operation Approval is ineffective for this backend.
- A real defect was found: after the kimi acp subprocess died during a Turn, the client's pending `session/prompt` never completed because the stdout read loop exited without waking slots, and the worker hung until manual stop. This is fixed: `_read_stdout` now calls `_fail_pending` on exit and wakes all waiting RPCs. The run was stopped afterward and smoke artifacts were not committed.

**Second round run `run-1789260380-309274` (full level, successful closeout)**: the agent used lark-cli to act as the user in the `im-align-dev` group Thread, initiator `qiaoshi`, with `permission: callback`:

- Root message -> first grill card (Q1-Q4) -> bot mention reply -> second-round convergence (Q5-Q6) -> empty frontier -> Agent wrote `docs/smoke/kimi-backend-round2.md` through Write File, 2796 bytes -> emitted completion marker on its own line -> Bridge validation passed and entered `done`. Total duration was about 20 minutes.
- Full acceptance passed: state `done`, `spec_path` inside cwd, regular file, ModTime belonging to this attempt, Thread received an `Alignment Complete` card, `bridge_resume_command` was usable, `backend_resume_command` stayed empty because kimi native `-S` session ID namespace has not been validated and is recorded in ADR-0003 plus the client docstring, no branch/commit/push, and no residual processes.
- Difference from the kiro full-level run: kimi showed no Approval card throughout this round. Per the Q4 conclusion, this Approval difference is covered rather than missing, because Bash had triggered Approval and file writes are allowed by default.
- The 60-second worker claim window was sufficient in this round; Defender scanning the venv caused daytime cold starts around 10 to 30 seconds.
