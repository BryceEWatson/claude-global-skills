#!/usr/bin/env python3
"""portal_core.py — durable storage + lifecycle + validation for the session portal.

The portal is a safe, local mailbox between Claude Code and Codex sessions. This module
is the pure core: it owns the SQLite schema, the message lifecycle state machine, and
every authorization / privacy / identifier check. It never touches a transcript, never
launches or resumes a session, and never executes message content — delivery returns
data only. The MCP transport (portal_mcp.py) and admin CLI (portal_admin.py) are thin
wrappers over the functions here.

Safety invariants enforced here (see docs/session-portal-plan.md and references/):
  * A message is untrusted DATA with a recorded author (user vs agent-suggestion).
  * Steering (kind="steer") requires explicit authorization to send AND to deliver.
  * Sends are idempotent when the caller supplies an idempotency key.
  * Delivery to one destination is serialized by a lease; crashes never double-deliver.
  * Prohibited content (reasoning, raw tool args, signatures, encrypted blobs, system/
    developer instructions, credentials/tokens/env, secrets) is rejected, not stored.
  * Loops / ping-pong are prevented by a forward-depth cap and a reversed-pair guard.
  * Nothing binds to the network; the DB lives in a user-level directory.

stdlib only; Windows-safe.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
VALID_PRODUCTS = ("claude", "codex")
VALID_AUTHORSHIP = ("user", "agent")
VALID_KINDS = ("note", "steer")

# Lifecycle states (see the state machine in _LEGAL_TRANSITIONS).
STATUS_QUEUED = "queued"
STATUS_DELIVERED = "delivered"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"
TERMINAL_STATES = {STATUS_ACKNOWLEDGED, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_FAILED}

# Legal message status transitions. Anything not listed is rejected (ConflictError).
_LEGAL_TRANSITIONS = {
    STATUS_QUEUED: {STATUS_DELIVERED, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_FAILED},
    STATUS_DELIVERED: {STATUS_ACKNOWLEDGED, STATUS_CANCELLED, STATUS_EXPIRED, STATUS_FAILED},
    STATUS_ACKNOWLEDGED: set(),
    STATUS_CANCELLED: set(),
    STATUS_EXPIRED: set(),
    STATUS_FAILED: set(),
}

# Boundaries at which a recipient may safely PULL its own inbox (it is between turns).
SAFE_PULL_BOUNDARIES = {"session_start", "prompt_submit", "stop", "command", "heartbeat"}
# Destination states into which a message may be PUSHED (recipient paused but alive).
# `completed` routes through the guarded resume adapter; `stale` intentionally waits in the
# queue until its TTL (closure is unproven, so we neither push nor resume it).
PUSH_DELIVERABLE_STATES = {"idle", "waiting-for-user"}

MAX_BODY_BYTES = 4096
MAX_LABEL_LEN = 256
MAX_FORWARD_DEPTH = 1
DEFAULT_TTL_SECONDS = 24 * 3600
_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_RUNTIME_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,120}$")

# Fields that must NEVER appear in a portal message payload. The MCP layer only accepts a
# whitelisted set of arguments; this list is the belt-and-suspenders reject check so a
# prohibited key can't ride in even if a caller bypasses the schema.
PROHIBITED_FIELDS = frozenset({
    "reasoning", "thinking", "chain_of_thought", "cot", "hidden",
    "tool_args", "tool_arguments", "raw_tool_input", "arguments",
    "signature", "sig", "encrypted", "ciphertext",
    "system", "system_prompt", "developer", "developer_message",
    "credentials", "credential", "password", "secret", "secrets",
    "token", "api_key", "apikey", "access_token", "env", "environment",
})

# Secret-shaped content in a message body → reject (documented invariant; the alternative
# would be redaction, we choose reject for a clear, testable line).
_SECRET_RE = re.compile(
    r"(sk-ant-[A-Za-z0-9-]{8,}|ghp_[A-Za-z0-9]{20,}|github_pat_[0-9A-Za-z_]{22,}|"
    r"glpat-[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:api[_-]?key|access[_-]?token|secret|bearer)\b\s*[:=]\s*\S{8,})",
    re.IGNORECASE,
)
# Control chars other than tab/newline/carriage-return are rejected in a body.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# --------------------------------------------------------------------------- #
# Errors (each carries a stable machine code for the MCP layer)
# --------------------------------------------------------------------------- #
class PortalError(Exception):
    code = "portal_error"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ValidationError(PortalError):
    code = "validation_error"


class AuthorizationError(PortalError):
    code = "authorization_error"


class ConflictError(PortalError):
    code = "conflict"


class NotFoundError(PortalError):
    code = "not_found"


# --------------------------------------------------------------------------- #
# Clock (injectable so tests are deterministic without Date/random)
# --------------------------------------------------------------------------- #
_clock: Callable[[], float] = time.time


def set_clock(fn: Callable[[], float]) -> None:
    global _clock
    _clock = fn


def now() -> float:
    return float(_clock())


# --------------------------------------------------------------------------- #
# Paths / connection
# --------------------------------------------------------------------------- #
def portal_home() -> Path:
    env = os.environ.get("SESSION_PORTAL_HOME")
    return Path(env).resolve() if env else (Path.home() / ".session-portal").resolve()


def db_path() -> Path:
    env = os.environ.get("SESSION_PORTAL_DB")
    return Path(env).resolve() if env else (portal_home() / "portal.db")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating parent dirs) a WAL, foreign-key-enforcing connection."""
    p = Path(path) if path is not None else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# --------------------------------------------------------------------------- #
