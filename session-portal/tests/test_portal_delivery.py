#!/usr/bin/env python3
"""Hermetic tests for portal_adapters — boundary-safe delivery.

Covers: active-session delivery refusal, idle boundary delivery, turn-boundary pickup in
both products, both message directions, the unavailable-Codex-native fallback, and the
Claude resume safeguards (never resume an active session; closed+lease required; dry-run).
Fake session logs are built under temp dirs; the DB is a throwaway.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("portal_core")
st = _load("portal_state")
ad = _load("portal_adapters")


class DeliveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.claude = self.tmp / "claude"
        self.codex = self.tmp / "codex"
        self.claude.mkdir()
        self.codex.mkdir()
        self._saved = {k: os.environ.get(k) for k in
                       ("SESSION_PORTAL_DB", "SESSION_PORTAL_HOME",
                        "SESSION_PORTAL_CLAUDE_LOGS", "SESSION_PORTAL_CODEX_LOGS",
                        "SESSION_PORTAL_CODEX_NATIVE")}
        os.environ["SESSION_PORTAL_HOME"] = str(self.tmp)
        os.environ["SESSION_PORTAL_DB"] = str(self.tmp / "portal.db")
        os.environ["SESSION_PORTAL_CLAUDE_LOGS"] = str(self.claude)
        os.environ["SESSION_PORTAL_CODEX_LOGS"] = str(self.codex)
        os.environ.pop("SESSION_PORTAL_CODEX_NATIVE", None)
        core.set_clock(time.time)
        self.conn = core.connect()
        core.init_db(self.conn)
        core.register_session(self.conn, "claude", "B", label="B")
        core.register_session(self.conn, "codex", "A", label="A")

    def tearDown(self):
        self.conn.close()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def claude_log(self, sid, rows):
        d = self.claude / "proj"
        d.mkdir(exist_ok=True)
        p = d / f"{sid}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    def codex_log(self, tid, rows):
        d = self.codex / "2026" / "07" / "11"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"rollout-2026-07-11-{tid}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    def claude_idle(self, sid="B"):
        return self.claude_log(sid, [
            {"message": {"role": "assistant", "stop_reason": "tool_use",
                         "content": [{"type": "tool_use", "name": "Bash"}]}},
            {"message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}]}}])

    def claude_active(self, sid="B"):
        return self.claude_log(sid, [
            {"message": {"role": "assistant", "stop_reason": "tool_use",
                         "content": [{"type": "tool_use", "name": "Bash"}]}}])

    def codex_idle(self, tid="A"):
        return self.codex_log(tid, [
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "done"}}])

    def send(self, src, dst, body="hi", **kw):
        return core.send_message(self.conn, src, dst, body, authorship="user", **kw)


class TestPushDelivery(DeliveryBase):
    def test_active_session_delivery_refused(self):
        self.claude_active("B")  # fresh mtime -> active
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        res = ad.push_deliver(self.conn, m["message_id"], "claude", "B")
        self.assertFalse(res["delivered"])
        self.assertEqual(res["dest_state"], st.ACTIVE)
        # Still queued (durable), and a refusal was audited.
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "queued")
        events = [e["event"] for e in core.message_events(self.conn, m["message_id"])]
        self.assertIn("delivery_refused", events)

    def test_idle_boundary_delivery(self):
        log = self.claude_idle("B")
        # Age the log past the active window so state is idle, not active.
        old = log.stat().st_mtime - (st.ACTIVE_WINDOW_SECONDS + 60)
        os.utime(log, (old, old))
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        res = ad.push_deliver(self.conn, m["message_id"], "claude", "B")
        self.assertTrue(res["delivered"])
        self.assertEqual(res["dest_state"], st.IDLE)
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "delivered")

    def test_unknown_state_not_pushed(self):
        log = self.claude_active("B")  # working, no completion marker
        old = log.stat().st_mtime - (st.ACTIVE_WINDOW_SECONDS + 60)
        os.utime(log, (old, old))  # quiet but no end_turn -> unknown
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        res = ad.push_deliver(self.conn, m["message_id"], "claude", "B")
        self.assertFalse(res["delivered"])
        self.assertEqual(res["dest_state"], st.UNKNOWN)


class TestPullPickup(DeliveryBase):
    def test_pull_delivers_at_boundary(self):
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        inbox = core.list_inbox(self.conn, "claude:B", deliver=True, boundary="prompt_submit")
        self.assertEqual(inbox[0]["status"], "delivered")

    def test_pull_without_boundary_refused(self):
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        res = core.deliver_one(self.conn, m["message_id"], mode="pull", boundary=None,
                               dest_state=None, holder="h")
        self.assertFalse(res["delivered"])
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "queued")

    def test_both_directions(self):
        # codex -> claude
        m1 = self.send("codex:A", "claude:B", body="to claude", idempotency_key="k1")
        core.list_inbox(self.conn, "claude:B", deliver=True, boundary="stop")
        core.acknowledge(self.conn, m1["message_id"], by="claude:B")
        # claude -> codex
        m2 = self.send("claude:B", "codex:A", body="to codex", idempotency_key="k2")
        core.list_inbox(self.conn, "codex:A", deliver=True, boundary="session_start")
        core.acknowledge(self.conn, m2["message_id"], by="codex:A")
        self.assertEqual(core.get_message_status(self.conn, m1["message_id"])["status"], "acknowledged")
        self.assertEqual(core.get_message_status(self.conn, m2["message_id"])["status"], "acknowledged")


class TestCodexFallback(DeliveryBase):
    def test_native_unavailable_by_default(self):
        self.assertFalse(ad.codex_native_status()["native_available"])

    def test_codex_deliver_falls_back_to_queue_without_boundary(self):
        m = self.send("claude:B", "codex:A", idempotency_key="k")
        res = ad.codex_deliver(self.conn, m["message_id"], "A", boundary=None)
        self.assertFalse(res["delivered"])
        self.assertEqual(res["via"], "queue")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "queued")

    def test_codex_deliver_at_boundary(self):
        m = self.send("claude:B", "codex:A", idempotency_key="k")
        res = ad.codex_deliver(self.conn, m["message_id"], "A", boundary="stop")
        self.assertTrue(res["delivered"])
        self.assertEqual(res["via"], "queue_boundary_pull")

    def test_codex_native_when_surfaced(self):
        os.environ["SESSION_PORTAL_CODEX_NATIVE"] = "1"
        self.assertTrue(ad.codex_native_status()["native_available"])
        m = self.send("claude:B", "codex:A", idempotency_key="k")
        res = ad.codex_deliver(self.conn, m["message_id"], "A")
        self.assertTrue(res["delivered"])
        self.assertEqual(res["via"], "native_task_message")


class TestClaudeResumeSafeguards(DeliveryBase):
    def test_resume_refused_when_active(self):
        self.claude_active("B")  # active session must never be resumed
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        res = ad.claude_resume_plan(self.conn, m["message_id"], "B")
        self.assertFalse(res.get("resumed"))
        self.assertIn("not closed", res["refused_reason"])
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "queued")

    def test_resume_refused_when_idle_but_not_closed(self):
        log = self.claude_idle("B")
        old = log.stat().st_mtime - (st.ACTIVE_WINDOW_SECONDS + 60)
        os.utime(log, (old, old))  # idle, alive -> not a resume candidate
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        res = ad.claude_resume_plan(self.conn, m["message_id"], "B")
        self.assertFalse(res.get("resumed"))

    def test_resume_dry_run_plan_when_closed(self):
        self.claude_idle("B")  # completed marker + process_alive=False -> completed
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        res = ad.claude_resume_plan(self.conn, m["message_id"], "B", process_alive=False)
        self.assertFalse(res["delivered"])  # dry-run: nothing spawned, message stays queued
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["resume_command"], ["claude", "--resume", "B"])
        events = [e["event"] for e in core.message_events(self.conn, m["message_id"])]
        self.assertIn("resume_planned", events)


if __name__ == "__main__":
    unittest.main()
