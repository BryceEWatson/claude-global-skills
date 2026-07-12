# Session portal — threat model

## In plain terms

The portal moves short notes between two AI assistants. The danger is not the notes
themselves but what an assistant might be tricked into *doing* with one, or what private
data might leak through one. So the portal treats every note as untrusted text, refuses to
carry anything secret or sensitive, refuses to act on a note itself, and refuses to
interrupt an assistant that is still working. This page lists what it trusts, what it
blocks, and what it deliberately cannot do.

## Trust boundaries

- **Identity is authenticated, not asserted.** A caller proves who it is with a bearer token
  that resolves to a principal (`product:runtime_session_id`); only the token's salted hash
  is stored. The message source, the inbox owner, and the acknowledger are all derived from
  that principal server-side — never from a tool argument. A session cannot send as another,
  read another's inbox, or acknowledge another's message. Tokens expire and are revocable.
- **Authorization is an operator grant, not a caller boolean.** Sending steering, accepting
  steering, and recording `authorship=user` are operator-issued capabilities (scoped to a
  counterparty, expiring, revocable). There is no caller-supplied `authorized` flag, so a
  compromised or over-eager caller cannot flip a security gate open.
- **Message content is untrusted data.** The portal never executes it, never interprets it
  as a command, and never uses it to approve a permission, publish, merge, push, or delete
  anything. Delivery returns the text and its author label; the receiving assistant acts
  under its own normal gates.
- **Authorship is a vouched claim, distinct from the authenticated sender.** A message
  records both the proven `source_session_id` and an `authorship` label (`user` vs `agent`).
  `agent` is the default; `user` is only recordable with an operator `speak-as-user` grant,
  so the label is something the operator vouched for, not a free assertion.
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

- **Delivery is pull-only.** A message becomes `delivered` only when its authenticated
  recipient pulls its own inbox. That pull is the only proof of receipt, so the portal never
  fabricates a `delivered`, `resumed`, or native-delivered receipt it cannot back with a real
  pull. There is no sender-side push and no caller-supplied delivery boundary.
- **Boundary and liveness come from runtime-owned evidence.** The state classifier reads a
  session's own append-only log (read-only) plus optional host liveness evidence written by a
  runtime hook — never a caller-asserted `boundary` or `process_alive`. A quiet transcript
  with no end-of-turn marker is `unknown` (not deliverable via any advisory push).
- **Another session is never interrupted or resumed.** The portal has no code path that
  pushes into, or resumes, another session. `resume_plan` only *describes* an operator
  `claude --resume` command for a proven-closed session; running it is a human decision, and
  the message is still delivered only when the resumed session pulls it.
- **Loops are prevented.** A session can't message itself; forwarding is depth-capped; and
  an agent-authored message can't be forwarded straight back to its origin (ping-pong).

## Explicit non-capabilities

The portal deliberately does **not**:

- write to or tail-inject any transcript JSONL, or open a second writer on one;
- push into, resume, steer, or inject into **any** session — delivery is pull-only;
- claim a delivery/resume/native receipt it cannot back with a real recipient pull;
- let a caller assert its own identity or set an authorization flag;
- use Codex `turn/steer`, raw history injection, or a separately launched app-server as an
  idleness oracle;
- approve permissions, publish, merge, push, or delete on another session's behalf;
- carry secrets or hidden model internals;
- open a network port, run a permanent daemon, or read arbitrary files;
- replace `monitor-agent-thread`, which stays read-only. Monitoring and steering are
  separate capabilities by design.
