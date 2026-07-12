#!/usr/bin/env python3
"""Hermetic tests for portal_adapters — advisory classification for the PULL-ONLY portal.

The portal delivers ONLY when the authenticated recipient pulls its own inbox. The adapters
are advisory: they classify a recipient's runtime state and describe (never perform) a
resume, and they never fabricate a delivery/resume/native receipt. These tests assert that
the adapters have NO delivery side effects and that the honest pull path still works.
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
                        "SESSION_PORTAL_CODEX_NATIVE", "SESSION_PORTAL_HOST_EVIDENCE")}
        os.environ["SESSION_PORTAL_HOME"] = str(self.tmp)
        os.environ["SESSION_PORTAL_DB"] = str(self.tmp / "portal.db")
        os.environ["SESSION_PORTAL_CLAUDE_LOGS"] = str(self.claude)
        os.environ["SESSION_PORTAL_CODEX_LOGS"] = str(self.codex)
        os.environ.pop("SESSION_PORTAL_CODEX_NATIVE", None)
        os.environ.pop("SESSION_PORTAL_HOST_EVIDENCE", None)
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

    def send(self, src, dst, body="hi", **kw):
        return core.send_message(self.conn, src, dst, body, authorship="agent", **kw)


class TestAdvisoryClassification(DeliveryBase):
    def test_active_recipient_not_pull_safe(self):
        self.claude_active("B")  # fresh mtime -> active
        adv = ad.classify_deliverability("claude", "B")
        self.assertEqual(adv["state"], st.ACTIVE)
        self.assertFalse(adv["would_pull_safely"])
        self.assertTrue(adv["advisory_only"])

    def test_idle_recipient_pull_safe(self):
        log = self.claude_idle("B")
        old = log.stat().st_mtime - (st.ACTIVE_WINDOW_SECONDS + 60)
        os.utime(log, (old, old))
        adv = ad.classify_deliverability("claude", "B")
        self.assertEqual(adv["state"], st.IDLE)
        self.assertTrue(adv["would_pull_safely"])

    def test_classify_has_no_delivery_side_effect(self):
        # An advisory classification must NOT transition a queued message.
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        self.claude_idle("B")
        ad.classify_deliverability("claude", "B")
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "queued")


class TestResumePlanAdvisoryOnly(DeliveryBase):
    def test_resume_refused_when_active(self):
        self.claude_active("B")
        plan = ad.resume_plan("B")
        self.assertFalse(plan["resumable"])
        self.assertIn("not proven closed", plan["reason"])

    def test_resume_refused_when_idle_but_not_closed(self):
        log = self.claude_idle("B")
        old = log.stat().st_mtime - (st.ACTIVE_WINDOW_SECONDS + 60)
        os.utime(log, (old, old))  # idle, liveness unknown -> not proven closed
        plan = ad.resume_plan("B")
        self.assertFalse(plan["resumable"])

    def test_resume_plan_when_closed_is_advice_not_action(self):
        # Proven closed via host evidence that the process is gone.
        self.claude_idle("B")
        ev = self.tmp / "evidence.json"
        ev.write_text(json.dumps({"claude:B": False}), encoding="utf-8")
        os.environ["SESSION_PORTAL_HOST_EVIDENCE"] = str(ev)
        m = self.send("codex:A", "claude:B", idempotency_key="k")
        plan = ad.resume_plan("B")
        self.assertTrue(plan["resumable"])
        self.assertEqual(plan["suggested_command"], ["claude", "--resume", "B"])
        self.assertTrue(plan["advisory_only"])
        # Crucially: the message is untouched — advice never delivers or resumes.
        self.assertEqual(core.get_message_status(self.conn, m["message_id"])["status"], "queued")
        events = [e["event"] for e in core.message_events(self.conn, m["message_id"])]
        self.assertNotIn("resume_planned", events)
        self.assertNotIn("resume_requested", events)


class TestCodexNativeHonesty(DeliveryBase):
    def test_native_never_claims_delivery(self):
        self.assertFalse(ad.codex_native_status()["native_available"])

    def test_forcing_env_is_ignored(self):
        # The old backdoor env must no longer fabricate a native delivery.
        os.environ["SESSION_PORTAL_CODEX_NATIVE"] = "1"
        status = ad.codex_native_status()
        self.assertFalse(status["native_available"])
        self.assertTrue(status["ignored_forcing_env"])


class TestPullPathStillWorks(DeliveryBase):
    def test_both_directions_via_pull(self):
        m1 = self.send("codex:A", "claude:B", body="to claude", idempotency_key="k1")
        core.list_inbox(self.conn, "claude:B", deliver=True, at_boundary=True)
        core.acknowledge(self.conn, m1["message_id"], by="claude:B")
        m2 = self.send("claude:B", "codex:A", body="to codex", idempotency_key="k2")
        core.list_inbox(self.conn, "codex:A", deliver=True, at_boundary=True)
        core.acknowledge(self.conn, m2["message_id"], by="codex:A")
        self.assertEqual(core.get_message_status(self.conn, m1["message_id"])["status"], "acknowledged")
        self.assertEqual(core.get_message_status(self.conn, m2["message_id"])["status"], "acknowledged")


class TestCoherence(DeliveryBase):
    def test_push_deliverable_set_shape(self):
        self.assertEqual(st.PUSH_DELIVERABLE, {st.IDLE, st.WAITING})


if __name__ == "__main__":
    unittest.main()
