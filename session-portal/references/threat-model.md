# Session portal — threat model

## In plain terms

The portal moves short notes between two AI assistants. The danger is not the notes
themselves but what an assistant might be tricked into *doing* with one, or what private
data might leak through one. So the portal treats every note as untrusted text, refuses to
carry anything secret or sensitive, refuses to act on a note itself, and refuses to
interrupt an assistant that is still working. This page lists what it trusts, what it
blocks, and what it deliberately cannot do.

## Trust boundaries

- **Message content is untrusted data.** The portal never executes it, never interprets it
  as a command, and never uses it to approve a permission, publish, merge, push, or delete
  anything. Delivery returns the text and its author label; the receiving assistant acts
  under its own normal gates.
- **Authorship is recorded.** Every message is marked `user` (a person wrote it) or `agent`
  (an assistant is suggesting it). A recipient can weigh an agent suggestion differently
  from a human instruction.
- **The two products keep their own gates.** The portal coordinates; it does not bypass any
  product's permission, publication, or safety controls.

## Rejected content (never stored, never emitted)

The send path rejects a message that carries, or whose body contains, any of:

- hidden reasoning / chain-of-thought,
- raw tool arguments,
- signatures, encrypted blobs / ciphertext,
- system or developer instructions,
- credentials, passwords, tokens, API keys, access tokens,
- environment values,
- other secret-shaped strings (provider key patterns, `Bearer …`, `api_key = …`, etc.).

Prohibited *fields* are blocked by a whitelist (`additionalProperties: false`) plus an
explicit key check; secret-shaped *body* content is blocked by a pattern scan. The chosen
policy is **reject** (a clear, testable line) rather than silent redaction.

## Input validation

- Identifiers must match `^[A-Za-z0-9._:-]{1,128}$`; a session id must carry a valid
  `product:` prefix.
- A body must be non-empty, valid UTF-8, ≤ 4 KiB, and free of control characters other
  than tab / newline / carriage-return.
- No arbitrary filesystem reads are exposed. The MCP server binds nothing to the network.

## Delivery safety

- **A quiet transcript does not prove idleness.** A log that merely stopped, with no
  explicit end-of-turn marker, is classified `unknown`, and `unknown` is not deliverable —
  the message waits.
- **An active session is never interrupted.** Push delivery is refused unless the recipient
  is provably `idle` or `waiting-for-user`.
- **Resuming is a last resort.** `claude --resume` is used only when the target is proven
  closed and a destination lease is held, and it never resumes an active interactive
  session. It defaults to a dry-run.
- **Loops are prevented.** A session can't message itself; forwarding is depth-capped; and
  an agent-authored message can't be forwarded straight back to its origin (ping-pong).

## Explicit non-capabilities

The portal deliberately does **not**:

- write to or tail-inject any transcript JSONL, or open a second writer on one;
- resume, steer, or inject into an **active** session;
- use Codex `turn/steer`, raw history injection, or a separately launched app-server as an
  idleness oracle;
- approve permissions, publish, merge, push, or delete on another session's behalf;
- carry secrets or hidden model internals;
- open a network port, run a permanent daemon, or read arbitrary files;
- replace `monitor-agent-thread`, which stays read-only. Monitoring and steering are
  separate capabilities by design.