# Schema + migrations
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    product             TEXT NOT NULL,
    runtime_session_id  TEXT NOT NULL,
    cwd                 TEXT,
    label               TEXT,
    registered          INTEGER NOT NULL DEFAULT 0,
    registered_at       REAL,
    last_seen_at        REAL,
    last_state          TEXT NOT NULL DEFAULT 'unknown',
    accepts_steering    INTEGER NOT NULL DEFAULT 0,
    meta                TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id          TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,
    source_session_id   TEXT NOT NULL,
    dest_session_id     TEXT NOT NULL,
    dest_product        TEXT NOT NULL,
    body                TEXT NOT NULL,
    authorship          TEXT NOT NULL,
    kind                TEXT NOT NULL,
    authorized          INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'queued',
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    expires_at          REAL,
    delivered_at        REAL,
    acknowledged_at     REAL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    forward_depth       INTEGER NOT NULL DEFAULT 0,
    root_message_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_dest ON messages(dest_session_id, status);
CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source_session_id);

CREATE TABLE IF NOT EXISTS message_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  TEXT NOT NULL,
    ts          REAL NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_message ON message_events(message_id, id);

CREATE TABLE IF NOT EXISTS leases (
    dest_session_id TEXT PRIMARY KEY,
    holder          TEXT NOT NULL,
    acquired_at     REAL NOT NULL,
    expires_at      REAL NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if absent and run migrations. Idempotent."""
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        return
    current = int(row["version"])
    # Future migrations: while current < SCHEMA_VERSION: apply step; current += 1.
    if current > SCHEMA_VERSION:
        raise PortalError(
            f"database schema v{current} is newer than this code (v{SCHEMA_VERSION}); upgrade the portal",
            code="schema_too_new",
        )
    if current != SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _valid_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationError(f"invalid {what}: must match {_ID_RE.pattern}")
    return value


def make_session_id(product: str, runtime_session_id: str) -> str:
    if product not in VALID_PRODUCTS:
        raise ValidationError(f"unknown product {product!r}; allowed: {list(VALID_PRODUCTS)}")
    if not isinstance(runtime_session_id, str) or not _RUNTIME_ID_RE.match(runtime_session_id):
        raise ValidationError("invalid runtime_session_id")
    return f"{product}:{runtime_session_id}"


def product_of(session_id: str) -> str:
    _valid_id(session_id, "session_id")
    product = session_id.split(":", 1)[0]
    if product not in VALID_PRODUCTS:
        raise ValidationError(f"session_id must start with a product prefix ({VALID_PRODUCTS})")
    return product


def validate_no_prohibited_fields(payload: dict[str, Any]) -> None:
    """Reject a payload that carries any prohibited key (defense in depth for the MCP
    schema whitelist). Case-insensitive on the top-level keys."""
    if not isinstance(payload, dict):
        return
    bad = sorted({k for k in payload if str(k).lower() in PROHIBITED_FIELDS})
    if bad:
        raise ValidationError(f"prohibited field(s) not allowed in a portal message: {bad}")


def validate_body(body: str) -> str:
    if not isinstance(body, str):
        raise ValidationError("body must be a string")
    if body == "":
        raise ValidationError("body must not be empty")
    encoded = body.encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        raise ValidationError(f"body exceeds {MAX_BODY_BYTES} bytes ({len(encoded)})")
    try:
        encoded.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("body is not valid UTF-8")
    if _CTRL_RE.search(body):
        raise ValidationError("body contains disallowed control characters")
    if _SECRET_RE.search(body):
        raise ValidationError("body appears to contain a secret/credential; rejected")
    return body


# --------------------------------------------------------------------------- #
# Row helpers
# --------------------------------------------------------------------------- #
def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    if "meta" in d and isinstance(d["meta"], str) and d["meta"]:
        try:
            d["meta"] = json.loads(d["meta"])
        except json.JSONDecodeError:
            pass
    return d


def _log_event(conn: sqlite3.Connection, message_id: str, event: str, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO message_events(message_id, ts, event, detail) VALUES (?,?,?,?)",
        (message_id, now(), event, detail),
    )


@contextlib.contextmanager
def _atomic(conn: sqlite3.Connection):
    """Group multiple writes into one all-or-nothing transaction. The connection runs in
    autocommit (isolation_level=None), so a composite op (a row change + its audit event,
    or an insert + its 'created' event) would otherwise commit statement-by-statement and a
    crash could split them. BEGIN IMMEDIATE takes the write lock up front; COMMIT on success,
    ROLLBACK on error. Not nested — callers wrap a whole op, and helpers inside never wrap."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def register_session(conn: sqlite3.Connection, product: str, runtime_session_id: str,
                     cwd: str | None = None, label: str | None = None,
                     meta: dict | None = None, accepts_steering: bool = False) -> dict:
    """Register (or update) a session. Idempotent upsert keyed on session_id."""
    session_id = make_session_id(product, runtime_session_id)
    if label is not None and len(label) > MAX_LABEL_LEN:
        raise ValidationError(f"label exceeds {MAX_LABEL_LEN} chars")
    ts = now()
    meta_json = json.dumps(meta) if meta is not None else None
    conn.execute(
        """INSERT INTO sessions(session_id, product, runtime_session_id, cwd, label,
                registered, registered_at, last_seen_at, last_state, accepts_steering, meta)
           VALUES (?,?,?,?,?,1,?,?,'unknown',?,?)
           ON CONFLICT(session_id) DO UPDATE SET
                cwd=COALESCE(excluded.cwd, sessions.cwd),
                label=COALESCE(excluded.label, sessions.label),
                registered=1,
                registered_at=COALESCE(sessions.registered_at, excluded.registered_at),
                last_seen_at=excluded.last_seen_at,
                accepts_steering=excluded.accepts_steering,
                meta=COALESCE(excluded.meta, sessions.meta)""",
        (session_id, product, runtime_session_id, cwd, label, ts, ts,
         1 if accepts_steering else 0, meta_json),
    )
    return get_session(conn, session_id)


