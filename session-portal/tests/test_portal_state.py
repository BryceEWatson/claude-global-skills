#!/usr/bin/env python3
"""Hermetic tests for portal_state — conservative, read-only session-state detection.

Builds throwaway Claude/Codex JSONL logs under temp dirs (via SESSION_PORTAL_CLAUDE_LOGS
/ SESSION_PORTAL_CODEX_LOGS) and asserts the classifier maps evidence to the right state.
The load-bearing rule: a quiet transcript with no completion marker is `unknown`, never
`idle`. The classifier only ever READS these files; tests confirm they are unchanged.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load("portal_state")


class StateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.claude = self.tmp / "claude"
        self.codex = self.tmp / "codex"
        self.claude.mkdir()
        self.codex.mkdir()
        self._saved = {k: os.environ.get(k) for k in
                       ("SESSION_PORTAL_CLAUDE_LOGS", "SESSION_PORTAL_CODEX_LOGS")}
        os.environ["SESSION_PORTAL_CLAUDE_LOGS"] = str(self.claude)
        os.environ["SESSION_PORTAL_CODEX_LOGS"] = str(self.codex)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _claude_log(self, sid, rows):
        d = self.claude / "proj"
        d.mkdir(exist_ok=True)
        p = d / f"{sid}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    def _codex_log(self, tid, rows):
        d = self.codex / "2026" / "07" / "11"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"rollout-2026-07-11-{tid}.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return p

    def _at(self, log, age):
        return log.stat().st_mtime + age


class TestClaudeStates(StateBase):
    def _end_turn(self):
        return {"sessionId": "b", "message": {"role": "assistant", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "all done"}]}}

    def _working(self):
        return {"sessionId": "b", "message": {"role": "assistant", "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "Bash"}]}}

    def test_unavailable_when_no_log(self):
        self.assertEqual(st.classify("claude", "ghost", now=1.0)["state"], st.UNAVAILABLE)

    def test_active_recent_working(self):
        log = self._claude_log("b", [self._working()])
        self.assertEqual(st.classify("claude", "b", now=self._at(log, 5))["state"], st.ACTIVE)

    def test_idle_turn_complete_quiet(self):
        log = self._claude_log("b", [self._working(), self._end_turn()])
        self.assertEqual(st.classify("claude", "b", now=self._at(log, 300))["state"], st.IDLE)

    def test_unknown_quiet_no_completion(self):
        # Working, then went quiet, but NO end_turn: idleness is NOT proven.
        log = self._claude_log("b", [self._working()])
        self.assertEqual(st.classify("claude", "b", now=self._at(log, 300))["state"], st.UNKNOWN)

    def test_waiting_for_user_on_input_request(self):
        rows = [self._working(),
                {"sessionId": "b", "message": {"role": "assistant", "stop_reason": "tool_use",
                 "content": [{"type": "tool_use", "name": "AskUserQuestion"}]}}]
        log = self._claude_log("b", rows)
        self.assertEqual(st.classify("claude", "b", now=self._at(log, 10))["state"], st.WAITING)

    def test_completed_when_process_gone(self):
        log = self._claude_log("b", [self._end_turn()])
        out = st.classify("claude", "b", process_alive=False, now=self._at(log, 300))
        self.assertEqual(out["state"], st.COMPLETED)

    def test_stale_when_ancient(self):
        log = self._claude_log("b", [self._working()])
        out = st.classify("claude", "b", now=self._at(log, st.STALE_WINDOW_SECONDS + 100))
        self.assertEqual(out["state"], st.STALE)

    def test_read_only_does_not_modify_log(self):
        log = self._claude_log("b", [self._end_turn()])
        before = log.read_bytes()
        mtime = log.stat().st_mtime
        st.classify("claude", "b", now=self._at(log, 50))
        self.assertEqual(log.read_bytes(), before)
        self.assertEqual(log.stat().st_mtime, mtime)

    def test_reengaged_after_completion_is_not_idle(self):
        # Regression: a stale end_turn followed by a FRESH user prompt must NOT read as
        # idle — the user just re-engaged and the agent is active.
        rows = [self._working(), self._end_turn(),
                {"sessionId": "b", "message": {"role": "user",
                 "content": [{"type": "text", "text": "do more"}]}}]
        log = self._claude_log("b", rows)
        self.assertEqual(st.classify("claude", "b", now=self._at(log, 5))["state"], st.ACTIVE)

    def test_resolve_log_rejects_path_like_session(self):
        # A session id is an opaque token; a path-shaped value never reaches the filesystem.
        for evil in ("../../etc/passwd", "$HOME/.ssh/id_rsa", "a/b", "~/secret", "x\x00y"):
            self.assertIsNone(st.resolve_log("claude", evil))


class TestCodexStates(StateBase):
    def _complete(self):
        return {"type": "event_msg", "payload": {"type": "task_complete",
                "last_agent_message": "finished"}}

    def _working(self):
        return {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}}

    def test_unavailable_when_no_log(self):
        self.assertEqual(st.classify("codex", "ghost", now=1.0)["state"], st.UNAVAILABLE)

    def test_active_recent(self):
        log = self._codex_log("A", [self._working()])
        self.assertEqual(st.classify("codex", "A", now=self._at(log, 5))["state"], st.ACTIVE)

    def test_idle_turn_complete(self):
        log = self._codex_log("A", [self._working(), self._complete()])
        self.assertEqual(st.classify("codex", "A", now=self._at(log, 300))["state"], st.IDLE)

    def test_unknown_quiet_no_completion(self):
        log = self._codex_log("A", [self._working()])
        self.assertEqual(st.classify("codex", "A", now=self._at(log, 300))["state"], st.UNKNOWN)

    def test_waiting_on_input_request(self):
        rows = [self._working(),
                {"type": "event_msg", "payload": {"type": "agent_message",
                 "message": "I need your input required to proceed"}}]
        log = self._codex_log("A", rows)
        self.assertEqual(st.classify("codex", "A", now=self._at(log, 10))["state"], st.WAITING)

    def test_completed_process_gone(self):
        log = self._codex_log("A", [self._complete()])
        self.assertEqual(
            st.classify("codex", "A", process_alive=False, now=self._at(log, 300))["state"],
            st.COMPLETED)

    def test_reengaged_after_completion_is_not_idle(self):
        rows = [self._working(), self._complete(),
                {"type": "event_msg", "payload": {"type": "user_message", "message": "do more"}}]
        log = self._codex_log("A", rows)
        self.assertEqual(st.classify("codex", "A", now=self._at(log, 5))["state"], st.ACTIVE)


class TestDeliverability(StateBase):
    def test_push_deliverable_set(self):
        self.assertTrue(st.is_push_deliverable(st.IDLE))
        self.assertTrue(st.is_push_deliverable(st.WAITING))
        for s in (st.ACTIVE, st.UNKNOWN, st.STALE, st.COMPLETED, st.UNAVAILABLE):
            self.assertFalse(st.is_push_deliverable(s), s)


if __name__ == "__main__":
    unittest.main()
