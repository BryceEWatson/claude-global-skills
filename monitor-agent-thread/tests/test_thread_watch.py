#!/usr/bin/env python3
"""Privacy, direction, and false-blocker tests for monitor-agent-thread.

The monitoring skill's safe projection must NEVER expose hidden reasoning, raw tool
arguments, signatures, encrypted content, credentials/secrets, or system/developer
instructions — for either monitoring direction (Codex watching Claude, Claude
watching Codex). These tests build synthetic Claude and Codex session logs that plant
each forbidden surface and assert none of them leak into the projection, plus the
false-blocker / stall discipline.

Run:  python -m unittest discover -s monitor-agent-thread/tests -p 'test_*.py'
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

_TW_PATH = Path(__file__).resolve().parent.parent / "scripts" / "thread_watch.py"
_spec = importlib.util.spec_from_file_location("thread_watch_under_test", _TW_PATH)
tw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tw)

# Distinctive markers for each forbidden surface (must never appear in output).
HIDDEN = "HIDDEN_REASONING_MARKER"
SIG = "SIGNATURE_MARKER"
TOOLARG = "TOOLARG_MARKER"
SYSINSTR = "SYSTEM_INSTRUCTION_MARKER"
ENC = "ENCRYPTED_CONTENT_MARKER"
OUTBODY = "TOOL_OUTPUT_BODY_MARKER"
SECRET = "sk-ant-visiblesecretshouldberedacted123456"
VISIBLE = "VISIBLE_PROSE_MARKER"

T0 = "2026-07-11T10:00:00Z"
T1 = "2026-07-11T10:30:00Z"


def write_jsonl(rows) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "session.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return p


def claude_rows():
    return [
        {"timestamp": T0, "sessionId": "claude-1", "cwd": "/repo", "gitBranch": "main",
         "message": {"role": "system", "content": [
             {"type": "text", "text": f"{SYSINSTR}: obey developer {SECRET}"}]}},
        {"timestamp": T0, "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": f"{HIDDEN} plotting with {SECRET}",
             "signature": SIG}]}},
        {"timestamp": T0, "message": {"role": "assistant", "content": [
            {"type": "redacted_thinking", "data": ENC}]}},
        {"timestamp": T0, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": f"echo {TOOLARG} {SECRET}"}}]}},
        {"timestamp": T0, "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": True, "content": f"boom {OUTBODY}"}]}},
        {"timestamp": T1, "message": {"role": "assistant", "stop_reason": "end_turn",
         "content": [{"type": "text", "text": f"{VISIBLE} done; key {SECRET}"}]}},
    ]


def codex_rows():
    return [
        {"timestamp": T0, "type": "session_meta",
         "payload": {"session_id": "codex-1", "cwd": "/repo"}},
        {"timestamp": T0, "type": "response_item",
         "payload": {"type": "reasoning", "summary": f"{HIDDEN} with {SECRET}",
                     "signature": SIG}},
        {"timestamp": T0, "type": "response_item",
         "payload": {"type": "message", "role": "developer",
                     "content": f"{SYSINSTR} instruction"}},
        {"timestamp": T0, "type": "response_item",
         "payload": {"type": "reasoning", "encrypted_content": ENC}},
        {"timestamp": T0, "type": "response_item",
         "payload": {"type": "function_call", "name": "shell",
                     "arguments": f"{{\"cmd\": \"{TOOLARG} {SECRET}\"}}"}},
        {"timestamp": T0, "type": "response_item",
         "payload": {"type": "function_call_output", "output": f"error: fail {OUTBODY}"}},
        {"timestamp": T1, "type": "event_msg",
         "payload": {"type": "agent_message", "message": f"{VISIBLE} done; key {SECRET}"}},
    ]


class PrivacyMixin:
    rows_fn = None
    product = None

    def _projection_blob(self, rows):
        path = write_jsonl(rows)
        snap = tw.snapshot(path, self.product, stall_seconds=600)
        return snap, json.dumps(snap, ensure_ascii=False)

    def test_all_forbidden_surfaces_excluded(self):
        snap, blob = self._projection_blob(self.rows_fn())
        for marker in (HIDDEN, SIG, TOOLARG, SYSINSTR, ENC, OUTBODY):
            self.assertNotIn(marker, blob, f"leaked forbidden surface: {marker}")
        # Raw secret redacted, but the visible prose around it survives.
        self.assertNotIn(SECRET, blob, "raw secret leaked")
        self.assertIn(VISIBLE, blob, "visible assistant prose should be projected")
        self.assertIn("[REDACTED]", blob, "secret in visible prose should be redacted")

    def test_tool_names_projected_without_args(self):
        snap, blob = self._projection_blob(self.rows_fn())
        tools = snap["safe_projection"]["recent_tools"]
        self.assertTrue(tools, "expected at least one tool name projected")
        self.assertNotIn(TOOLARG, blob)


class TestClaudeFromCodex(PrivacyMixin, unittest.TestCase):
    """Codex watching a Claude Code session (claude projection)."""
    rows_fn = staticmethod(claude_rows)
    product = "claude"


class TestCodexFromClaude(PrivacyMixin, unittest.TestCase):
    """Claude Code watching a Codex session (codex projection)."""
    rows_fn = staticmethod(codex_rows)
    product = "codex"


class TestBlockerDiscipline(unittest.TestCase):
    def test_stale_alert_is_not_active_blocker(self):
        # An alert early, normal activity later -> the stale alert is NOT active.
        rows = [
            {"timestamp": T0, "sessionId": "c", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "permission denied earlier"}]}},
            {"timestamp": T1, "message": {"role": "assistant",
             "content": [{"type": "text", "text": "continuing along fine now"}]}},
        ]
        snap = tw.snapshot(write_jsonl(rows), "claude", stall_seconds=600)
        self.assertEqual(snap["safe_projection"]["active_alerts"], [])
        self.assertNotEqual(snap["state"], "attention")

    def test_recent_alert_raises_attention(self):
        rows = [
            {"timestamp": T0, "sessionId": "c", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "working"}]}},
            {"timestamp": T1, "message": {"role": "assistant",
             "content": [{"type": "text", "text": "permission denied on the socket"}]}},
        ]
        snap = tw.snapshot(write_jsonl(rows), "claude", stall_seconds=600)
        self.assertTrue(snap["safe_projection"]["active_alerts"])
        self.assertEqual(snap["state"], "attention")

    def test_quiet_recent_session_is_active_not_stalled(self):
        rows = [{"timestamp": T1, "sessionId": "c", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "thinking quietly"}]}}]
        path = write_jsonl(rows)  # fresh mtime
        snap = tw.snapshot(path, "claude", stall_seconds=600)
        self.assertEqual(snap["state"], "active")

    def test_old_mtime_is_stalled_candidate(self):
        rows = [{"timestamp": T1, "sessionId": "c", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "no completion here"}]}}]
        path = write_jsonl(rows)
        old = time.time() - 5000
        os.utime(path, (old, old))
        snap = tw.snapshot(path, "claude", stall_seconds=600)
        self.assertEqual(snap["state"], "stalled_candidate")

    def test_explicit_completion_reported(self):
        rows = [
            {"timestamp": T0, "sessionId": "c", "message": {"role": "assistant",
             "content": [{"type": "tool_use", "name": "Bash", "input": {"x": 1}}]}},
            {"timestamp": T1, "message": {"role": "assistant", "stop_reason": "end_turn",
             "content": [{"type": "text", "text": "finished the task"}]}},
        ]
        snap = tw.snapshot(write_jsonl(rows), "claude", stall_seconds=600)
        self.assertTrue(snap["safe_projection"]["explicit_turn_complete"])
        self.assertEqual(snap["state"], "completed_turn")

    def test_codex_task_complete_reported(self):
        rows = [
            {"timestamp": T0, "type": "session_meta", "payload": {"session_id": "x"}},
            {"timestamp": T0, "type": "response_item",
             "payload": {"type": "function_call", "name": "shell", "arguments": "{}"}},
            {"timestamp": T1, "type": "event_msg",
             "payload": {"type": "task_complete", "last_agent_message": "all green"}},
        ]
        snap = tw.snapshot(write_jsonl(rows), "codex", stall_seconds=600)
        self.assertTrue(snap["safe_projection"]["explicit_turn_complete"])
        self.assertEqual(snap["state"], "completed_turn")


class TestDirectionDetection(unittest.TestCase):
    def test_detect_product_from_path(self):
        self.assertEqual(tw.detect_product(Path("/home/u/.claude/projects/s.jsonl")), "claude")
        self.assertEqual(tw.detect_product(Path("/home/u/.codex/sessions/rollout-x.jsonl")), "codex")
        with self.assertRaises(ValueError):
            tw.detect_product(Path("/tmp/unknown.jsonl"))

    def test_discover_finds_both_products(self):
        base = Path(tempfile.mkdtemp())
        croot = base / ".claude" / "projects" / "p"
        xroot = base / ".codex" / "sessions" / "2026" / "07" / "11"
        croot.mkdir(parents=True)
        xroot.mkdir(parents=True)
        (croot / "claude-sess.jsonl").write_text(
            json.dumps({"timestamp": T0, "sessionId": "cs", "cwd": "/repo"}) + "\n",
            encoding="utf-8")
        (xroot / "rollout-2026-thread.jsonl").write_text(
            json.dumps({"timestamp": T0, "type": "session_meta",
                        "payload": {"session_id": "xs", "cwd": "/repo"}}) + "\n",
            encoding="utf-8")
        saved = (tw.CLAUDE_ROOT, tw.CODEX_ROOT)
        try:
            tw.CLAUDE_ROOT = base / ".claude" / "projects"
            tw.CODEX_ROOT = base / ".codex" / "sessions"
            paths = {p.name for p in tw.candidate_paths("auto")}
        finally:
            tw.CLAUDE_ROOT, tw.CODEX_ROOT = saved
        self.assertIn("claude-sess.jsonl", paths)
        self.assertIn("rollout-2026-thread.jsonl", paths)


if __name__ == "__main__":
    unittest.main()
