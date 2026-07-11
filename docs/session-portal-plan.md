# Session portal — design & implementation plan

## In plain terms

This adds a safe **mailbox** between two AI coding assistants that run on this
machine: Claude Code and OpenAI's Codex. Today, if you want a follow-up from one to
reach the other, you copy it across by hand. The portal lets each assistant *drop a
short message into a durable local queue* addressed to a specific session of the
other product, and lets the recipient *pick it up only at a safe pause* (when it is
between turns), acknowledge it, and act on it under its own normal permissions.

The portal is deliberately timid. It never edits either assistant's conversation log,
never wakes up or takes over a session that is still working, and never runs the
contents of a message. A message is just text with a label saying who wrote it (a
person, or an assistant making a suggestion). The queue lives in a small local
database so nothing is lost if a process crashes, the same message sent twice only
lands once, and messages to the same recipient are handed over one at a time. If a
safe hand-off can't be *proven*, the message simply waits in the queue.

Source of truth for this work: **GitHub issue #16** ("Build a safe bidirectional
Claude Code and Codex session portal"), stacked on the corrected PR #15 cross-runtime
sync engine.

## Scope

### In scope
- A new dual-target skill `session-portal` (`targets: [claude, codex]`) deployed by the
  PR #15 sync engine (shared content once; Codex-only files under `overlays/codex/`).
- Durable storage in user-level SQLite (WAL) with migrations, idempotency, per-destination
  serialized delivery, and crash-safe recovery. No permanent daemon.
- A local **stdio MCP** server exposing (repo-named, semantics per issue):
  `list_sessions`, `get_session`, `send_message`, `list_inbox`, `acknowledge`,
  `cancel_message`, `get_message_status` (plus `register_session`, `health`).
- Full message lifecycle: `queued → delivered → acknowledged`, plus `cancelled`,
  `expired`, `failed`; every transition audited.
- Conservative session-state reporting: `active`, `idle`, `waiting-for-user`,
  `completed`, `unavailable`, `stale`, `unknown` — derived from read-only evidence
  (log mtime/age, explicit completion markers, process/lease/file evidence). A quiet
  transcript never alone proves idleness.
- Authorization + privacy gates: explicit authorization to send/deliver steering;
  authorship (user vs agent-suggestion) recorded; strict identifier/encoding/size
  validation; rejection of prohibited content (hidden reasoning, raw tool args,
  signatures, encrypted blobs, system/developer instructions, credentials/tokens/env);
  secret rejection; loop/ping-pong prevention; local-only, no network bind.
- Boundary delivery: pull-based pickup at supported boundaries; active-session delivery
  refusal; a **guarded** `claude --resume` adapter (closed-session proof + lease) that
  never resumes an active session; a Codex-native fallback that leaves messages queued
  when native task messaging is not surfaced.
- Hermetic tests for every issue #16 category, wired into `.github/workflows/ci.yml`.
- Documentation (architecture, threat model, install, MCP config, lifecycle,
  authorization, boundary delivery, adapter limits, health, troubleshooting, uninstall,
  rollback, stale-lock recovery, explicit non-capabilities).
- A controlled bidirectional forward test using disposable sessions only.

### Out of scope / Deferred (would be a scope change — flag before building)
- Any write to a transcript JSONL, or a concurrent transcript writer.
- Resuming or steering an **active** session by any means.
- Codex `turn/steer`, raw history injection, or a separately launched app-server used as
  an idleness oracle.
- Network transport, multi-machine sync, or a long-running daemon.
- Changing `monitor-agent-thread`; it stays read-only. Monitoring and steering remain
  separate skills.

## Definition of done (testable)
- PR #15's two P2 findings resolved with regression tests, ordinary commits, no history
  rewrite. **(done: commit 970862b, 40 sync tests green, threads resolved.)**
- All automated checks pass locally and in CI (Node + Python matrix, py_compile).
- Portal deployed to **both** `~/.claude/skills` and `~/.codex/skills`; installed copies
  verified (Claude has no `agents/`/`overlays/`; Codex has `agents/openai.yaml`), not just
  repo fixtures.
- Controlled bidirectional forward test passes with audit evidence; neither transcript
  written; prohibited data neither stored nor emitted; test sessions/messages cleaned up.
- Docs match observed behavior.
- Full adversarial `/review-loop` clean (or documented cap); commit-pinned PR verdict.
- Separate **draft** PR opened, stacked on the corrected PR #15 branch; CI green. No merge
  without operator authorization.

