#!/usr/bin/env python3
"""portal_core.py — durable storage + lifecycle + validation for the session portal.

The portal is a safe, local mailbox between Claude Code and Codex sessions. This module
is the pure core: it owns the SQLite schema, the message lifecycle state machine, the
authenticated-principal + operator-grant model, and every authorization / privacy /
identifier check. It never touches a transcript, never launches or resumes a session, and
never executes message content — delivery returns data only. The MCP transport
(portal_mcp.py) and admin CLI (portal_admin.py) are thin wrappers over the functions here.

Safety invariants enforced here (see docs/session-portal-plan.md and references/):
  * Identity is authenticated, not asserted. A caller proves who it is with a bearer token
    that resolves to a PRINCIPAL (product:runtime_session_id). The message source, the
    inbox owner, and the acknowledger are all derived from that principal server-side —
    never taken from a tool argument.
  * Authorization is an operator-issued GRANT, not a caller boolean. Sending steering,
    accepting steering, and speaking as the user are capabilities the operator grants to a
    principal (scoped + expiring + revocable). There is no caller-supplied `authorized`.
  * A message is untrusted DATA with a recorded authorship claim AND an authenticated
    sender principal (the two are distinct: the sender is proven, the authorship label is
    a claim the operator vouches for via a grant).
  * Delivery is PULL-ONLY: a message becomes `delivered` only when the authenticated
    recipient itself pulls it. That is the only event that proves runtime acceptance. The
    portal never fabricates a delivery/resume/native receipt it cannot back with a pull.
  * Prohibited content (reasoning, raw tool args, signatures, encrypted blobs, system/
    developer instructions, credentials/tokens/env, secrets) is rejected, not stored.
  * Loops / ping-pong are prevented by a forward-depth cap and a reversed-pair guard.
  * Nothing binds to the network; the DB lives in a user-level directory.

stdlib only; Windows-safe.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 2
VALID_PRODUCTS = ("claude", "codex")
VALID_AUTHORSHIP = ("user", "agent")
VALID_KINDS = ("note", "steer")

# Operator-issued capabilities (rows in auth_grants). These REPLACE the old caller-supplied
# `authorized` / `accepts_steering` booleans: a caller can no longer flip a security gate.
CAP_SEND_STEER = "send-steer"          # principal may SEND kind="steer" (scope = dest id or *)
CAP_ACCEPT_STEERING = "accept-steering"  # principal may RECEIVE a steer (scope = source id or *)
CAP_SPEAK_AS_USER = "speak-as-user"    # principal may record authorship="user" (scope = *)
VALID_CAPABILITIES = (CAP_SEND_STEER, CAP_ACCEPT_STEERING, CAP_SPEAK_AS_USER)

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

MAX_BODY_BYTES = 4096
MAX_LABEL_LEN = 256
MAX_FORWARD_DEPTH = 1
DEFAULT_TTL_SECONDS = 24 * 3600
DEFAULT_PRINCIPAL_TTL_SECONDS = 12 * 3600
DEFAULT_GRANT_TTL_SECONDS = 3600
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
    meta                TEXT
);

-- An authenticated identity. A bearer token (only its salted hash is stored) resolves to
-- exactly one principal; the MCP server binds to it at startup and derives all identity
-- from it. Tokens are operator-minted, expiring, and revocable.
CREATE TABLE IF NOT EXISTS principals (
    session_id   TEXT PRIMARY KEY,
    product      TEXT NOT NULL,
    token_hash   TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL,
    revoked      INTEGER NOT NULL DEFAULT 0,
    label        TEXT
);
-- No index on token_hash on purpose: resolve_principal scans all rows with a constant-time
-- compare (hmac.compare_digest) rather than a hash-keyed lookup, so an index would never be
-- consulted and could leak timing about which hash exists.

-- Operator-issued capability grants (replace the old caller booleans). scope is a session
-- id the capability is limited to, or '*' for any.
CREATE TABLE IF NOT EXISTS auth_grants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    grantee     TEXT NOT NULL,
    capability  TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '*',
    created_at  REAL NOT NULL,
    expires_at  REAL,
    revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_grants_lookup ON auth_grants(grantee, capability, revoked);

CREATE TABLE IF NOT EXISTS messages (
    message_id          TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,
    source_session_id   TEXT NOT NULL,
    dest_session_id     TEXT NOT NULL,
    dest_product        TEXT NOT NULL,
    body                TEXT NOT NULL,
    authorship          TEXT NOT NULL,
    kind                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'queued',
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    expires_at          REAL,
    delivered_at        REAL,
    delivered_to        TEXT,
    acknowledged_at     REAL,
    acknowledged_by     TEXT,
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


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if absent and run migrations. Idempotent."""
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        return
    current = int(row["version"])
    if current > SCHEMA_VERSION:
        raise PortalError(
            f"database schema v{current} is newer than this code (v{SCHEMA_VERSION}); upgrade the portal",
            code="schema_too_new",
        )
    while current < SCHEMA_VERSION:
        _migrate_step(conn, current)
        current += 1
    if current != int(row["version"]):
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))


