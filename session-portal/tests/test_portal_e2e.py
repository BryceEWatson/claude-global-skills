#!/usr/bin/env python3
"""End-to-end cross-runtime round trip for the session portal.

This is the honest version of the issue-#16 "controlled bidirectional forward test": it does
NOT poke the database or call the admin CLI to simulate delivery. It drives the REAL
portal_mcp.py server over the REAL stdio JSON-RPC transport, once per side, each server bound
to a distinct AUTHENTICATED principal token — exactly how Claude Code and Codex would each
launch their own portal MCP server. Each side has a disposable session with a real transcript
file on disk that the portal reads as runtime evidence.

It exercises a full disposable round trip in BOTH directions:
    Claude(E2E-C) -> Codex(E2E-X):  send, Codex pulls at its own boundary, Codex acks.
    Codex(E2E-X) -> Claude(E2E-C):  send, Claude pulls at its own boundary, Claude acks.

and asserts:
  * identity is derived from the bound token (a session cannot send AS another, nor ack
    another session's message),
  * acknowledgement and the created/delivered/acknowledged audit trail are recorded,
  * neither disposable transcript file is mutated (sha256 identical before/after) — the
    portal never writes a transcript,
  * cleanup removes the portal DB and the disposable sessions.

Pure stdlib; runs in CI (it needs no real `claude`/`codex` process, only the real MCP
transport + real authenticated identities + disposable sessions).
"""
import hashlib
import importlib.util
import json
import os
import subprocess
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MCPClient:
    """A minimal real MCP stdio client: spawns portal_mcp.py as a subprocess bound to one
    principal token and speaks newline-delimited JSON-RPC 2.0 over its stdin/stdout."""

    def __init__(self, env: dict, token: str):
        run_env = dict(os.environ)
        run_env.update(env)
        run_env["SESSION_PORTAL_TOKEN"] = token
        run_env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, str(_SCRIPTS / "portal_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(_SCRIPTS), env=run_env, text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0
        self._rpc("initialize", {})
        self._notify("notifications/initialized")

    def _rpc(self, method, params):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id,
                                          "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"MCP server produced no response; stderr:\n{err}")
        return json.loads(line)

    def _notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name, args):
        resp = self._rpc("tools/call", {"name": name, "arguments": args})
        result = resp["result"]
        payload = json.loads(result["content"][0]["text"])
        return result["isError"], payload

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
                self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass


