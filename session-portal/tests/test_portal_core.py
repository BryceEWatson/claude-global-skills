#!/usr/bin/env python3
"""Hermetic tests for portal_core — storage, migrations, principals, grants, lifecycle,
validation, idempotency, leases (CAS), crash recovery, concurrency. Every test uses a
throwaway DB under a temp dir via SESSION_PORTAL_DB, so the developer's real portal is
never touched.

Run: python -m unittest discover -s session-portal/tests -p 'test_*.py'
"""
import importlib.util
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name] = mod  # so cross-imports (portal_adapters -> portal_core) resolve
    spec.loader.exec_module(mod)
    return mod


core = _load("portal_core")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class CoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {k: os.environ.get(k) for k in
                       ("SESSION_PORTAL_DB", "SESSION_PORTAL_HOME")}
        os.environ["SESSION_PORTAL_HOME"] = str(self.tmp)
        os.environ["SESSION_PORTAL_DB"] = str(self.tmp / "portal.db")
        self.clock = Clock()
        core.set_clock(self.clock)
        self.conn = core.connect()
        core.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        core.set_clock(__import__("time").time)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _register_pair(self):
        core.register_session(self.conn, "claude", "B", label="claude-B")
        core.register_session(self.conn, "codex", "A", label="codex-A")

    def _send(self, **kw):
        # Default sender identity codex:A, recipient claude:B, agent authorship (needs no grant).
        kw.setdefault("source_session_id", "codex:A")
        kw.setdefault("dest_session_id", "claude:B")
        kw.setdefault("body", "hello")
        kw.setdefault("authorship", "agent")
        return core.send_message(self.conn, kw.pop("source_session_id"),
                                 kw.pop("dest_session_id"), kw.pop("body"), **kw)