def _migrate_step(conn: sqlite3.Connection, from_version: int) -> None:
    """Apply the migration from `from_version` to from_version+1. Additive only."""
    if from_version == 1:
        # v1 -> v2: principals + auth_grants (created by executescript above), and new
        # message columns delivered_to / acknowledged_by. The legacy `authorized` column
        # (a caller boolean) is left in place but no longer consulted — grants supersede it.
        # The ADD COLUMN is idempotent under a concurrent startup race: if another process
        # added the column between our check and our ALTER, SQLite raises "duplicate column
        # name" — tolerate that rather than aborting startup.
        cols = _column_names(conn, "messages")
        for col in ("delivered_to", "acknowledged_by"):
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise


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


def runtime_of(session_id: str) -> str:
    _valid_id(session_id, "session_id")
    if ":" not in session_id:
        raise ValidationError("session_id must be product:runtime_session_id")
    return session_id.split(":", 1)[1]


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
# Principals (authenticated identity)
# --------------------------------------------------------------------------- #
_SALT_LEN = 32


def _read_salt(p: Path) -> bytes | None:
    """Read the salt file. Returns the salt if it is exactly _SALT_LEN bytes, None if the
    file is absent or empty, and raises on a non-empty wrong-length file (corruption) rather
    than silently hashing tokens with a weak salt."""
    try:
        b = p.read_bytes()
    except FileNotFoundError:
        return None
    if len(b) == _SALT_LEN:
        return b
    if b:
        raise PortalError(
            f"portal token salt at {p} is corrupt ({len(b)} bytes, expected {_SALT_LEN}); "
            "refusing to hash with it", code="salt_corrupt")
    return None


def _token_salt() -> bytes:
    """A machine-local salt so a stolen DB alone can't be brute-forced offline as easily.
    Stored beside the DB (0600 where the OS supports it); created exactly once, safely under
    concurrent first-use.

    The salt file anchors every issued token: if two processes both created it and the last
    write clobbered the first, tokens minted under the first salt would orphan. So the winner
    is chosen EXCLUSIVELY — a fully-written temp file is hard-linked into place (`os.link`
    fails with FileExistsError if the target exists), so the final file is always a complete
    _SALT_LEN bytes (never partial) and only one creator's salt ever lands; everyone else
    reads that single winner. A hardlink-less filesystem falls back to an O_EXCL create."""
    p = portal_home() / ".token-salt"
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_salt(p)
    if existing is not None:
        return existing

    salt = secrets.token_bytes(_SALT_LEN)
    tmp = p.with_name(f".token-salt.tmp-{os.getpid()}-{secrets.token_hex(6)}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(salt)
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        try:
            os.link(tmp, p)  # atomic exclusive: fails if p already exists
            return salt
        except FileExistsError:
            pass  # lost the race — fall through to read the winner
        except (OSError, NotImplementedError):
            # Filesystem without hardlink support: exclusive-create the final path directly.
            try:
                fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                try:
                    os.write(fd, salt)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return salt
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)

    winner = _read_salt(p)
    if winner is None:
        raise PortalError(f"portal token salt at {p} could not be established", code="salt_corrupt")
    return winner


def hash_token(token: str) -> str:
    return hashlib.sha256(_token_salt() + token.encode("utf-8")).hexdigest()


