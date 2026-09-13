# Scope Is Strictly Limited To Pre-Coding

Status: accepted. Delivery and handoff behavior were revised by ADR-0004.

im-align's responsibility boundary is Alignment -> local Spec write -> hand results and resume instructions back to the developer. It stops there. Automated coding is downstream work and is not triggered or orchestrated by im-align.

## Consequences

- The Bridge ends the Session immediately after it detects the strict completion signal and confirms the Spec was written by this Session.
- It does not create branches, commit, push, or create/manage PRs automatically.
- The Spec is the primary source of truth for the coding stage. im-align may also provide the native resume command for the same Agent Backend session, but it does not run that command.
- Explicitly out of scope: code execution, coding progress tracking, PR management, and deployment. If needed, those capabilities belong in another Skill or system.
