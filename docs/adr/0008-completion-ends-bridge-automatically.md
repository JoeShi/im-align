# Completion Ends the Bridge Automatically

Status: accepted.

When the Agent Backend emits a valid completion signal, the Alignment has already produced its only terminal artifact: a Spec file validated to be inside the Spec Root and written by this attempt. At that point the Bridge could either exit on its own or keep running until idle timeout or a local `bridge.py stop`. This decision records the shipped behavior and its rationale, because a participant seeing the bot go offline right after the green completion card may expect it to stay available for follow-up questions.

Decision: a valid completion signal is a terminal event for the whole Bridge process. The Orchestrator marks the run `done`, the worker finalizes active.json, appends history, closes the ACP client and the IM long connection, and exits immediately, releasing the machine-level single-session lock. There is no grace window for late Thread messages, `done` is not resumable, and an invalid completion signal never terminates anything: the Session continues and asks the Agent to write this attempt's Spec before declaring completion again.

## Considered Options

- Exit immediately on valid completion (chosen): the Session's purpose is fulfilled, the machine-level lock frees the machine for the next Session, the host Agent's `wait`/handoff gets its terminal state, and the WebSocket plus ACP processes stop instead of idling for hours.
- Linger until idle timeout or manual stop: rejected because it holds the single-session lock and blocks the next Session while doing no work, and the run would be recorded as idle_timeout instead of done.
- Grace window for follow-up questions: rejected because Thread text has no lifecycle authority (ADR-0007); if it cannot stop a Session, it must not extend one either. Follow-ups are new requirements and belong to a new Session.
- Terminate after repeated invalid signals: rejected because an invalid marker is the Agent's output error, not a participant's, and the Spec may already be valid on disk; terminating would strand a recoverable Session.
- Make `done` resumable: rejected because `done` is terminal by definition; post-completion questions are a new brownfield Session, and resume stays limited to failed/idle_timeout/stopped.

## Consequences

- The run reaches terminal state `done` without developer action; the e2e harness asserts this as `status_done`.
- Late replies in the Thread receive the "Cannot Reply" card. The green completion card is best-effort: its failure is logged but cannot undo `done`, because terminal state is determined by file validation, not IM delivery.
- The Bridge worker exits with code 0 on `done`, and a new Session may start immediately after.
- ADR-0007 already listed "let Spec completion close it" among the ways a Session ends; this ADR gives that behavior its own record without changing ADR-0007's rule that Thread text has no lifecycle authority.