class TestStorageAndMigrations(CoreBase):
    def test_init_is_idempotent(self):
        core.init_db(self.conn)
        core.init_db(self.conn)  # second call must not error or duplicate version rows
        rows = self.conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()
        self.assertEqual(rows["c"], 1)
        self.assertEqual(self.conn.execute("SELECT version FROM schema_version").fetchone()["version"],
                         core.SCHEMA_VERSION)

    def test_wal_and_foreign_keys_on(self):
        jm = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(jm.lower(), "wal")
        fk = self.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(fk, 1)

    def test_schema_too_new_raises(self):
        self.conn.execute("UPDATE schema_version SET version=?", (core.SCHEMA_VERSION + 5,))
        with self.assertRaises(core.PortalError):
            core.init_db(self.conn)

    def test_db_reopen_preserves_data(self):
        self._register_pair()
        m = self._send(idempotency_key="k1")
        self.conn.close()
        conn2 = core.connect()
        core.init_db(conn2)
        got = core.get_message_status(conn2, m["message_id"])
        self.assertEqual(got["body"], "hello")
        conn2.close()

    def test_v1_to_v2_migration(self):
        # Build a minimal v1 database (no principals/auth_grants, messages lacks the new
        # delivered_to/acknowledged_by columns), then let init_db migrate it to v2.
        p = self.tmp / "legacy.db"
        raw = sqlite3.connect(str(p))
        raw.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version(version) VALUES (1);
            CREATE TABLE messages (
                message_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                source_session_id TEXT NOT NULL, dest_session_id TEXT NOT NULL,
                dest_product TEXT NOT NULL, body TEXT NOT NULL, authorship TEXT NOT NULL,
                kind TEXT NOT NULL, authorized INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued', created_at REAL NOT NULL,
                updated_at REAL NOT NULL, expires_at REAL, delivered_at REAL,
                acknowledged_at REAL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                forward_depth INTEGER NOT NULL DEFAULT 0, root_message_id TEXT);
            INSERT INTO messages(message_id, idempotency_key, source_session_id, dest_session_id,
                dest_product, body, authorship, kind, status, created_at, updated_at)
              VALUES ('msg_legacy','ik','codex:A','claude:B','claude','old body','agent','note',
                      'queued', 1.0, 1.0);
            """)
        raw.commit()
        raw.close()

        conn = core.connect(p)
        core.init_db(conn)  # should migrate 1 -> 2
        self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()["version"], 2)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        self.assertIn("delivered_to", cols)
        self.assertIn("acknowledged_by", cols)
        # New tables exist and the legacy row survived.
        conn.execute("SELECT 1 FROM principals LIMIT 1")
        conn.execute("SELECT 1 FROM auth_grants LIMIT 1")
        self.assertEqual(core.get_message_status(conn, "msg_legacy")["body"], "old body")
        conn.close()


class TestPrincipals(CoreBase):
    def test_issue_resolve_roundtrip(self):
        pr = core.issue_principal(self.conn, "claude", "B", label="demo")
        self.assertEqual(pr["session_id"], "claude:B")
        self.assertIn("token", pr)
        self.assertNotIn("token_hash", pr)  # never leak the hash
        self.assertEqual(core.resolve_principal(self.conn, pr["token"]), "claude:B")

    def test_only_hash_is_stored(self):
        pr = core.issue_principal(self.conn, "claude", "B")
        stored = self.conn.execute("SELECT token_hash FROM principals WHERE session_id=?",
                                   ("claude:B",)).fetchone()["token_hash"]
        self.assertNotEqual(stored, pr["token"])
        self.assertEqual(len(stored), 64)  # sha256 hex

    def test_unknown_token_rejected(self):
        with self.assertRaises(core.AuthorizationError):
            core.resolve_principal(self.conn, "not-a-real-token-0000000000")

    def test_expired_token_rejected(self):
        pr = core.issue_principal(self.conn, "claude", "B", ttl_seconds=10)
        self.clock.advance(11)
        with self.assertRaises(core.AuthorizationError):
            core.resolve_principal(self.conn, pr["token"])

    def test_revoked_token_rejected(self):
        pr = core.issue_principal(self.conn, "claude", "B")
        core.revoke_principal(self.conn, "claude:B")
        with self.assertRaises(core.AuthorizationError):
            core.resolve_principal(self.conn, pr["token"])

    def test_rotation_invalidates_old_token(self):
        old = core.issue_principal(self.conn, "claude", "B")
        new = core.issue_principal(self.conn, "claude", "B")  # rotate
        self.assertNotEqual(old["token"], new["token"])
        self.assertEqual(core.resolve_principal(self.conn, new["token"]), "claude:B")
        with self.assertRaises(core.AuthorizationError):
            core.resolve_principal(self.conn, old["token"])

    def test_negative_ttl_rejected(self):
        with self.assertRaises(core.ValidationError):
            core.issue_principal(self.conn, "claude", "B", ttl_seconds=-3600)

    def test_corrupt_salt_is_hard_error(self):
        # A short/garbage salt must fail loudly, not silently hash tokens with a weak salt.
        core.issue_principal(self.conn, "claude", "B")  # creates a valid salt
        (self.tmp / ".token-salt").write_bytes(b"short")
        with self.assertRaises(core.PortalError):
            core.hash_token("anything")

    def test_concurrent_salt_creation_is_single_winner(self):
        # Exclusive create: many threads racing to first-create the salt must all end up with
        # the SAME salt (else tokens minted under one salt would orphan under another).
        salt_file = self.tmp / ".token-salt"
        if salt_file.exists():
            salt_file.unlink()
        results, lock = [], threading.Lock()

        def hash_it():
            h = core.hash_token("same-input")
            with lock:
                results.append(h)

        threads = [threading.Thread(target=hash_it) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(results)), 1, "salt race produced divergent salts")
        self.assertEqual(len(salt_file.read_bytes()), 32)


class TestGrants(CoreBase):
    def test_grant_and_check(self):
        self.assertFalse(core.has_capability(self.conn, "codex:A", core.CAP_SEND_STEER))
        core.grant_capability(self.conn, "codex:A", core.CAP_SEND_STEER, scope="claude:B")
        self.assertTrue(core.has_capability(self.conn, "codex:A", core.CAP_SEND_STEER, scope="claude:B"))
        # Scope is enforced: a grant for claude:B does not authorize sending to codex:C.
        self.assertFalse(core.has_capability(self.conn, "codex:A", core.CAP_SEND_STEER, scope="codex:C"))

    def test_wildcard_scope(self):
        core.grant_capability(self.conn, "codex:A", core.CAP_SPEAK_AS_USER, scope="*")
        self.assertTrue(core.has_capability(self.conn, "codex:A", core.CAP_SPEAK_AS_USER, scope="anything:x"))

    def test_grant_expiry(self):
        core.grant_capability(self.conn, "codex:A", core.CAP_SEND_STEER, scope="claude:B", ttl_seconds=10)
        self.assertTrue(core.has_capability(self.conn, "codex:A", core.CAP_SEND_STEER, scope="claude:B"))
        self.clock.advance(11)
        self.assertFalse(core.has_capability(self.conn, "codex:A", core.CAP_SEND_STEER, scope="claude:B"))

    def test_grant_revoke(self):
        g = core.grant_capability(self.conn, "codex:A", core.CAP_ACCEPT_STEERING)
        self.assertTrue(core.has_capability(self.conn, "codex:A", core.CAP_ACCEPT_STEERING))
        core.revoke_grant(self.conn, g["id"])
        self.assertFalse(core.has_capability(self.conn, "codex:A", core.CAP_ACCEPT_STEERING))

    def test_unknown_capability_rejected(self):
        with self.assertRaises(core.ValidationError):
            core.grant_capability(self.conn, "codex:A", "make-coffee")

    def test_speak_as_user_must_be_global_scope(self):
        # A narrow scope on a global capability would be silently ignored — reject it.
        with self.assertRaises(core.ValidationError):
            core.grant_capability(self.conn, "codex:A", core.CAP_SPEAK_AS_USER, scope="claude:B")
        core.grant_capability(self.conn, "codex:A", core.CAP_SPEAK_AS_USER, scope="*")  # ok

    def test_grant_negative_ttl_rejected(self):
        with self.assertRaises(core.ValidationError):
            core.grant_capability(self.conn, "codex:A", core.CAP_SEND_STEER, ttl_seconds=-1)


class TestSessions(CoreBase):
    def test_register_and_get(self):
        s = core.register_session(self.conn, "claude", "B", cwd="/repo", label="x")
        self.assertEqual(s["session_id"], "claude:B")
        self.assertEqual(s["registered"], 1)
        self.assertEqual(core.get_session(self.conn, "claude:B")["label"], "x")

    def test_multiple_simultaneous_sessions(self):
        for p, sid in [("claude", "B1"), ("claude", "B2"), ("codex", "A1"), ("codex", "A2")]:
            core.register_session(self.conn, p, sid)
        self.assertEqual(len(core.list_sessions(self.conn)), 4)
        self.assertEqual(len(core.list_sessions(self.conn, product="claude")), 2)
        self.assertEqual(len(core.list_sessions(self.conn, product="codex")), 2)

    def test_unknown_product_rejected(self):
        with self.assertRaises(core.ValidationError):
            core.register_session(self.conn, "gemini", "X")

    def test_get_missing_session_raises(self):
        with self.assertRaises(core.NotFoundError):
            core.get_session(self.conn, "claude:nope")

    def test_send_autocreates_placeholder_dest(self):
        m = self._send(dest_session_id="claude:LATER", idempotency_key="k")
        dest = core.get_session(self.conn, "claude:LATER")
        self.assertEqual(dest["registered"], 0)
        self.assertEqual(m["status"], "queued")


class TestValidation(CoreBase):
    def test_identifier_rejection(self):
        for bad in ("claude:bad id", "claude:" + "x" * 200, "no-colon", "weird:$$$"):
            with self.assertRaises(core.ValidationError):
                self._send(dest_session_id=bad)

    def test_body_size_limit(self):
        with self.assertRaises(core.ValidationError):
            self._send(body="x" * (core.MAX_BODY_BYTES + 1))

    def test_body_empty_rejected(self):
        with self.assertRaises(core.ValidationError):
            self._send(body="")

    def test_control_chars_rejected(self):
        with self.assertRaises(core.ValidationError):
            self._send(body="line\x00null")

    def test_valid_newline_tab_allowed(self):
        m = self._send(body="line1\nline2\ttabbed", idempotency_key="ok")
        self.assertEqual(m["status"], "queued")

    def test_secret_rejected(self):
        for secret in ("token: sk-ant-abcd1234efgh", "ghp_" + "a" * 30,
                       "api_key = supersecretvalue123"):
            with self.assertRaises(core.ValidationError):
                self._send(body=f"here {secret}")

    def test_prohibited_fields_helper(self):
        with self.assertRaises(core.ValidationError):
            core.validate_no_prohibited_fields({"body": "x", "reasoning": "hidden"})
        with self.assertRaises(core.ValidationError):
            core.validate_no_prohibited_fields({"token": "abc"})
        core.validate_no_prohibited_fields({"body": "x", "authorship": "user"})  # ok

    def test_invalid_authorship_kind(self):
        with self.assertRaises(core.ValidationError):
            self._send(authorship="robot")
        with self.assertRaises(core.ValidationError):
            self._send(kind="command")


class TestIdempotency(CoreBase):
    def test_same_key_is_single_message(self):
        m1 = self._send(idempotency_key="dup")
        m2 = self._send(idempotency_key="dup", body="different text")
        self.assertEqual(m1["message_id"], m2["message_id"])
        self.assertTrue(m2.get("idempotent_duplicate"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"], 1)
        created = [e for e in core.message_events(self.conn, m1["message_id"]) if e["event"] == "created"]
        self.assertEqual(len(created), 1)
        self.assertEqual(core.get_message_status(self.conn, m1["message_id"])["body"], "hello")

    def test_no_key_distinct_messages(self):
        a = self._send()
        b = self._send()
        self.assertNotEqual(a["message_id"], b["message_id"])

    def test_key_scoped_to_destination(self):
        m_b = self._send(dest_session_id="claude:B", body="to B", idempotency_key="task-42")
        m_c = self._send(dest_session_id="codex:C", body="to C", idempotency_key="task-42")
        self.assertNotEqual(m_b["message_id"], m_c["message_id"])
        self.assertFalse(m_c.get("idempotent_duplicate"))
        self.assertEqual(core.get_message_status(self.conn, m_c["message_id"])["body"], "to C")
        self.assertEqual(core.get_message_status(self.conn, m_b["message_id"])["body"], "to B")


class TestLifecycle(CoreBase):
    def setUp(self):
        super().setUp()
        self._register_pair()

    def test_queued_delivered_acknowledged(self):
        m = self._send(idempotency_key="k")
        self.assertEqual(m["status"], "queued")
        core.list_inbox(self.conn, "claude:B", deliver=True, at_boundary=True)
        got = core.get_message_status(self.conn, m["message_id"])
        self.assertEqual(got["status"], "delivered")
        self.assertEqual(got["delivered_to"], "claude:B")
        core.acknowledge(self.conn, m["message_id"], by="claude:B")
        got = core.get_message_status(self.conn, m["message_id"])
        self.assertEqual(got["status"], "acknowledged")
        self.assertEqual(got["acknowledged_by"], "claude:B")
        events = [e["event"] for e in core.message_events(self.conn, m["message_id"])]
        self.assertEqual(events, ["created", "delivered", "acknowledged"])

    def test_ack_by_wrong_session_rejected(self):
        m = self._send(idempotency_key="k")
        core.list_inbox(self.conn, "claude:B", deliver=True, at_boundary=True)
        with self.assertRaises(core.AuthorizationError):
            core.acknowledge(self.conn, m["message_id"], by="codex:A")  # not the recipient

    def test_cancel_by_party(self):
        m = self._send(idempotency_key="k")
        core.cancel_message(self.conn, m["message_id"], by="codex:A", reason="never mind")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "cancelled")

    def test_cancel_by_non_party_rejected(self):
        m = self._send(idempotency_key="k")
        with self.assertRaises(core.AuthorizationError):
            core.cancel_message(self.conn, m["message_id"], by="codex:STRANGER")

    def test_cannot_cancel_acknowledged(self):
        m = self._send(idempotency_key="k")
        core.list_inbox(self.conn, "claude:B", deliver=True, at_boundary=True)
        core.acknowledge(self.conn, m["message_id"], by="claude:B")
        with self.assertRaises(core.ConflictError):
            core.cancel_message(self.conn, m["message_id"], by="claude:B")

    def test_ack_is_idempotent(self):
        m = self._send(idempotency_key="k")
        core.list_inbox(self.conn, "claude:B", deliver=True, at_boundary=True)
        core.acknowledge(self.conn, m["message_id"], by="claude:B")
        core.acknowledge(self.conn, m["message_id"], by="claude:B")  # no error
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "acknowledged")

    def test_expiration(self):
        m = self._send(idempotency_key="k", ttl_seconds=10)
        self.clock.advance(11)
        self.assertEqual(core.expire_due(self.conn), 1)
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "expired")

    def test_expired_never_delivers(self):
        m = self._send(idempotency_key="k", ttl_seconds=10)
        self.clock.advance(20)
        core.list_inbox(self.conn, "claude:B", deliver=True, at_boundary=True)
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "expired")

    def test_illegal_transition_rejected(self):
        m = self._send(idempotency_key="k")
        with self.assertRaises(core.ConflictError):
            core.acknowledge(self.conn, m["message_id"], by="claude:B")  # cannot ack a queued msg

    def test_fail_message_reaches_failed_state(self):
        m = self._send(idempotency_key="k")
        core.fail_message(self.conn, m["message_id"], "delivery error")
        got = core.get_message_status(self.conn, m["message_id"])
        self.assertEqual(got["status"], "failed")
        self.assertEqual(got["last_error"], "delivery error")
        self.assertIn("failed", [e["event"] for e in core.message_events(self.conn, m["message_id"])])


class TestPullDeliveryGating(CoreBase):
    def setUp(self):
        super().setUp()
        self._register_pair()

    def test_pull_without_boundary_refused(self):
        m = self._send(idempotency_key="k")
        res = core.deliver_one(self.conn, m["message_id"], at_boundary=False, holder="h")
        self.assertFalse(res["delivered"])
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "queued")

    def test_pull_at_boundary_delivers(self):
        m = self._send(idempotency_key="k")
        res = core.deliver_one(self.conn, m["message_id"], at_boundary=True, holder="h")
        self.assertTrue(res["delivered"])
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "delivered")

    def test_delivery_audit_reflects_trigger_not_a_false_pull(self):
        # An operator-vouched drain must NOT record "pulled by <dest>" (the recipient never
        # pulled); the audit detail carries the actual boundary reason.
        m = self._send(idempotency_key="k")
        core.deliver_one(self.conn, m["message_id"], at_boundary=True, holder="h",
                         boundary_reason="operator-asserted")
        detail = [e["detail"] for e in core.message_events(self.conn, m["message_id"])
                  if e["event"] == "delivered"][0]
        self.assertNotIn("pulled by", detail)
        self.assertIn("operator-asserted", detail)


class TestSteeringGrants(CoreBase):
    def setUp(self):
        super().setUp()
        self._register_pair()

    def test_unauthorized_steer_rejected_at_send(self):
        # No send-steer grant on the source -> refused at send.
        with self.assertRaises(core.AuthorizationError):
            self._send(kind="steer", authorship="agent")

    def test_steer_needs_send_grant_then_accept_grant(self):
        core.grant_capability(self.conn, "codex:A", core.CAP_SEND_STEER, scope="claude:B")
        m = self._send(kind="steer", authorship="agent", idempotency_key="s1")
        # Destination has NO accept-steering grant -> SOFT refusal (queued), never raises.
        res = core.deliver_one(self.conn, m["message_id"], at_boundary=True, holder="h")
        self.assertFalse(res["delivered"])
        self.assertIn("steering", res["refused_reason"])
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "queued")
        # Grant the destination accept-steering scoped to the sender, then delivery succeeds.
        core.grant_capability(self.conn, "claude:B", core.CAP_ACCEPT_STEERING, scope="codex:A")
        res2 = core.deliver_one(self.conn, m["message_id"], at_boundary=True, holder="h")
        self.assertTrue(res2["delivered"])

    def test_steer_to_non_optin_does_not_poison_inbox(self):
        core.grant_capability(self.conn, "codex:A", core.CAP_SEND_STEER, scope="claude:B")
        steer = self._send(kind="steer", authorship="agent", idempotency_key="s1")
        self.clock.advance(1)  # ensure the note sorts AFTER the steer by created_at
        note = self._send(body="just a note", idempotency_key="n1")
        inbox = core.list_inbox(self.conn, "claude:B", deliver=True, at_boundary=True)
        statuses = {x["message_id"]: x["status"] for x in inbox}
        self.assertEqual(statuses[steer["message_id"]], "queued")     # steer left queued
        self.assertEqual(statuses[note["message_id"]], "delivered")   # note still delivered

    def test_user_authorship_requires_grant(self):
        with self.assertRaises(core.AuthorizationError):
            self._send(authorship="user", idempotency_key="u1")
        core.grant_capability(self.conn, "codex:A", core.CAP_SPEAK_AS_USER)
        m = self._send(authorship="user", idempotency_key="u2")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["authorship"], "user")


class TestLoops(CoreBase):
    def setUp(self):
        super().setUp()
        self._register_pair()

    def test_self_message_rejected(self):
        with self.assertRaises(core.ValidationError):
            self._send(source_session_id="claude:B", dest_session_id="claude:B")

    def test_forward_depth_cap(self):
        # Forward through third parties so this isolates the DEPTH cap (a reversed pair would
        # trip the separate ping-pong guard instead).
        m = self._send(idempotency_key="root")                          # codex:A -> claude:B
        f1 = core.send_message(self.conn, "claude:B", "codex:C", "fwd", authorship="agent",
                               idempotency_key="f1", forward_of=m["message_id"])  # depth 1
        self.assertEqual(f1["forward_depth"], 1)
        with self.assertRaises(core.ValidationError):
            core.send_message(self.conn, "codex:C", "claude:D", "fwd2", authorship="agent",
                              idempotency_key="f2", forward_of=f1["message_id"])  # depth 2 > cap

    def test_pingpong_reversed_agent_forward_rejected(self):
        m = self._send(authorship="agent", idempotency_key="root")
        with self.assertRaises(core.ValidationError):
            core.send_message(self.conn, "claude:B", "codex:A", "back", authorship="agent",
                              idempotency_key="pp", forward_of=m["message_id"])


class TestLeasesAndCrashRecovery(CoreBase):
    def setUp(self):
        super().setUp()
        self._register_pair()

    def test_lease_single_flight(self):
        self.assertTrue(core.acquire_lease(self.conn, "claude:B", "holder1"))
        self.assertFalse(core.acquire_lease(self.conn, "claude:B", "holder2"))
        self.assertTrue(core.acquire_lease(self.conn, "claude:B", "holder1"))  # re-entrant same holder
        core.release_lease(self.conn, "claude:B", "holder1")
        self.assertTrue(core.acquire_lease(self.conn, "claude:B", "holder2"))

    def test_lease_cas_two_holders_exactly_one_wins(self):
        # Compare-and-set under real thread contention: many holders race for one dest; the
        # UNIQUE-PK single-statement INSERT must let EXACTLY ONE win, and the DB must end with
        # exactly one lease row owned by that winner.
        core.set_clock(__import__("time").time)  # real clock for threads
        winners, lock = [], threading.Lock()
        barrier = threading.Barrier(8)

        def contend(n):
            conn = core.connect()
            core.init_db(conn)
            barrier.wait()
            if core.acquire_lease(conn, "codex:A", f"h{n}", ttl_seconds=300):
                with lock:
                    winners.append(f"h{n}")
            conn.close()

        threads = [threading.Thread(target=contend, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1, f"expected one CAS winner, got {winners}")
        rows = self.conn.execute("SELECT holder FROM leases WHERE dest_session_id=?", ("codex:A",)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["holder"], winners[0])

    def test_expired_lease_reclaimed(self):
        core.acquire_lease(self.conn, "claude:B", "dead", ttl_seconds=10)
        self.clock.advance(11)
        self.assertEqual(core.reclaim_expired_leases(self.conn), 1)
        self.assertTrue(core.acquire_lease(self.conn, "claude:B", "fresh"))

    def test_stale_holder_release_does_not_steal_new_lease(self):
        # A crashed holder A's lease expires; B takes it. A's late release_lease(A) must NOT
        # delete B's lease (release is holder-scoped).
        core.acquire_lease(self.conn, "claude:B", "A", ttl_seconds=10)
        self.clock.advance(11)
        self.assertTrue(core.acquire_lease(self.conn, "claude:B", "B"))  # reclaims A, takes it
        core.release_lease(self.conn, "claude:B", "A")  # A's stale release
        row = self.conn.execute("SELECT holder FROM leases WHERE dest_session_id=?", ("claude:B",)).fetchone()
        self.assertEqual(row["holder"], "B")  # B still holds it

    def test_crash_leaves_deliverable_after_lease_expiry(self):
        m = self._send(idempotency_key="k")
        core.acquire_lease(self.conn, "claude:B", "crashed", ttl_seconds=30)
        res = core.deliver_one(self.conn, m["message_id"], at_boundary=True, holder="new")
        self.assertFalse(res["delivered"])
        self.assertEqual(res["refused_reason"], "lease_held")
        self.clock.advance(31)
        res2 = core.deliver_one(self.conn, m["message_id"], at_boundary=True, holder="new")
        self.assertTrue(res2["delivered"])

    def test_delivered_persists_until_ack(self):
        m = self._send(idempotency_key="k")
        core.list_inbox(self.conn, "claude:B", deliver=True, at_boundary=True)
        self.conn.close()
        self.conn = core.connect()
        core.init_db(self.conn)
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "delivered")


class TestConcurrency(CoreBase):
    def test_concurrent_writers(self):
        self._register_pair()
        core.set_clock(__import__("time").time)
        errors = []

        def worker(n):
            try:
                conn = core.connect()
                core.init_db(conn)
                for i in range(10):
                    core.send_message(conn, "codex:A", "claude:B", f"m{n}-{i}",
                                      authorship="agent", idempotency_key=f"k-{n}-{i}")
                conn.close()
            except Exception as e:  # pragma: no cover - surfaced via assert below
                errors.append(repr(e))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], errors)
        count = self.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        self.assertEqual(count, 50)


class TestLineEndingsAndPaths(CoreBase):
    def test_crlf_body_preserved_and_valid(self):
        self._register_pair()
        m = self._send(body="win\r\nlines", idempotency_key="k")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["body"], "win\r\nlines")

    def test_db_path_under_portal_home_env(self):
        self.assertTrue(str(core.db_path()).startswith(str(self.tmp)))


if __name__ == "__main__":
    unittest.main()