def issue_principal(conn: sqlite3.Connection, product: str, runtime_session_id: str, *,
                    ttl_seconds: int | None = None, label: str | None = None) -> dict:
    """Mint (or rotate) a bearer token for a session and record its salted hash. Returns the
    principal record WITH the plaintext token under `token` — shown once; only the hash is
    stored. Operator-only (admin CLI); never exposed as an MCP tool."""
    session_id = make_session_id(product, runtime_session_id)
    if label is not None and len(label) > MAX_LABEL_LEN:
        raise ValidationError(f"label exceeds {MAX_LABEL_LEN} chars")
    if ttl_seconds is not None and ttl_seconds < 0:
        raise ValidationError("ttl_seconds must be >= 0 (0 means never-expires)")
    token = secrets.token_urlsafe(32)
    th = hash_token(token)
    ts = now()
    ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_PRINCIPAL_TTL_SECONDS
    expires_at = ts + ttl if ttl and ttl > 0 else None
    conn.execute(
        """INSERT INTO principals(session_id, product, token_hash, created_at, expires_at, revoked, label)
           VALUES (?,?,?,?,?,0,?)
           ON CONFLICT(session_id) DO UPDATE SET
                token_hash=excluded.token_hash, created_at=excluded.created_at,
                expires_at=excluded.expires_at, revoked=0, label=COALESCE(excluded.label, principals.label)""",
        (session_id, product, th, ts, expires_at, label),
    )
    out = dict(conn.execute("SELECT * FROM principals WHERE session_id=?", (session_id,)).fetchone())
    out.pop("token_hash", None)
    out["token"] = token
    return out


def resolve_principal(conn: sqlite3.Connection, token: str) -> str:
    """Resolve a bearer token to its principal session_id. Raises AuthorizationError if the
    token is unknown, expired, or revoked. Constant-time hash comparison."""
    if not isinstance(token, str) or not (16 <= len(token) <= 512):
        raise AuthorizationError("invalid or missing portal token")
    th = hash_token(token)
    ts = now()
    for row in conn.execute("SELECT session_id, token_hash, expires_at, revoked FROM principals"):
        if hmac.compare_digest(row["token_hash"], th):
            if int(row["revoked"]):
                raise AuthorizationError("portal token has been revoked")
            if row["expires_at"] is not None and float(row["expires_at"]) < ts:
                raise AuthorizationError("portal token has expired")
            return row["session_id"]
    raise AuthorizationError("portal token is not recognized")


def revoke_principal(conn: sqlite3.Connection, session_id: str) -> bool:
    _valid_id(session_id, "session_id")
    cur = conn.execute("UPDATE principals SET revoked=1 WHERE session_id=?", (session_id,))
    return (cur.rowcount or 0) > 0


