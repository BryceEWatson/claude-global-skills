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

### What changed after the Codex audit (v2)

An independent audit found the first cut trusted the *caller* for things it must not: who
you are, whether you're authorized, and whether a message was actually received. The
hardened design fixes that with three load-bearing decisions, all reflected below:

1. **Authenticated principals.** Identity is a bearer token the operator mints per session
   (`issue-principal`); the server derives source / inbox / acknowledger from it. No caller
   asserts its own identity.
2. **Operator grants, not caller booleans.** Steering and "speak as the user" are operator
   capability grants (scoped, expiring, revocable), replacing the `authorized` /
   `accepts_steering` flags a caller used to set.
3. **Pull-only delivery.** A message is `delivered` only when the authenticated recipient
   pulls it — the one honest proof of receipt. Every push / auto-resume / native-delivery
   "receipt" that couldn't prove acceptance was removed rather than faked; the adapters are
   now advisory-only. Boundary and liveness are derived from runtime-owned evidence, never a
   caller-supplied `boundary` / `process_alive`.

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
- Authenticated principals: operator-minted bearer tokens (salted-hash stored, expiring,
  revocable) bind each MCP server to one session; source / inbox owner / acknowledger are
  derived from the principal server-side, never from a tool argument.
- Conservative session-state reporting (advisory): `active`, `idle`, `waiting-for-user`,
  `completed`, `unavailable`, `stale`, `unknown` — derived from read-only runtime-owned
  evidence (log mtime/age, explicit completion markers, host liveness evidence). A quiet
  transcript never alone proves idleness. Not a caller-asserted state.
- Authorization + privacy gates: steering (send + accept) and `authorship=user` are
  operator capability grants (scoped, expiring, revocable) — not caller booleans; authorship
  (user vs agent-suggestion) recorded alongside the authenticated sender; strict
  identifier/encoding/size validation; rejection of prohibited content (hidden reasoning, raw
  tool args, signatures, encrypted blobs, system/developer instructions,
  credentials/tokens/env); secret rejection; loop/ping-pong prevention; local-only, no
  network bind.
- Pull-only delivery: a message becomes delivered only when the authenticated recipient pulls
  its own inbox. No sender-side push, no auto-resume, no native-task push — the adapters
  advise (classify a recipient, describe an operator resume) but never deliver, resume, or
  fabricate a receipt.
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
- All automated checks pass locally and in CI (Node + Python matrix, py_compile). The CI
  Python suite (`.github/workflows/ci.yml`, "Run session-portal tests") includes
  `test_portal_e2e.py`, a cross-runtime round trip over the **real** MCP stdio transport with
  authenticated principals. The send/pull/ack round trip itself uses no admin/db shortcuts;
  token minting and DB teardown are the operator setup/teardown.
- Portal deployed to **both** `~/.claude/skills` and `~/.codex/skills`; installed copies
  verified (Claude has no `agents/`/`overlays/`; Codex has `agents/openai.yaml`), not just
  repo fixtures.
- Genuine bidirectional round trip passes with audit evidence: `tests/e2e_live.py` spawns a
  real disposable headless Claude session (real transcript) and drives both directions
  through the real MCP transport; delivery/ack/audit hold, neither transcript is mutated
  (sha256 identical), spoofing is impossible, and the disposable sessions + DB are cleaned
  up. (Codex `exec` needs interactive ChatGPT auth a headless run lacks, so the Codex side
  uses a read-only copy of a real Codex rollout as genuine runtime evidence — stated plainly,
  not papered over.)
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
    test_portal_core.py          # storage, migrations, principals, grants, lifecycle, leases (CAS)
    test_portal_state.py         # read-only classifier
    test_portal_mcp.py           # authenticated JSON-RPC dispatch + identity derivation
    test_portal_delivery.py      # advisory adapters (no delivery side effects)
    test_portal_e2e.py           # real MCP-transport cross-runtime round trip (CI)
    e2e_live.py                  # manual: genuine round trip vs a real headless Claude session
