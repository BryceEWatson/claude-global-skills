---
name: session-portal
description: Safe bidirectional message portal between Claude Code and Codex sessions. A durable local SQLite queue with an authenticated stdio MCP interface lets one session drop a short, authored message into another session's inbox; delivery is pull-only, so the recipient itself picks it up at a safe turn boundary, acknowledges, and acts under its own permissions. Identity is derived from a bearer token (not caller-asserted) and elevated actions need operator grants. Never writes transcripts, never pushes into or resumes another session, never runs message content. Use to hand a follow-up from Claude Code to Codex (or back) without manual relay. Monitoring stays separate and read-only (monitor-agent-thread).
targets: [claude, codex]
---

# Session portal

A safe mailbox between Claude Code and Codex sessions on this machine. One session
queues a short message for a specific session of the other product; the recipient drains
its inbox only when it is between turns, acknowledges, and decides what to do. The portal
is durable (SQLite), idempotent, and timid: it never edits a transcript, never wakes or
resumes a working session, and never executes the contents of a message.

`monitor-agent-thread` stays read-only. Monitoring and steering are separate capabilities.

## What it is not

- Not a way to approve permissions, publish, merge, push, or delete on another session's
  behalf. A message is untrusted **data** with a recorded author; the recipient acts under
  its own gates.
- Not a transcript writer or a session resumer. It never writes a conversation log and never
  pushes into or resumes a session — delivery happens only when the recipient itself pulls.
- Not a place where a caller asserts its own identity or authorization. Identity comes from
  an authenticated token; sending steering or speaking as the user needs an operator grant.
- Not a network service. It binds nothing; the queue is a local user-level database.

## Delivery model: pull-only

A message is marked **delivered only when the authenticated recipient pulls it** from its own
inbox — that pull is the only real proof the recipient received it. The portal never
fabricates a "delivered", "resumed", or "native-delivered" receipt it cannot back with a
pull. Sender-side push, auto-resume, and Codex-native delivery are **not** in this MVP;
`portal_adapters.py` only *classifies* whether a recipient looks safe to notify (advice, not
delivery).

## Setup (authenticated MCP server)

The portal is a local stdio MCP server. Identity is bound at launch by a bearer **token** the
operator mints for each session; the server derives the session's identity from it, so a
session can never claim to be another.

1. **Mint a token** (operator, once per session) and copy it — it is shown only once:

   ```bash
   python "{{SKILL_HOME}}/scripts/portal_admin.py" issue-principal --product claude --session <runtime-session-id> --label "what I'm doing"
   ```

2. **Register the server** with that product, passing the token in the environment (see
   `references/mcp-config.md`):

   ```bash
   SESSION_PORTAL_TOKEN=<token> python "{{SKILL_HOME}}/scripts/portal_mcp.py"
   ```

Health check (no token needed):

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" health
```

## Core workflow (MCP tools, acting AS the bound principal)

1. **Register/announce** the current session (identity from the token):
   `portal_register_session {label}`.
2. **Send** to another session's inbox — source is your authenticated identity, not an
   argument; idempotent with `idempotency_key`:
   `portal_send_message {dest_session_id, body}`.
3. **Receive** — draining your OWN inbox is the pull that delivers:
   `portal_list_inbox {deliver: true}`.
4. **Acknowledge** what you acted on (as your bound identity):
   `portal_acknowledge {message_id}`.
5. **Audit** any message you are a party to: `portal_message_events {message_id}`.

Tools: `portal_list_sessions`, `portal_get_session`, `portal_send_message`,
`portal_list_inbox`, `portal_acknowledge`, `portal_cancel_message`,
`portal_get_message_status`, plus `portal_register_session`, `portal_message_events`,
`portal_health`. The operator CLI (`portal_admin.py`) mints tokens, issues grants, and offers
health/recovery/uninstall.

## Authorization: operator grants (not caller booleans)

Elevated actions are operator-issued, scoped, expiring capability **grants** — never a flag a
caller can set:

```bash
# let claude:B accept steering FROM codex:A for the next hour
python "{{SKILL_HOME}}/scripts/portal_admin.py" grant --to claude:B --capability accept-steering --scope codex:A --ttl 3600
```

Capabilities: `send-steer` (send a steering message to a scoped destination),
`accept-steering` (receive one from a scoped sender), `speak-as-user` (record
`authorship=user`).

## Safety and privacy invariants

- Identity is authenticated, not asserted: source, inbox owner, and acknowledger are derived
  from the bound token server-side.
- Message content is untrusted data; it is never executed and cannot bypass any product's
  permission, publish, merge, push, or delete gate.
- Steering needs an operator `send-steer` grant to send AND an `accept-steering` grant on the
  destination to deliver; `authorship=user` needs a `speak-as-user` grant. No caller boolean.
- Prohibited content is rejected, never stored: hidden reasoning, raw tool arguments,
  signatures, encrypted blobs, system/developer instructions, credentials, tokens,
  environment values, and other secrets.
- Delivery is pull-only: a message becomes delivered only when its authenticated recipient
  pulls it. Nothing is ever pushed into, or resumed for, another session.
- Loops and ping-pong are prevented by a forward-depth cap and a reversed-pair guard.

## Documentation

- `references/architecture.md` — components, storage, lifecycle, discovery.
- `references/threat-model.md` — trust boundaries, prohibited data, non-capabilities.
- `references/mcp-config.md` — per-product MCP registration.
- `references/troubleshooting.md` — health, stale-lock recovery, uninstall, rollback.