class TestPortalE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="portal-e2e-"))
        self.claude_logs = self.tmp / "claude-logs" / "proj"
        self.codex_logs = self.tmp / "codex-logs" / "2026" / "07" / "11"
        self.claude_logs.mkdir(parents=True)
        self.codex_logs.mkdir(parents=True)
        # Disposable transcripts (the sessions' runtime evidence). The portal must NEVER
        # write these; we hash them before/after to prove immutability.
        self.claude_transcript = self.claude_logs / "E2E-C.jsonl"
        self.claude_transcript.write_text(
            json.dumps({"message": {"role": "assistant", "stop_reason": "end_turn",
                                    "content": [{"type": "text", "text": "idle"}]}}) + "\n",
            encoding="utf-8")
        self.codex_transcript = self.codex_logs / "rollout-2026-07-11-E2E-X.jsonl"
        self.codex_transcript.write_text(
            json.dumps({"type": "event_msg", "payload": {"type": "task_complete",
                                                        "last_agent_message": "idle"}}) + "\n",
            encoding="utf-8")

        self.env = {
            "SESSION_PORTAL_HOME": str(self.tmp / "portal"),
            "SESSION_PORTAL_DB": str(self.tmp / "portal" / "portal.db"),
            "SESSION_PORTAL_CLAUDE_LOGS": str(self.tmp / "claude-logs"),
            "SESSION_PORTAL_CODEX_LOGS": str(self.tmp / "codex-logs"),
        }
        for k, v in self.env.items():
            os.environ[k] = v
        os.environ.pop("SESSION_PORTAL_TOKEN", None)

        # Operator step: mint principal tokens for the two disposable sessions.
        conn = core.connect()
        core.init_db(conn)
        self.tok_claude = core.issue_principal(conn, "claude", "E2E-C", ttl_seconds=3600)["token"]
        self.tok_codex = core.issue_principal(conn, "codex", "E2E-X", ttl_seconds=3600)["token"]
        conn.close()

        self.claude = MCPClient(self.env, self.tok_claude)
        self.codex = MCPClient(self.env, self.tok_codex)

    def tearDown(self):
        self.claude.close()
        self.codex.close()
        # Cleanup: remove the portal DB and disposable sessions.
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in self.env:
            os.environ.pop(k, None)
        os.environ.pop("SESSION_PORTAL_TOKEN", None)

    def _events(self, client, message_id):
        is_err, payload = client.call_tool("portal_message_events", {"message_id": message_id})
        self.assertFalse(is_err, payload)
        return [e["event"] for e in payload]

    def test_bidirectional_round_trip(self):
        c_before = _sha256(self.claude_transcript)
        x_before = _sha256(self.codex_transcript)

        # Register both (announce self; identity derived from token).
        self.claude.call_tool("portal_register_session", {"label": "disposable claude"})
        self.codex.call_tool("portal_register_session", {"label": "disposable codex"})

        # ---- Direction 1: Claude -> Codex ------------------------------------------------
        is_err, sent1 = self.claude.call_tool("portal_send_message",
            {"dest_session_id": "codex:E2E-X", "body": "please rerun CI on main",
             "idempotency_key": "e2e-1"})
        self.assertFalse(is_err, sent1)
        self.assertEqual(sent1["source_session_id"], "claude:E2E-C")  # derived, not asserted
        self.assertEqual(sent1["status"], "queued")

        # Codex pulls its own inbox at its own boundary (real runtime acceptance) and acks.
        is_err, inbox = self.codex.call_tool("portal_list_inbox", {"deliver": True})
        self.assertFalse(is_err, inbox)
        got = [m for m in inbox if m["message_id"] == sent1["message_id"]][0]
        self.assertEqual(got["status"], "delivered")
        self.assertEqual(got["delivered_to"], "codex:E2E-X")
        is_err, ack1 = self.codex.call_tool("portal_acknowledge",
                                            {"message_id": sent1["message_id"], "note": "done"})
        self.assertFalse(is_err, ack1)
        self.assertEqual(ack1["status"], "acknowledged")
        self.assertEqual(ack1["acknowledged_by"], "codex:E2E-X")
        self.assertEqual(self._events(self.codex, sent1["message_id"]),
                         ["created", "delivered", "acknowledged"])

        # ---- Direction 2: Codex -> Claude ------------------------------------------------
        is_err, sent2 = self.codex.call_tool("portal_send_message",
            {"dest_session_id": "claude:E2E-C", "body": "CI is green on main",
             "idempotency_key": "e2e-2"})
        self.assertFalse(is_err, sent2)
        self.assertEqual(sent2["source_session_id"], "codex:E2E-X")
        is_err, inbox2 = self.claude.call_tool("portal_list_inbox", {"deliver": True})
        self.assertFalse(is_err, inbox2)
        got2 = [m for m in inbox2 if m["message_id"] == sent2["message_id"]][0]
        self.assertEqual(got2["status"], "delivered")
        is_err, ack2 = self.claude.call_tool("portal_acknowledge", {"message_id": sent2["message_id"]})
        self.assertFalse(is_err, ack2)
        self.assertEqual(ack2["acknowledged_by"], "claude:E2E-C")

        # ---- Spoofing is impossible ------------------------------------------------------
        # Codex cannot acknowledge a message addressed to Claude.
        is_err, spoof = self.codex.call_tool("portal_acknowledge", {"message_id": sent2["message_id"]})
        self.assertTrue(is_err)
        self.assertEqual(spoof["code"], "authorization_error")

        # ---- No transcript mutation ------------------------------------------------------
        self.assertEqual(_sha256(self.claude_transcript), c_before, "claude transcript was mutated")
        self.assertEqual(_sha256(self.codex_transcript), x_before, "codex transcript was mutated")

    def test_cleanup_removes_state(self):
        self.claude.call_tool("portal_send_message",
                              {"dest_session_id": "codex:E2E-X", "body": "hi", "idempotency_key": "c1"})
        db = Path(self.env["SESSION_PORTAL_DB"])
        self.assertTrue(db.exists())
        # Simulate operator uninstall.
        self.claude.close()
        self.codex.close()
        out = subprocess.run([sys.executable, str(_SCRIPTS / "portal_admin.py"), "uninstall", "--yes"],
                             cwd=str(_SCRIPTS), env={**os.environ, **self.env},
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(db.exists())


if __name__ == "__main__":
    unittest.main()