```

### Storage (`portal_core.py`)
- DB at `$SESSION_PORTAL_HOME/portal.db` (default `~/.session-portal/`; env-overridable
  for hermetic tests). `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout`.
- Tables (schema **v2**): `schema_version`; `sessions(session_id PK, product,
  runtime_session_id, cwd, label, registered_at, last_seen_at, last_state, meta)`;
  `principals(session_id PK, product, token_hash, created_at, expires_at, revoked, label)`;
  `auth_grants(id PK, grantee, capability, scope, created_at, expires_at, revoked)`;
  `messages(message_id PK, idempotency_key UNIQUE, source_session_id, dest_session_id,
  dest_product, body, authorship, kind, status, created_at, updated_at, expires_at,
  delivered_at, delivered_to, acknowledged_at, acknowledged_by, attempts, last_error,
  forward_depth, root_message_id)`; `message_events(id PK, message_id, ts, event, detail)`;
  `leases(dest_session_id PK, holder, acquired_at, expires_at)`.
- Migrations keyed on `schema_version`; `init_db()` is idempotent and creates-or-migrates. The
  v1→v2 step adds `principals`/`auth_grants` and the `delivered_to`/`acknowledged_by` columns
  in place (additive; a v1 DB upgrades without data loss).
- Idempotency: `message_id = "msg_"+sha256(idempotency_key)[:24]`; `INSERT OR IGNORE` on
  the unique key; a duplicate send returns the existing id + status, no new row/event.
- Serialized delivery + lease CAS: `acquire_lease(dest)` is a single-statement `INSERT`
  guarded by the destination PK's UNIQUE constraint — that statement *is* the compare-and-set
  (SQLite serializes writers; exactly one contender wins), so no wrapping transaction is used.
  A re-entrant same holder returns true; `release_lease` is holder-scoped. Crash recovery:
  expired leases are reclaimed before each attempt; sends are atomic (rolled back on crash);
  `delivered`-without-ack persists durably until ack/expiry.

### Lifecycle + audit
Transitions each append a `message_events` row: `created`, `delivered`,
`delivery_refused`, `acknowledged`, `cancelled`, `expired`, `failed`. Guards reject
illegal transitions (e.g. ack of a cancelled/expired message).

### Identity & authorization (`portal_core`)
- Principals: `issue_principal(product, runtime)` mints a `secrets.token_urlsafe(32)` bearer
  token, stores only `sha256(salt+token)` (machine-local salt at `$SESSION_PORTAL_HOME/
  .token-salt`, chmod 0600), with an expiry (default 12 h) and a revoke flag.
  `resolve_principal` does a constant-time (`hmac.compare_digest`) lookup and rejects
  unknown/expired/revoked tokens.
- Grants: `grant_capability(grantee, capability, scope, ttl)` records an operator capability
  (`send-steer` / `accept-steering` / `speak-as-user`), scoped to a counterparty (or `*`),
  expiring, revocable. `has_capability` is the live check.

### Validation & message authorization (`portal_core`)
- Identifiers: `^[A-Za-z0-9._:-]{1,128}$`. Body ≤ 4 KiB, valid UTF-8, no control chars
  except `\n`/`\t`. Reject unknown/prohibited payload fields (reasoning, tool_args,
  signature, encrypted, system, developer, credentials, token, env, secret).
- Secret scan on body → reject (documented; redaction is the alternative, we choose
  reject for a clear invariant).
- Steering: `kind="steer"` requires the (authenticated) source to hold a live `send-steer`
  grant scoped to the destination; delivering it additionally requires the destination to
  hold a live `accept-steering` grant scoped to the source. `authorship="user"` requires a
  `speak-as-user` grant. No caller-supplied `authorized` flag exists.
- Loop prevention: `source==dest` rejected; `forward_depth` capped (default 1); a message
  that re-forwards an agent-authored, already-forwarded message on the same reversed pair
  is rejected (ping-pong guard).
- The portal has **no** code path that approves permissions, publishes, merges, pushes, or
  deletes based on message content; delivery returns data only. Asserted by test.

### Session-state (`portal_state.py`, read-only, advisory)
Reuses the `monitor-agent-thread` evidence surfaces (log locations, mtime/age, explicit
turn-complete markers) but **never writes** and **never trusts a caller-asserted state**.
Liveness comes from host evidence (`host_process_alive`, a runtime-written file at
`$SESSION_PORTAL_HOST_EVIDENCE`), not a caller argument. The classifier returns the 7
conservative states; `unknown` is the default when idleness can't be proven. It is advisory
(it informs whether to notify a human) and does not itself deliver.

### MCP server (`portal_mcp.py`, authenticated)
Newline-delimited JSON-RPC 2.0 over stdio: `initialize`, `notifications/initialized`,
`tools/list`, `tools/call`. The server resolves `SESSION_PORTAL_TOKEN` to a principal and
derives identity from it: `portal_send_message` takes no `source_session_id`,
`portal_list_inbox` drains only the principal's own inbox with no `boundary` argument, and
`portal_acknowledge`/reads are limited to messages the principal is a party to. Every
identity-bearing tool requires a token; only `portal_health` and the protocol methods do not.
Each tool validates input against a JSON schema; errors return structured JSON-RPC errors.
Binds nothing to the network. `dispatch()` is pure and tested without a process; the transport
is additionally tested by a real subprocess round trip (`test_portal_e2e.py`).

### Deploy / verify
`python scripts/sync.py --deploy --target both` materializes to both homes;
`--check --target both` must report in sync; installed-copy assertions verify target-specific
materialization (Claude: no `agents/`/`overlays/`; Codex: `agents/openai.yaml`) and that
unrelated skills + `.local-state/` are preserved.
