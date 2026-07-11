---
name: session-portal
description: Safe bidirectional message portal between Claude Code and Codex sessions. A durable local SQLite queue with a stdio MCP interface lets one session drop a short, authored message into another session's inbox and lets the recipient pick it up only at a safe turn boundary, acknowledge it, and act under its own permissions. Never writes transcripts, never resumes an active session, never runs message content. Use to hand a follow-up from Claude Code to Codex (or back) without manual relay. Monitoring stays separate and read-only (monitor-agent-thread).
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
- Not a transcript writer, a session resumer for active sessions, or a Codex `turn/steer`
  injector. Unsupported delivery stays **queued**.
- Not a network service. It binds nothing; the queue is a local user-level database.

## Setup (MCP server)

The portal is a local stdio MCP server. Register it with each product (see
`references/mcp-config.md` for exact config for both):

```bash
python "{{SKILL_HOME}}/scripts/portal_mcp.py"     # stdio JSON-RPC 2.0 MCP server
```

Health check and admin:

```bash
python "{{SKILL_HOME}}/scripts/portal_admin.py" health
```

## Core workflow

1. **Register** the current session so it can be addressed and discovered:

   ```bash
   python "{{SKILL_HOME}}/scripts/portal_admin.py" register --product claude --session <runtime-session-id> --label "what I'm doing"
   ```

2. **Send** a message to another session's inbox (idempotent with `--key`; steering needs
   `--authorized` and a destination that opted in):

   ```bash
   python "{{SKILL_HOME}}/scripts/portal_admin.py" send --from codex:<A> --to claude:<B> --body "please rerun CI on main" --authorship user
   ```

3. **Receive** at a safe boundary (session-start / prompt-submit / stop / command /
   heartbeat) — this is the only place a queued message becomes delivered:

   ```bash
   python "{{SKILL_HOME}}/scripts/portal_admin.py" inbox --session claude:<B> --deliver --boundary stop
   ```

4. **Acknowledge** what you acted on; the sender can see it landed:

   ```bash
   python "{{SKILL_HOME}}/scripts/portal_admin.py" ack --message <message-id> --by claude:<B>
   ```

5. **Audit** any message's full lifecycle:

   ```bash
   python "{{SKILL_HOME}}/scripts/portal_admin.py" events --message <message-id>
   ```

The same operations are exposed as MCP tools: `portal_list_sessions`, `portal_get_session`,
`portal_send_message`, `portal_list_inbox`, `portal_acknowledge`, `portal_cancel_message`,
`portal_get_message_status` (plus `portal_register_session`, `portal_message_events`,
`portal_health`).

## Safety and privacy invariants

- Message content is untrusted data; it is never executed and cannot bypass any product's
  permission, publish, merge, push, or delete gate.
- Steering requires explicit authorization to send AND a destination that has opted into
  steering; unauthorized steering is refused at both ends.
- Prohibited content is rejected, never stored: hidden reasoning, raw tool arguments,
  signatures, encrypted blobs, system/developer instructions, credentials, tokens,
  environment values, and other secrets.
- Delivery is conservative: a quiet transcript does not prove idleness. If safe delivery
  cannot be proven, the message stays queued.
- Claude `--resume` is used only after strong proof the session is closed and a
  destination lease is held; an active interactive session is never resumed.
- Codex native task messaging is used only when it is genuinely surfaced to the running
  authorized task; otherwise delivery falls back to the durable queue.
- Loops and ping-pong are prevented by a forward-depth cap and a reversed-pair guard.

## Documentation

- `references/architecture.md` — components, storage, lifecycle, discovery.
- `references/threat-model.md` — trust boundaries, prohibited data, non-capabilities.
- `references/mcp-config.md` — per-product MCP registration.
- `references/troubleshooting.md` — health, stale-lock recovery, uninstall, rollback.