def _ensure_session(conn: sqlite3.Connection, session_id: str) -> None:
    """Create a placeholder row for a not-yet-registered session (so a message can be
    queued before the recipient first checks in). product comes from the id prefix."""
    if conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone():
        return
    product = product_of(session_id)
    runtime = session_id.split(":", 1)[1]
    conn.execute(
        """INSERT OR IGNORE INTO sessions(session_id, product, runtime_session_id,
                registered, last_seen_at, last_state)
           VALUES (?,?,?,0,?, 'unknown')""",
        (session_id, product, runtime, now()),
    )


def get_session(conn: sqlite3.Connection, session_id: str) -> dict:
    _valid_id(session_id, "session_id")
    row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"session not found: {session_id}")
    return _row_to_dict(row)


def touch_session(conn: sqlite3.Connection, session_id: str, state: str | None = None) -> None:
    if state is not None:
        conn.execute("UPDATE sessions SET last_seen_at=?, last_state=? WHERE session_id=?",
                     (now(), state, session_id))
    else:
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?", (now(), session_id))


def list_sessions(conn: sqlite3.Connection, product: str | None = None,
                  registered_only: bool = False) -> list[dict]:
    q = "SELECT * FROM sessions"
    clauses, params = [], []
    if product is not None:
        if product not in VALID_PRODUCTS:
            raise ValidationError(f"unknown product {product!r}")
        clauses.append("product=?")
        params.append(product)
    if registered_only:
        clauses.append("registered=1")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY last_seen_at DESC"  # SQLite sorts NULLs last under DESC
    rows = conn.execute(q, params).fetchall()
    return [_row_to_dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Sending (idempotent)
# --------------------------------------------------------------------------- #
def _derive_idempotency_key(source: str, dest: str, provided: str | None) -> str:
    """The stored idempotency identity. A caller-supplied key is SCOPED to (source, dest)
    so the same natural key (e.g. a task id) reused across different recipients yields
    DISTINCT messages — a global key would let a send to C collide with an earlier send to
    B and silently drop C's message (returning B's body). No key -> unique per call."""
    if provided is not None:
        _valid_id(provided, "idempotency_key")
        return f"{source}\x1f{dest}\x1f{provided}"
    return "auto-" + os.urandom(16).hex()


def message_id_for(idempotency_key: str) -> str:
    return "msg_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]


def send_message(conn: sqlite3.Connection, source_session_id: str, dest_session_id: str,
                 body: str, *, authorship: str, kind: str = "note",
                 authorized: bool = False, idempotency_key: str | None = None,
                 ttl_seconds: int | None = None, forward_of: str | None = None) -> dict:
    """Queue a message. Idempotent when idempotency_key is supplied (a repeat returns the
    existing message unchanged). Enforces all authorization / privacy / loop guards."""
    _valid_id(source_session_id, "source_session_id")
    _valid_id(dest_session_id, "dest_session_id")
    src_product = product_of(source_session_id)
    dest_product = product_of(dest_session_id)
    if source_session_id == dest_session_id:
        raise ValidationError("a session cannot message itself (loop guard)")
    if authorship not in VALID_AUTHORSHIP:
        raise ValidationError(f"authorship must be one of {VALID_AUTHORSHIP}")
    if kind not in VALID_KINDS:
        raise ValidationError(f"kind must be one of {VALID_KINDS}")
    validate_body(body)
    if kind == "steer" and not authorized:
        raise AuthorizationError("sending a steering message requires explicit authorization")

    depth = 0
    root = None
    if forward_of is not None:
        parent = conn.execute("SELECT * FROM messages WHERE message_id=?", (forward_of,)).fetchone()
        if parent is None:
            raise NotFoundError(f"forward_of message not found: {forward_of}")
        depth = int(parent["forward_depth"]) + 1
        root = parent["root_message_id"] or parent["message_id"]
        if depth > MAX_FORWARD_DEPTH:
            raise ValidationError(
                f"forward depth {depth} exceeds cap {MAX_FORWARD_DEPTH} (loop prevention)")
        # Ping-pong guard: refuse forwarding an agent-authored message straight back to
        # its origin (A->B then B->A of the same chain).
        if (parent["source_session_id"] == dest_session_id
                and parent["dest_session_id"] == source_session_id
                and authorship == "agent"):
            raise ValidationError("reversed agent-authored forward rejected (ping-pong guard)")

    key = _derive_idempotency_key(source_session_id, dest_session_id, idempotency_key)
    mid = message_id_for(key)
    ts = now()
    expires_at = ts + (ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_SECONDS)

    _ensure_session(conn, dest_session_id)
    _ensure_session(conn, source_session_id)

    detail = f"kind={kind} authorship={authorship} authorized={int(authorized)}"
    if forward_of:
        detail += f" forward_of={forward_of} depth={depth}"
    # The message row and its 'created' audit event commit together (or not at all).
    with _atomic(conn):
        cur = conn.execute(
            """INSERT OR IGNORE INTO messages(message_id, idempotency_key, source_session_id,
                    dest_session_id, dest_product, body, authorship, kind, authorized, status,
                    created_at, updated_at, expires_at, forward_depth, root_message_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, key, source_session_id, dest_session_id, dest_product, body, authorship, kind,
             1 if authorized else 0, STATUS_QUEUED, ts, ts, expires_at, depth, root),
        )
        inserted = cur.rowcount != 0
        if inserted:
            _log_event(conn, mid, "created", detail)
    if not inserted:
        # Idempotent hit: the message already exists. Return it unchanged.
        existing = get_message_status(conn, mid)
        existing["idempotent_duplicate"] = True
        return existing
    return get_message_status(conn, mid)


# --------------------------------------------------------------------------- #
# Leases (serialize delivery per destination; reclaim after crash)
# --------------------------------------------------------------------------- #
LEASE_TTL_SECONDS = 120


def reclaim_expired_leases(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM leases WHERE expires_at < ?", (now(),))
    return cur.rowcount or 0


def acquire_lease(conn: sqlite3.Connection, dest_session_id: str, holder: str,
                  ttl_seconds: int = LEASE_TTL_SECONDS) -> bool:
    """Single-flight lease for a destination. Returns False if another live holder owns it."""
    reclaim_expired_leases(conn)
    ts = now()
    try:
        conn.execute(
            "INSERT INTO leases(dest_session_id, holder, acquired_at, expires_at) VALUES (?,?,?,?)",
            (dest_session_id, holder, ts, ts + ttl_seconds),
        )
        return True
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT holder FROM leases WHERE dest_session_id=?",
                           (dest_session_id,)).fetchone()
        return bool(row) and row["holder"] == holder


def release_lease(conn: sqlite3.Connection, dest_session_id: str, holder: str) -> None:
    conn.execute("DELETE FROM leases WHERE dest_session_id=? AND holder=?",
                 (dest_session_id, holder))


# --------------------------------------------------------------------------- #
# Lifecycle transitions
# --------------------------------------------------------------------------- #
def _get_message_row(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row:
    _valid_id(message_id, "message_id")
    row = conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"message not found: {message_id}")
    return row


def _transition(conn: sqlite3.Connection, row: sqlite3.Row, new_status: str,
                extra: dict[str, Any] | None = None, event_detail: str | None = None) -> None:
    old = row["status"]
    if new_status not in _LEGAL_TRANSITIONS.get(old, set()):
        raise ConflictError(f"illegal transition {old} -> {new_status} for {row['message_id']}")
    sets = ["status=?", "updated_at=?"]
    params: list[Any] = [new_status, now()]
    for k, v in (extra or {}).items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(row["message_id"])
    # The status change and its audit event commit together (or not at all).
    with _atomic(conn):
        conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE message_id=?", params)
        _log_event(conn, row["message_id"], new_status, event_detail)


def expire_due(conn: sqlite3.Connection) -> int:
    """Sweep messages past their TTL (queued/delivered -> expired). Returns count."""
    ts = now()
    rows = conn.execute(
        "SELECT * FROM messages WHERE expires_at IS NOT NULL AND expires_at < ? "
        "AND status IN (?,?)", (ts, STATUS_QUEUED, STATUS_DELIVERED)).fetchall()
    for row in rows:
        _transition(conn, row, STATUS_EXPIRED, event_detail="ttl elapsed")
    return len(rows)


def acknowledge(conn: sqlite3.Connection, message_id: str, by: str | None = None,
                note: str | None = None) -> dict:
    row = _get_message_row(conn, message_id)
    if row["status"] == STATUS_ACKNOWLEDGED:
        return get_message_status(conn, message_id)  # idempotent ack
    detail = f"by={by}" + (f" note={note[:80]}" if note else "")
    _transition(conn, row, STATUS_ACKNOWLEDGED, extra={"acknowledged_at": now()},
                event_detail=detail)
    return get_message_status(conn, message_id)


def cancel_message(conn: sqlite3.Connection, message_id: str, by: str | None = None,
                   reason: str | None = None) -> dict:
    row = _get_message_row(conn, message_id)
    if row["status"] == STATUS_CANCELLED:
        return get_message_status(conn, message_id)
    if row["status"] in (STATUS_ACKNOWLEDGED, STATUS_EXPIRED, STATUS_FAILED):
        raise ConflictError(f"cannot cancel a {row['status']} message")
    _transition(conn, row, STATUS_CANCELLED, event_detail=f"by={by} reason={reason or ''}"[:160])
    return get_message_status(conn, message_id)


def fail_message(conn: sqlite3.Connection, message_id: str, error: str) -> dict:
    row = _get_message_row(conn, message_id)
    _transition(conn, row, STATUS_FAILED,
                extra={"last_error": error[:500], "attempts": int(row["attempts"]) + 1},
                event_detail=error[:160])
    return get_message_status(conn, message_id)


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def _steer_refusal_reason(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Return a reason a steering message may NOT be delivered yet, or None if it may.
    A steer may deliver only if it was authorized AND the destination opted into steering.
    This is a SOFT gate: like every other delivery gate it leaves the message queued rather
    than raising, so one undeliverable steer can never poison a batch inbox pull."""
    if row["kind"] != "steer":
        return None
    if not row["authorized"]:
        return "unauthorized steering message; left queued"
    dest = conn.execute("SELECT accepts_steering FROM sessions WHERE session_id=?",
                        (row["dest_session_id"],)).fetchone()
    if not dest or not dest["accepts_steering"]:
        return "destination has not authorized steering; left queued"
    return None


def deliver_one(conn: sqlite3.Connection, message_id: str, *, mode: str, boundary: str | None,
                dest_state: str | None, holder: str) -> dict:
    """Attempt to mark ONE queued message delivered. Serialized by a destination lease.

    mode="pull": the recipient is fetching its OWN inbox at a safe boundary; delivery is
        allowed iff `boundary` is a recognized safe pull boundary.
    mode="push": an adapter is pushing to the recipient; delivery is allowed iff the
        recipient's conservative state is in PUSH_DELIVERABLE_STATES. active/unknown/
        unavailable/completed/stale => refuse (leave queued) — "queue when unproven".
    Refusal never raises for the ordinary not-safe case; it records a delivery_refused
    event and returns the (still-queued) message with a `delivered: False` flag.
    """
    row = _get_message_row(conn, message_id)
    if row["status"] != STATUS_QUEUED:
        raise ConflictError(f"message is {row['status']}, not queued")

    allowed, reason = _delivery_allowed(mode, boundary, dest_state)
    if not allowed:
        _log_event(conn, message_id, "delivery_refused", reason)
        out = get_message_status(conn, message_id)
        out["delivered"] = False
        out["refused_reason"] = reason
        return out

    # Steering authorization is a SOFT gate (leaves the message queued, never raises), so a
    # single undeliverable steer can't abort a batch inbox pull of sibling messages.
    steer_reason = _steer_refusal_reason(conn, row)
    if steer_reason is not None:
        _log_event(conn, message_id, "delivery_refused", steer_reason)
        out = get_message_status(conn, message_id)
        out["delivered"] = False
        out["refused_reason"] = steer_reason
        return out

    if not acquire_lease(conn, row["dest_session_id"], holder):
        _log_event(conn, message_id, "delivery_refused", "destination lease held by another deliverer")
        out = get_message_status(conn, message_id)
        out["delivered"] = False
        out["refused_reason"] = "lease_held"
        return out
    try:
        row = _get_message_row(conn, message_id)  # re-read under lease
        if row["status"] != STATUS_QUEUED:
            out = get_message_status(conn, message_id)
            out["delivered"] = (row["status"] == STATUS_DELIVERED)
            return out
        _transition(conn, row, STATUS_DELIVERED,
                    extra={"delivered_at": now(), "attempts": int(row["attempts"]) + 1},
                    event_detail=f"mode={mode} boundary={boundary} dest_state={dest_state}")
    finally:
        release_lease(conn, row["dest_session_id"], holder)
    out = get_message_status(conn, message_id)
    out["delivered"] = True
    return out


def _delivery_allowed(mode: str, boundary: str | None, dest_state: str | None) -> tuple[bool, str]:
    if mode == "pull":
        if boundary in SAFE_PULL_BOUNDARIES:
            return True, "ok"
        return False, f"boundary {boundary!r} is not a recognized safe pull boundary"
    if mode == "push":
        if dest_state in PUSH_DELIVERABLE_STATES:
            return True, "ok"
        return False, f"destination state {dest_state!r} is not push-deliverable (queued)"
    return False, f"unknown delivery mode {mode!r}"


def list_inbox(conn: sqlite3.Connection, dest_session_id: str, *, status: str | None = None,
               deliver: bool = False, boundary: str | None = None, dest_state: str | None = None,
               holder: str | None = None, max_deliver: int | None = None,
               include_body: bool = True) -> list[dict]:
    """Return messages addressed to dest_session_id. With deliver=True this is the safe
    PULL path: queued messages are transitioned to delivered at the given safe boundary,
    serialized per destination. Expired messages are swept first so they never deliver."""
    _valid_id(dest_session_id, "dest_session_id")
    expire_due(conn)
    if deliver:
        holder = holder or f"pull:{dest_session_id}"
        queued = conn.execute(
            "SELECT message_id FROM messages WHERE dest_session_id=? AND status=? ORDER BY created_at",
            (dest_session_id, STATUS_QUEUED)).fetchall()
        count = 0
        for r in queued:
            if max_deliver is not None and count >= max_deliver:
                break
            res = deliver_one(conn, r["message_id"], mode="pull", boundary=boundary,
                              dest_state=dest_state, holder=holder)
            if res.get("delivered"):
                count += 1
    q = "SELECT * FROM messages WHERE dest_session_id=?"
    params: list[Any] = [dest_session_id]
    if status is not None:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY created_at"
    rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if not include_body:
            d.pop("body", None)
        out.append(d)
    return out


def get_message_status(conn: sqlite3.Connection, message_id: str) -> dict:
    row = _get_message_row(conn, message_id)
    return _row_to_dict(row)


def message_events(conn: sqlite3.Connection, message_id: str) -> list[dict]:
    _valid_id(message_id, "message_id")
    rows = conn.execute(
        "SELECT ts, event, detail FROM message_events WHERE message_id=? ORDER BY id",
        (message_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def health(conn: sqlite3.Connection) -> dict:
    init_db(conn)
    reclaim_expired_leases(conn)
    expired = expire_due(conn)
    counts = {}
    for row in conn.execute("SELECT status, COUNT(*) c FROM messages GROUP BY status"):
        counts[row["status"]] = row["c"]
    sess = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
    leases = conn.execute("SELECT COUNT(*) c FROM leases").fetchone()["c"]
    ver = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    return {
        "ok": True,
        "db_path": str(db_path()),
        "schema_version": int(ver["version"]) if ver else None,
        "sessions": sess,
        "messages_by_status": counts,
        "active_leases": leases,
        "expired_swept": expired,
    }
