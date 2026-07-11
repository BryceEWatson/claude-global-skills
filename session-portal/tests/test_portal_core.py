#!/usr/bin/env python3
"""Hermetic tests for portal_core — storage, lifecycle, validation, idempotency, leases,
crash recovery, concurrency. Every test uses a throwaway DB under a temp dir via
SESSION_PORTAL_DB, so the developer's real portal is never touched.

Run: python -m unittest discover -s session-portal/tests -p 'test_*.py'
"""
import importlib.util
import os
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
        kw.setdefault("source_session_id", "codex:A")
        kw.setdefault("dest_session_id", "claude:B")
        kw.setdefault("body", "hello")
        kw.setdefault("authorship", "user")
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
        # A message may be queued before the recipient first registers.
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
        # Only one row + one 'created' event.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"], 1)
        created = [e for e in core.message_events(self.conn, m1["message_id"]) if e["event"] == "created"]
        self.assertEqual(len(created), 1)
        # The original body wins (no silent overwrite).
        self.assertEqual(core.get_message_status(self.conn, m1["message_id"])["body"], "hello")

    def test_no_key_distinct_messages(self):
        a = self._send()
        b = self._send()
        self.assertNotEqual(a["message_id"], b["message_id"])

    def test_key_scoped_to_destination(self):
        # Regression: the SAME natural key to two DIFFERENT recipients must create two
        # distinct messages, not collide and silently drop the second (returning the first's
        # body). Idempotency identity is scoped to (source, dest, key).
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
        core.list_inbox(self.conn, "claude:B", deliver=True, boundary="stop")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "delivered")
        core.acknowledge(self.conn, m["message_id"], by="claude:B")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "acknowledged")
        events = [e["event"] for e in core.message_events(self.conn, m["message_id"])]
        self.assertEqual(events, ["created", "delivered", "acknowledged"])

    def test_cancel_queued(self):
        m = self._send(idempotency_key="k")
        core.cancel_message(self.conn, m["message_id"], by="codex:A", reason="never mind")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "cancelled")

    def test_cannot_cancel_acknowledged(self):
        m = self._send(idempotency_key="k")
        core.list_inbox(self.conn, "claude:B", deliver=True, boundary="stop")
        core.acknowledge(self.conn, m["message_id"])
        with self.assertRaises(core.ConflictError):
            core.cancel_message(self.conn, m["message_id"])

    def test_ack_is_idempotent(self):
        m = self._send(idempotency_key="k")
        core.list_inbox(self.conn, "claude:B", deliver=True, boundary="stop")
        core.acknowledge(self.conn, m["message_id"])
        core.acknowledge(self.conn, m["message_id"])  # no error, still acknowledged
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "acknowledged")

    def test_expiration(self):
        m = self._send(idempotency_key="k", ttl_seconds=10)
        self.clock.advance(11)
        self.assertEqual(core.expire_due(self.conn), 1)
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "expired")

    def test_expired_never_delivers(self):
        m = self._send(idempotency_key="k", ttl_seconds=10)
        self.clock.advance(20)
        core.list_inbox(self.conn, "claude:B", deliver=True, boundary="stop")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "expired")

    def test_illegal_transition_rejected(self):
        m = self._send(idempotency_key="k")
        with self.assertRaises(core.ConflictError):
            core.acknowledge(self.conn, m["message_id"])  # cannot ack a queued (undelivered) msg

    def test_fail_message_reaches_failed_state(self):
        m = self._send(idempotency_key="k")
        core.fail_message(self.conn, m["message_id"], "delivery error")
        got = core.get_message_status(self.conn, m["message_id"])
        self.assertEqual(got["status"], "failed")
        self.assertEqual(got["last_error"], "delivery error")
        self.assertIn("failed", [e["event"] for e in core.message_events(self.conn, m["message_id"])])


