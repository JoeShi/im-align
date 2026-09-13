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
One complete Alignment run. It binds the git worktree active at launch time, one Alignment Skill, one Thread, and one Agent Backend session, all carried by one Bridge process.
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
The document written by the Agent Backend to the current repository after Alignment completes. It is the only terminal artifact of a Session. It remains a local file only; im-align does not create branches or commits. Path and format are decided by the Agent Backend and the Alignment Skill.
_Avoid_: requirements doc, design doc, PRD

**Approval**:
An allow or reject decision made by the Session Initiator through an interactive card when the Agent Backend requests a restricted operation.
_Avoid_: authorization, confirmation, permission

**Session Initiator**:
The user who starts the Session. Their Feishu/Lark identity is resolved at launch time. Only this user can approve write operations or stop the Session.
_Avoid_: asker, owner, admin
