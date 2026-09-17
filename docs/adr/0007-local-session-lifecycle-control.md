# Keep Session Lifecycle Control Local

Status: accepted.

Starting a Session previously required binding it to a Session Initiator. The Bridge tried repository config, same-app `lark-cli`, git email, and an interactive prompt, then resolved email through the Feishu/Lark contact API. In real use, valid account email addresses were not always visible to the app contact API, so identity lookup became a startup single point of failure. The identity existed only to authorize `stop` text in the Thread; Permission Policy decisions are deterministic and no longer depend on a person.

Decision: remove Session Initiator identity binding. `setup` does not ask for an email, `start` does not accept `--initiator`, runtime state does not add initiator fields, and the Bridge does not resolve email to open_id. Any delivered participant message may drive Alignment. Thread text has no lifecycle authority: `stop` is ordinary Alignment input, while Session stopping remains available through the local `bridge.py stop` command. Legacy repository and state initiator fields are ignored.

## Considered Options

- Keep the contact lookup and document tenant directory requirements: rejected because a valid user can still be invisible to the app, preventing every happy-path Backend from starting.
- Predeclare an identity or lease: rejected because it adds configuration and expiry state for a single low-value Thread command.
- Let any Thread participant stop the Session: rejected because group membership would become remote lifecycle authority and accidental `stop` text would terminate work.
- Keep lifecycle control local (chosen): startup has no directory dependency and the existing local process owner can still stop or resume a Session.

## Consequences

- The app no longer needs `contact:user.id:readonly`; `contact:contact.base:readonly` remains for best-effort participant display names.
- Repository configuration no longer documents `initiator`. The loader silently discards the legacy field so existing repositories do not need a migration before startup.
- CI no longer needs `IM_ALIGN_E2E_INITIATOR_OPEN_ID`.
- A participant cannot end a Session from Feishu/Lark. The developer must run `bridge.py stop`, allow idle timeout, or let Spec completion close it.
- Historical run records may still contain initiator fields, but current code does not read or expose them.
- This ADR supersedes the identity-chain and Thread-stop portions of ADR-0004 and updates the stop consequence in ADR-0006.