class TestAuthorizationAndLoops(CoreBase):
    def setUp(self):
        super().setUp()
        self._register_pair()

    def test_unauthorized_steer_rejected_at_send(self):
        with self.assertRaises(core.AuthorizationError):
            self._send(kind="steer", authorship="agent")

    def test_authorized_steer_needs_optin_dest_to_deliver(self):
        m = self._send(kind="steer", authorship="user", authorized=True, idempotency_key="s1")
        # Destination has NOT opted into steering -> SOFT refusal (queued), never raises.
        res = core.deliver_one(self.conn, m["message_id"], mode="pull", boundary="stop",
                               dest_state=None, holder="h")
        self.assertFalse(res["delivered"])
        self.assertIn("steering", res["refused_reason"])
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "queued")
        # Opt the destination in, then delivery succeeds.
        core.register_session(self.conn, "claude", "B", accepts_steering=True)
        res2 = core.deliver_one(self.conn, m["message_id"], mode="pull", boundary="stop",
                                dest_state=None, holder="h")
        self.assertTrue(res2["delivered"])

    def test_steer_to_non_optin_does_not_poison_inbox(self):
        # Regression: an undeliverable authorized steer queued BEFORE a note must not abort
        # the batch inbox pull — the note still delivers and no exception escapes.
        steer = self._send(kind="steer", authorship="user", authorized=True, idempotency_key="s1")
        self.clock.advance(1)  # ensure the note sorts AFTER the steer by created_at
        note = self._send(body="just a note", idempotency_key="n1")
        inbox = core.list_inbox(self.conn, "claude:B", deliver=True, boundary="stop")
        statuses = {x["message_id"]: x["status"] for x in inbox}
        self.assertEqual(statuses[steer["message_id"]], "queued")     # steer left queued
        self.assertEqual(statuses[note["message_id"]], "delivered")   # note still delivered

    def test_self_message_rejected(self):
        with self.assertRaises(core.ValidationError):
            self._send(source_session_id="claude:B", dest_session_id="claude:B")

    def test_forward_depth_cap(self):
        m = self._send(idempotency_key="root")
        f1 = core.send_message(self.conn, "claude:B", "codex:A", "fwd", authorship="user",
                               idempotency_key="f1", forward_of=m["message_id"])
        self.assertEqual(f1["forward_depth"], 1)
        with self.assertRaises(core.ValidationError):
            core.send_message(self.conn, "codex:A", "claude:B", "fwd2", authorship="user",
                              idempotency_key="f2", forward_of=f1["message_id"])

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

    def test_expired_lease_reclaimed(self):
        core.acquire_lease(self.conn, "claude:B", "dead", ttl_seconds=10)
        self.clock.advance(11)
        self.assertEqual(core.reclaim_expired_leases(self.conn), 1)
        self.assertTrue(core.acquire_lease(self.conn, "claude:B", "fresh"))

    def test_crash_leaves_deliverable_after_lease_expiry(self):
        # Simulate a deliverer that acquired the lease then "crashed" (never released).
        m = self._send(idempotency_key="k")
        core.acquire_lease(self.conn, "claude:B", "crashed", ttl_seconds=30)
        # A new deliverer cannot get the lease yet, so delivery is refused (queued).
        res = core.deliver_one(self.conn, m["message_id"], mode="pull", boundary="stop",
                               dest_state=None, holder="new")
        self.assertFalse(res["delivered"])
        self.assertEqual(res["refused_reason"], "lease_held")
        # After the lease TTL lapses, delivery recovers.
        self.clock.advance(31)
        res2 = core.deliver_one(self.conn, m["message_id"], mode="pull", boundary="stop",
                                dest_state=None, holder="new")
        self.assertTrue(res2["delivered"])

    def test_delivered_persists_until_ack(self):
        m = self._send(idempotency_key="k")
        core.list_inbox(self.conn, "claude:B", deliver=True, boundary="stop")
        # Reopen the DB (as if the process restarted) — the delivered message is durable.
        self.conn.close()
        self.conn = core.connect()
        core.init_db(self.conn)
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "delivered")


class TestConcurrency(CoreBase):
    def test_concurrent_writers(self):
        self._register_pair()
        # Real clock for threads (shared mutable Clock isn't thread-relevant here).
        core.set_clock(__import__("time").time)
        errors = []

        def worker(n):
            try:
                conn = core.connect()
                core.init_db(conn)
                for i in range(10):
                    core.send_message(conn, "codex:A", "claude:B", f"m{n}-{i}",
                                      authorship="user", idempotency_key=f"k-{n}-{i}")
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
        self.assertEqual(count, 50)  # 5 writers x 10, no lost/duplicated rows


class TestLineEndingsAndPaths(CoreBase):
    def test_crlf_body_preserved_and_valid(self):
        self._register_pair()
        m = self._send(body="win\r\nlines", idempotency_key="k")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["body"], "win\r\nlines")

    def test_db_path_under_portal_home_env(self):
        self.assertTrue(str(core.db_path()).startswith(str(self.tmp)))


if __name__ == "__main__":
    unittest.main()