def list_principals(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT session_id, product, created_at, expires_at, revoked, label FROM principals "
        "ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Grants (operator-issued capabilities; scoped + expiring + revocable)
# --------------------------------------------------------------------------- #
def grant_capability(conn: sqlite3.Connection, grantee: str, capability: str, *,
                     scope: str = "*", ttl_seconds: int | None = None) -> dict:
    """Record an operator capability grant. Operator-only (admin CLI). scope='*' means any
    counterparty; otherwise it is a specific session id the capability is limited to."""
    _valid_id(grantee, "grantee")
    if capability not in VALID_CAPABILITIES:
        raise ValidationError(f"unknown capability {capability!r}; allowed: {list(VALID_CAPABILITIES)}")
    if scope != "*":
        _valid_id(scope, "scope")
    # speak-as-user is not counterparty-specific: authorship is a property of the sender, not
    # of any one recipient. A narrow scope would be silently ignored by has_capability(scope=
    # None) and read as universal, so reject it up front rather than mislead the operator.
    if capability == CAP_SPEAK_AS_USER and scope != "*":
        raise ValidationError("speak-as-user is a global capability; scope must be '*'")
    if ttl_seconds is not None and ttl_seconds < 0:
        raise ValidationError("ttl_seconds must be >= 0 (0 means never-expires)")
    ts = now()
    ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_GRANT_TTL_SECONDS
    expires_at = ts + ttl if ttl and ttl > 0 else None
    cur = conn.execute(
        "INSERT INTO auth_grants(grantee, capability, scope, created_at, expires_at, revoked) "
        "VALUES (?,?,?,?,?,0)", (grantee, capability, scope, ts, expires_at))
    return {"id": cur.lastrowid, "grantee": grantee, "capability": capability,
            "scope": scope, "expires_at": expires_at}


def has_capability(conn: sqlite3.Connection, grantee: str, capability: str,
                   scope: str | None = None) -> bool:
    """True iff `grantee` holds a live (non-revoked, non-expired) grant for `capability`
    whose scope is '*' or exactly matches `scope`."""
    ts = now()
    rows = conn.execute(
        "SELECT scope, expires_at FROM auth_grants WHERE grantee=? AND capability=? AND revoked=0",
        (grantee, capability)).fetchall()
    for r in rows:
        if r["expires_at"] is not None and float(r["expires_at"]) < ts:
            continue
        if r["scope"] == "*" or scope is None or r["scope"] == scope:
            return True
    return False


def revoke_grant(conn: sqlite3.Connection, grant_id: int) -> bool:
    cur = conn.execute("UPDATE auth_grants SET revoked=1 WHERE id=?", (int(grant_id),))
    return (cur.rowcount or 0) > 0


def list_grants(conn: sqlite3.Connection, grantee: str | None = None,
                include_inactive: bool = False) -> list[dict]:
    q = "SELECT * FROM auth_grants"
    clauses, params = [], []
    if grantee is not None:
        clauses.append("grantee=?")
        params.append(grantee)
    if not include_inactive:
        clauses.append("revoked=0")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def register_session(conn: sqlite3.Connection, product: str, runtime_session_id: str,
                     cwd: str | None = None, label: str | None = None,
                     meta: dict | None = None) -> dict:
    """Register (or update) a session. Idempotent upsert keyed on session_id. Note: whether
    a session accepts steering is NO LONGER a flag here — it is an operator grant
    (CAP_ACCEPT_STEERING). Registration only records discovery metadata."""
    session_id = make_session_id(product, runtime_session_id)
    if label is not None and len(label) > MAX_LABEL_LEN:
        raise ValidationError(f"label exceeds {MAX_LABEL_LEN} chars")
    ts = now()
    meta_json = json.dumps(meta) if meta is not None else None
    conn.execute(
        """INSERT INTO sessions(session_id, product, runtime_session_id, cwd, label,
                registered, registered_at, last_seen_at, last_state, meta)
           VALUES (?,?,?,?,?,1,?,?,'unknown',?)
           ON CONFLICT(session_id) DO UPDATE SET
                cwd=COALESCE(excluded.cwd, sessions.cwd),
                label=COALESCE(excluded.label, sessions.label),
                registered=1,
                registered_at=COALESCE(sessions.registered_at, excluded.registered_at),
                last_seen_at=excluded.last_seen_at,
                meta=COALESCE(excluded.meta, sessions.meta)""",
        (session_id, product, runtime_session_id, cwd, label, ts, ts, meta_json),
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
# Sending (idempotent, identity + grant enforced)
# --------------------------------------------------------------------------- #
def _derive_idempotency_key(source: str, dest: str, provided: str | None) -> str:
    """The stored idempotency identity. A caller-supplied key is SCOPED to (source, dest)
    so the same natural key (e.g. a task id) reused across different recipients yields
    DISTINCT messages — a global key would let a send to C collide with an earlier send to
    B and silently drop C's message (returning B's body). No key -> unique per call."""
    if provided is not None:
        _valid_id(provided, "idempotency_key")
        return f"{source}\x1f{dest}\x1f{provided}"
    return "auto-" + secrets.token_hex(16)


def message_id_for(idempotency_key: str) -> str:
    return "msg_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]


def send_message(conn: sqlite3.Connection, source_session_id: str, dest_session_id: str,
                 body: str, *, authorship: str = "agent", kind: str = "note",
                 idempotency_key: str | None = None, ttl_seconds: int | None = None,
                 forward_of: str | None = None) -> dict:
    """Queue a message from `source_session_id` (already-AUTHENTICATED at the caller; the MCP
    layer passes the bound principal, never a tool argument). Idempotent when idempotency_key
    is supplied. Authorization is by operator GRANT, not a caller boolean:
      * authorship="user" requires the source to hold CAP_SPEAK_AS_USER,
      * kind="steer"       requires the source to hold CAP_SEND_STEER scoped to the dest.
    Enforces all privacy / loop guards."""
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
    if authorship == "user" and not has_capability(conn, source_session_id, CAP_SPEAK_AS_USER):
        raise AuthorizationError(
            "recording authorship='user' requires an operator speak-as-user grant for the sender")
    if kind == "steer" and not has_capability(conn, source_session_id, CAP_SEND_STEER,
                                              scope=dest_session_id):
        raise AuthorizationError(
            "sending a steering message requires an operator send-steer grant scoped to the destination")

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

    detail = f"kind={kind} authorship={authorship} src={source_session_id}"
    if forward_of:
        detail += f" forward_of={forward_of} depth={depth}"
    # The message row and its 'created' audit event commit together (or not at all).
    with _atomic(conn):
        cur = conn.execute(
            """INSERT OR IGNORE INTO messages(message_id, idempotency_key, source_session_id,
                    dest_session_id, dest_product, body, authorship, kind, status,
                    created_at, updated_at, expires_at, forward_depth, root_message_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, key, source_session_id, dest_session_id, dest_product, body, authorship, kind,
             STATUS_QUEUED, ts, ts, expires_at, depth, root),
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
#
# A lease is a UNIQUE-PK row keyed on the destination. Acquisition is a single-statement
# INSERT: SQLite serializes writers, so at most one contender's INSERT succeeds — that is the
# compare-and-set. The loser gets IntegrityError and only "wins" if it already holds the row
# (re-entrant same holder). No wrapping BEGIN is needed or used; the atomicity is the single
# INSERT plus the UNIQUE constraint. Expired leases are reclaimed (deleted) first so a
# crashed holder never blocks forever.
# --------------------------------------------------------------------------- #
LEASE_TTL_SECONDS = 120


def reclaim_expired_leases(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM leases WHERE expires_at < ?", (now(),))
    return cur.rowcount or 0


def acquire_lease(conn: sqlite3.Connection, dest_session_id: str, holder: str,
                  ttl_seconds: int = LEASE_TTL_SECONDS) -> bool:
    """Single-flight compare-and-set lease for a destination. Returns True if THIS holder now
    owns the lease, False if a different live holder does. Re-entrant for the same holder."""
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


def acknowledge(conn: sqlite3.Connection, message_id: str, by: str, note: str | None = None) -> dict:
    """Acknowledge a delivered message. `by` is the AUTHENTICATED recipient principal (the
    MCP layer supplies it); a principal may only acknowledge a message addressed to it."""
    row = _get_message_row(conn, message_id)
    _valid_id(by, "by")
    if row["dest_session_id"] != by:
        raise AuthorizationError(
            f"{by} may not acknowledge a message addressed to {row['dest_session_id']}")
    if row["status"] == STATUS_ACKNOWLEDGED:
        return get_message_status(conn, message_id)  # idempotent ack
    detail = f"by={by}" + (f" note={note[:80]}" if note else "")
    _transition(conn, row, STATUS_ACKNOWLEDGED,
                extra={"acknowledged_at": now(), "acknowledged_by": by}, event_detail=detail)
    return get_message_status(conn, message_id)


def cancel_message(conn: sqlite3.Connection, message_id: str, by: str,
                   reason: str | None = None) -> dict:
    """Cancel a queued/delivered message. `by` must be the authenticated sender OR recipient
    principal of the message (only a party to it may cancel it)."""
    row = _get_message_row(conn, message_id)
    _valid_id(by, "by")
    if by not in (row["source_session_id"], row["dest_session_id"]):
        raise AuthorizationError(f"{by} is not a party to message {message_id}; cannot cancel")
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
# Delivery (PULL-ONLY: an authenticated recipient draining its own inbox)
# --------------------------------------------------------------------------- #
def _steer_refusal_reason(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Return a reason a steering message may NOT be delivered yet, or None if it may. A
    steer delivers only if its SENDER held a send-steer grant at send time (enforced there)
    AND the DESTINATION currently holds an operator accept-steering grant scoped to the
    sender. This is a SOFT gate: like every delivery gate it leaves the message queued
    rather than raising, so one undeliverable steer can never poison a batch inbox pull."""
    if row["kind"] != "steer":
        return None
    if not has_capability(conn, row["dest_session_id"], CAP_ACCEPT_STEERING,
                          scope=row["source_session_id"]):
        return "destination has no operator accept-steering grant for this sender; left queued"
    return None


def deliver_one(conn: sqlite3.Connection, message_id: str, *, at_boundary: bool, holder: str,
                boundary_reason: str = "") -> dict:
    """Mark ONE queued message delivered to its authenticated recipient. Serialized by a
    destination lease. `at_boundary` is decided by the caller (the MCP layer) from
    RUNTIME-OWNED evidence — the recipient is authenticated as the destination and is
    draining its own inbox at its own turn boundary. It is never a caller-supplied string.
    Refusal never raises for the ordinary not-yet case; it records a delivery_refused event
    and returns the (still-queued) message with `delivered: False`."""
    row = _get_message_row(conn, message_id)
    if row["status"] != STATUS_QUEUED:
        raise ConflictError(f"message is {row['status']}, not queued")

    if not at_boundary:
        reason = boundary_reason or "recipient is not at a safe delivery boundary"
        _log_event(conn, message_id, "delivery_refused", reason)
        out = get_message_status(conn, message_id)
        out.update({"delivered": False, "refused_reason": reason})
        return out

    steer_reason = _steer_refusal_reason(conn, row)
    if steer_reason is not None:
        _log_event(conn, message_id, "delivery_refused", steer_reason)
        out = get_message_status(conn, message_id)
        out.update({"delivered": False, "refused_reason": steer_reason})
        return out

    if not acquire_lease(conn, row["dest_session_id"], holder):
        _log_event(conn, message_id, "delivery_refused", "destination lease held by another deliverer")
        out = get_message_status(conn, message_id)
        out.update({"delivered": False, "refused_reason": "lease_held"})
        return out
    try:
        row = _get_message_row(conn, message_id)  # re-read under lease
        if row["status"] != STATUS_QUEUED:
            out = get_message_status(conn, message_id)
            out["delivered"] = (row["status"] == STATUS_DELIVERED)
            return out
        # Honest audit: record HOW the message was delivered (who vouched for the boundary),
        # never a blanket "pulled by X" — an operator-forced drain is an operator attestation,
        # not the recipient's own pull.
        detail = f"delivered to {row['dest_session_id']} ({boundary_reason or 'boundary'})"
        _transition(conn, row, STATUS_DELIVERED,
                    extra={"delivered_at": now(), "delivered_to": row["dest_session_id"],
                           "attempts": int(row["attempts"]) + 1},
                    event_detail=detail)
    finally:
        release_lease(conn, row["dest_session_id"], holder)
    out = get_message_status(conn, message_id)
    out["delivered"] = True
    return out


def list_inbox(conn: sqlite3.Connection, dest_session_id: str, *, status: str | None = None,
               deliver: bool = False, at_boundary: bool = False, boundary_reason: str = "",
               holder: str | None = None, max_deliver: int | None = None,
               include_body: bool = True) -> list[dict]:
    """Return messages addressed to dest_session_id. With deliver=True AND at_boundary=True
    this is the safe PULL path: queued messages are transitioned to delivered, serialized per
    destination. The caller (MCP layer) is the authenticated recipient and passes at_boundary
    derived from runtime evidence — not from a tool argument. Expired messages are swept
    first so they never deliver."""
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
            res = deliver_one(conn, r["message_id"], at_boundary=at_boundary,
                              boundary_reason=boundary_reason, holder=holder)
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
    principals = conn.execute("SELECT COUNT(*) c FROM principals WHERE revoked=0").fetchone()["c"]
    grants = conn.execute("SELECT COUNT(*) c FROM auth_grants WHERE revoked=0").fetchone()["c"]
    ver = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    return {
        "ok": True,
        "db_path": str(db_path()),
        "schema_version": int(ver["version"]) if ver else None,
        "sessions": sess,
        "principals": principals,
        "active_grants": grants,
        "messages_by_status": counts,
        "active_leases": leases,
        "expired_swept": expired,
        "delivery_model": "pull-only",
    }
