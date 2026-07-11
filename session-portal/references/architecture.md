# Session portal — architecture

## In plain terms

The portal is a small local **post office** shared by two AI assistants (Claude Code and
Codex). An assistant writes a short note addressed to a specific session of the other one
and drops it in a durable outbox. The note waits there until the recipient next pauses
between turns, at which point the recipient picks it up, marks it read, and decides what
to do. Nothing is pushed into an assistant that is still working, no conversation log is
ever edited, and the note is never run as a command. If the post office can't prove the
recipient is safely paused, the note simply waits.

## Components

| Piece | Role |
|---|---|
| `portal_core.py` | Storage, the message lifecycle state machine, and every validation / authorization / privacy check. Pure and importable. |
| `portal_state.py` | Read-only classifier that decides if a session is safe to deliver to. Never writes a transcript. |
| `portal_adapters.py` | Boundary-safe delivery: push (only to a paused session), the guarded Claude resume, the Codex-native fallback. |
| `portal_mcp.py` | Local stdio MCP (JSON-RPC 2.0) server exposing the tools. Binds nothing to the network. |
| `portal_admin.py` | Operator CLI: health, register, send/inbox/ack, stale-lock recovery, uninstall. |

## Storage

A single SQLite database in a user-level directory (`$SESSION_PORTAL_HOME`, default
`~/.session-portal/portal.db`; override the file with `$SESSION_PORTAL_DB`). It runs in
**WAL** mode with foreign keys on and a busy timeout, so multiple independent Claude and
Codex sessions can read and write at once without corruption, and a crashed process can't
leave a half-written message. There is **no daemon** — every operation opens the DB, does
its work in a transaction, and closes.

Tables: `sessions`, `messages`, `message_events` (the audit trail), `leases` (per-
destination single-flight), and `schema_version` (migrations).

## Message lifecycle

```
              cancel                         expire / fail
   queued ─────────────► cancelled     ┌────────────────────┐
     │                                 ▼                    ▼
     │  deliver (safe boundary)     expired               failed
     ▼
  delivered ──── acknowledge ────► acknowledged
```

Every transition appends a row to `message_events`, so any message has a complete,
inspectable history. Illegal transitions (for example acknowledging a message that was
never delivered) are rejected.

## Discovery

`portal_list_sessions` / `portal_get_session` return registered sessions and their last
reported state. A session registers itself (`portal_register_session`) with a stable id
of the form `product:runtime_session_id` (e.g. `claude:1a2b`, `codex:9f8e`). A message may
be queued for a session that has not registered yet; a placeholder row holds it until the
recipient checks in.

## Delivery paths

- **Pull** (primary, safe by construction): the recipient calls `portal_list_inbox` with
  `deliver=true` at one of its own safe boundaries — session-start, prompt-submit, stop,
  an explicit command, or an authorized heartbeat. Being at that boundary *is* the proof
  of safety, so queued messages transition to delivered.
- **Push** (sender-side, guarded): an adapter may deliver to a recipient only if the
  read-only classifier reports it `idle` or `waiting-for-user`. An `active` session — or
  any state where idleness isn't proven — is refused and the message stays queued.
- **Claude resume** (last resort, closed sessions only): used only after proof the target
  session is closed and a destination lease is held; it never resumes an active session
  and defaults to a dry-run that returns the command plan rather than launching it.
- **Codex native**: used only when a real Codex task tool is surfaced to a running,
  authorized task; otherwise delivery falls back to the durable queue.

## Idempotency, leases, recovery

- A send carrying an `idempotency_key` is safe to retry: a repeat returns the existing
  message unchanged (no duplicate, no second `created` event). The `message_id` is derived
  deterministically from the key.
- Delivery to one destination is serialized by a lease row; a second deliverer is refused
  until the lease is released or its TTL lapses. After a crash, an orphaned lease is
  reclaimed once its TTL passes, and any message left `queued` is simply delivered on the
  next safe boundary. A `delivered` message persists durably until it is acknowledged.

## Implementation detail

- Session-log locations mirror `monitor-agent-thread`'s surfaces: Claude
  `~/.claude/projects/**/<session-id>.jsonl`, Codex
  `~/.codex/sessions/**/rollout-*-<thread-id>.jsonl`. Both roots are env-overridable
  (`SESSION_PORTAL_CLAUDE_LOGS`, `SESSION_PORTAL_CODEX_LOGS`) for hermetic tests.
- The classifier reads only the tail of a log to judge state; it opens the file read-only
  and never creates a second writer.
- The MCP layer whitelists each tool's arguments (`additionalProperties: false`) and calls
  `portal_core.validate_no_prohibited_fields`, so a prohibited key can't ride in even if a
  caller bypasses the schema.
