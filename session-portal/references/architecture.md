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
| `portal_core.py` | Storage, the message lifecycle state machine, the authenticated-principal + operator-grant model, and every validation / authorization / privacy check. Pure and importable. |
| `portal_state.py` | Read-only classifier that reports a session's state from its own runtime-owned log (plus host liveness evidence). Never writes a transcript, never takes a caller-asserted state. |
| `portal_adapters.py` | **Advisory only.** Classifies whether a recipient looks safe to notify, and describes (never performs) an operator resume. No delivery side effects — delivery is pull-only in `portal_core`. |
| `portal_mcp.py` | Local stdio MCP (JSON-RPC 2.0) server. Binds nothing to the network. Resolves a bearer token to a principal and derives all identity from it. |
| `portal_admin.py` | Operator CLI: mint principal tokens, issue capability grants, health, register, operator-privileged send/inbox/ack, stale-lock recovery, uninstall. |

## Identity and authorization

- **Principals (authentication).** The operator mints a bearer token for a session
  (`issue-principal`); only its salted hash is stored. Each MCP server is launched by one
  session with that token in `SESSION_PORTAL_TOKEN`. The server resolves it to a principal
  (`product:runtime_session_id`) and derives the message source, the inbox owner, and the
  acknowledger from it — never from a tool argument. Tokens expire and are revocable.
- **Grants (authorization).** Elevated actions are operator-issued capability grants
  (`send-steer`, `accept-steering`, `speak-as-user`), each scoped to a counterparty (or `*`),
  expiring, and revocable. They replace the old caller-supplied `authorized` /
  `accepts_steering` booleans, so a caller can no longer flip a security gate.

## Storage

A single SQLite database in a user-level directory (`$SESSION_PORTAL_HOME`, default
`~/.session-portal/portal.db`; override the file with `$SESSION_PORTAL_DB`). It runs in
**WAL** mode with a busy timeout, so multiple independent Claude and Codex sessions can
read and write at once without corruption. Each composite operation (a message insert plus
its audit event; a status change plus its lifecycle event) runs inside an explicit
`BEGIN IMMEDIATE` transaction, so a crash can't leave a row and its audit event split. There
is **no daemon** — every operation opens the DB, does its work, and closes.

Tables: `sessions`, `principals` (authenticated identities, hashed tokens), `auth_grants`
(operator capabilities), `messages`, `message_events` (the audit trail), `leases` (per-
destination single-flight), and `schema_version` (migrations; current schema is **v2**, which
adds `principals`/`auth_grants` and migrates a v1 DB in place).

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
reported state. A session announces itself (`portal_register_session`) under its
authenticated id of the form `product:runtime_session_id` (e.g. `claude:1a2b`, `codex:9f8e`).
A message may be queued for a session that has not registered yet; a placeholder row holds it
until the recipient checks in.

## Delivery: pull-only

Delivery happens on exactly one event: the **authenticated recipient pulls its own inbox**.
The recipient calls `portal_list_inbox` with `deliver=true`; because it is the bound
principal draining its own inbox, the pull *is* the safe turn boundary and *is* the proof of
receipt, so queued messages transition to delivered and are stamped `delivered_to` = the
recipient. There is no caller-supplied boundary string; the server annotates the pull with
runtime-derived state read from the recipient's own log.

There is deliberately **no** sender-side push, auto-resume, or Codex-native delivery in this
MVP: each would have to claim the recipient received a message without the recipient ever
confirming it — exactly the false-receipt failure this design avoids. `portal_adapters.py`
therefore only *advises*:

- `classify_deliverability` — read-only: is the recipient provably between turns? Advice for
  deciding whether to notify a human; it never delivers.
- `resume_plan` — describes an operator `claude --resume` command for a *proven-closed*
  session; it never spawns anything and never marks a message delivered or resumed.
- `codex_native_status` — always reports native task messaging as not an implemented delivery
  channel; delivery to Codex is the durable queue drained by an authenticated Codex-side pull.

## Idempotency, leases, recovery

- A send carrying an `idempotency_key` is safe to retry: a repeat returns the existing
  message unchanged (no duplicate, no second `created` event). The `message_id` is derived
  deterministically from the key.
- Delivery to one destination is serialized by a **lease** row keyed on the destination.
  Acquisition is a single-statement `INSERT` guarded by the primary-key UNIQUE constraint:
  SQLite serializes writers, so at most one contender's INSERT succeeds — that single
  statement *is* the compare-and-set (no wrapping transaction is needed or used). A loser
  gets `IntegrityError` and only "wins" if it already holds the row (re-entrant same holder).
  Expired leases are reclaimed (deleted) before each attempt, so a crashed holder never blocks
  forever; `release_lease` is holder-scoped, so a late release from a crashed holder cannot
  delete the lease a new holder has since taken. A `delivered` message persists durably until
  it is acknowledged.

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