## Implementation detail

### File layout
```
session-portal/
  SKILL.md                       # targets: [claude, codex]; {{SKILL_HOME}} tokens
  scripts/
    portal_core.py               # storage + lifecycle + validation (importable, pure)
    portal_state.py              # conservative read-only session-state classifier
    portal_mcp.py                # stdio JSON-RPC 2.0 MCP server wrapping core
    portal_admin.py              # health, register, stale-lock recovery, uninstall
  references/
    architecture.md
    threat-model.md
    mcp-config.md                # per-product config; {{SKILL_HOME}} expands the path
    troubleshooting.md
  overlays/
    codex/agents/openai.yaml     # Codex interface manifest (Codex-only)
  tests/
    test_portal_core.py
    test_portal_state.py
    test_portal_mcp.py
    test_portal_delivery.py
```

### Storage (`portal_core.py`)
- DB at `$SESSION_PORTAL_HOME/portal.db` (default `~/.session-portal/`; env-overridable
  for hermetic tests). `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout`.
- Tables: `schema_version`; `sessions(session_id PK, product, runtime_session_id, cwd,
  label, registered_at, last_seen_at, last_state, meta)`; `messages(message_id PK,
  idempotency_key UNIQUE, source_session_id, dest_session_id, dest_product, body,
  authorship, kind, authorized, status, created_at, updated_at, expires_at, delivered_at,
  acknowledged_at, attempts, last_error, forward_depth, root_message_id)`;
  `message_events(id PK, message_id, ts, event, detail)`; `leases(dest_session_id PK,
  holder, acquired_at, expires_at)`.
- Migrations keyed on `schema_version`; `init_db()` is idempotent and creates-or-migrates.
- Idempotency: `message_id = "msg_"+sha256(idempotency_key)[:24]`; `INSERT OR IGNORE` on
  the unique key; a duplicate send returns the existing id + status, no new row/event.
- Serialized delivery: `acquire_lease(dest)` inside a transaction; delivery to one
  destination is single-flight. Crash recovery: expired leases are reclaimable; sends are
  atomic (rolled back on crash); `delivered`-without-ack persists durably until ack/expiry.

### Lifecycle + audit
Transitions each append a `message_events` row: `created`, `delivered`,
`delivery_refused`, `acknowledged`, `cancelled`, `expired`, `failed`. Guards reject
illegal transitions (e.g. ack of a cancelled/expired message).

### Validation & authorization (`portal_core.validate_*`)
- Identifiers: `^[A-Za-z0-9._:-]{1,128}$`. Body ≤ 4 KiB, valid UTF-8, no control chars
  except `\n`/`\t`. Reject unknown/prohibited payload fields (reasoning, tool_args,
  signature, encrypted, system, developer, credentials, token, env, secret).
- Secret scan on body → reject (documented; redaction is the alternative, we choose
  reject for a clear invariant).
- Steering: `kind="steer"` requires `authorized=True` and recorded `authorship`;
  delivering steer also requires the destination session to have opted into steering.
  Unauthorized steer rejected at send and at deliver.
- Loop prevention: `source==dest` rejected; `forward_depth` capped (default 1); a message
  that re-forwards an agent-authored, already-forwarded message on the same reversed pair
  is rejected (ping-pong guard).
- The portal has **no** code path that approves permissions, publishes, merges, pushes, or
  deletes based on message content; delivery returns data only. Asserted by test.

### Session-state (`portal_state.py`, read-only)
Reuses the `monitor-agent-thread` evidence surfaces (log locations, mtime/age, explicit
turn-complete markers) but **never writes**. Classifier returns the 7 conservative states;
unknown is the default when idleness can't be proven, which routes delivery to the queue.

### MCP server (`portal_mcp.py`)
Newline-delimited JSON-RPC 2.0 over stdio: `initialize`, `notifications/initialized`,
`tools/list`, `tools/call`. Each tool validates input against a JSON schema and calls a
`portal_core` function; errors return structured JSON-RPC errors. Binds nothing to the
network. Core logic is importable and tested without spawning the transport; the transport
layer is tested by feeding request frames.

### Deploy / verify
`python scripts/sync.py --deploy --target both` materializes to both homes;
`--check --target both` must report in sync; installed-copy assertions verify target-specific
materialization (Claude: no `agents/`/`overlays/`; Codex: `agents/openai.yaml`) and that
unrelated skills + `.local-state/` are preserved.
