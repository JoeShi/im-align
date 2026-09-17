# im-align

An IM alignment tool distributed as an Agent Skill. It moves a Coding Agent's requirements-alignment loop into a Feishu/Lark group thread so Product, Tech Lead, Operations, and other roles can participate together. The final output is a Spec written to the current git repository. Scope is strictly limited to pre-coding alignment.

## Language

**Alignment**:
The process where multiple roles answer the Agent's questions and converge on shared requirements. This is why the tool exists.
_Avoid_: clarification, requirements chat, Q&A

**im-align Skill**:
The entry Skill triggered by the developer in the terminal. The host Agent loads it, starts the Bridge, and hands the result and resume commands back to the developer after the Bridge exits. It does not perform the alignment itself.
_Avoid_: plugin, bot service

**Alignment Skill**:
The question-driven Skill executed by the Agent Backend. The default is `grill-with-docs`; callers may explicitly select another Skill. One Session uses exactly one Alignment Skill.
_Avoid_: command, plugin, workflow

**Bridge**:
The independent long-lived process started by the im-align Skill. One side receives and sends Thread messages through an IM Provider long connection; the other side drives the Agent Backend through ACP. The process exits after the Session ends.
_Avoid_: bot, server, daemon service

**Session**:
One complete Alignment run. It binds the git worktree and Spec Root resolved at launch time, one Alignment Skill, one Thread, and one Agent Backend session, all carried by one Bridge process.
_Avoid_: conversation, chat, task

**Thread**:
The Feishu/Lark message reply thread. One Thread has at most one active Session and is the IM-side container for that Session.
_Avoid_: topic, message chain, group

**Turn**:
One complete Agent Backend cycle: receive input, then produce output. The end of a Turn is the only point where the Bridge sends an aggregated reply to the Thread.
_Avoid_: round, response

**Agent Backend**:
The Coding Agent driven by the Bridge through ACP. Current backends are opencode, trae-cli, kiro-cli, and kimi. A Session is bound to one backend at creation time and cannot switch during its lifecycle.
_Avoid_: Agent, model, Provider

**Spec**:
One or more files written by the Agent Backend under the Spec Root after Alignment completes. They are the only terminal artifacts of a Session. They remain local files only; im-align does not create branches or commits. Filenames, subdirectories, formats, and file count are decided by the Agent Backend and the Alignment Skill.
_Avoid_: requirements doc, design doc, PRD

**Spec Root**:
The repository-relative directory where a Session may write Spec files. Writes outside it are not allowed during Alignment.
_Avoid_: output directory, writable workspace

**Permission Policy**:
The deterministic Bridge rule applied when an Agent Backend requests a restricted operation. Read-only work is allowed, edits are allowed only when every resolved target is inside the Spec Root, and all other restricted operations are rejected.
_Avoid_: Approval, authorization, confirmation

## Verification

**Unit Test**:
The deterministic base layer: stdlib unittest with faked boundaries and no network. Guards config, watchdog, and ACP client invariants.
_Avoid_: component test

**IM Integration**:
The Bridge-to-IM layer: a real Feishu Thread against a scripted multi-Turn fake Agent Backend over real ACP stdio. A fixed user-originated Participant reply must be acknowledged and drive the next Turn; it also asserts IM operations and on-disk artifacts. Trusted-main gate, run serially; pull requests run only the hermetic Unit Test layer.
_Avoid_: contract test, IM mock test

**Backend Smoke**:
The single-scenario layer: one real Agent Backend plus an LLM-driven User Simulator in one Participant Mode on a real Thread. Asserts the core Alignment workflow end to end with deterministic expectations.
_Avoid_: sanity check, canary

**Full e2e**:
The exhaustive layer: every supported Agent Backend in the supported `as-user` Participant Mode across all Scenarios, greenfield and brownfield, with the same schema as Backend Smoke. The blocked `bot` mode remains an optional platform diagnostic. Run on demand, not in CI.
_Avoid_: regression suite, UAT

**Scenario**:
One declarative test case: a directory declaring the Agent Backend, seed repository, requirement prompt, expected artifacts, and rubric dimensions. Shared by IM Integration, Backend Smoke, and Full e2e.
_Avoid_: test case file, fixture

**User Simulator**:
The harness component that plays the customer in Backend Smoke and Full e2e: it reads the Thread, answers the Agent's questions within the Scenario's brief, and never introduces requirements of its own.
_Avoid_: fake user, mock user, judge

**Participant Mode**:
The identity used by the User Simulator for one Backend Smoke or Full e2e run. `as-user` is the supported mode. `bot` remains selectable only to re-probe the platform delivery constraint recorded by ADR-0009; it is expected to fail acknowledgement and Turn-driving assertions until re-enabled.
_Avoid_: simulator identity, auth mode
