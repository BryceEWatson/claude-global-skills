#!/usr/bin/env python3
"""Hermetic tests for portal_mcp — JSON-RPC 2.0 dispatch, tool schemas, error responses.

Exercises the pure dispatch() function (no real stdio) plus one full stdin/stdout loop
pass, all against a throwaway DB.
"""
import importlib.util
import io
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


core = _load("portal_core")
mcp = _load("portal_mcp")


class McpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {k: os.environ.get(k) for k in ("SESSION_PORTAL_DB", "SESSION_PORTAL_HOME")}
        os.environ["SESSION_PORTAL_HOME"] = str(self.tmp)
        os.environ["SESSION_PORTAL_DB"] = str(self.tmp / "portal.db")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def factory(self):
        conn = core.connect()
        core.init_db(conn)
        return conn

    def call(self, name, args, req_id=1):
        req = {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
               "params": {"name": name, "arguments": args}}
        resp = mcp.dispatch(req, self.factory)
        return resp["result"], json.loads(resp["result"]["content"][0]["text"])


class TestProtocol(McpBase):
    def test_initialize(self):
        r = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, self.factory)
        self.assertEqual(r["result"]["serverInfo"]["name"], "session-portal")
        self.assertIn("protocolVersion", r["result"])

    def test_initialized_notification_returns_none(self):
        r = mcp.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.factory)
        self.assertIsNone(r)

    def test_tools_list_covers_required_semantics(self):
        r = mcp.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.factory)
        names = {t["name"] for t in r["result"]["tools"]}
        for required in ("portal_list_sessions", "portal_get_session", "portal_send_message",
                         "portal_list_inbox", "portal_acknowledge", "portal_cancel_message",
                         "portal_get_message_status"):
            self.assertIn(required, names)
        # Every tool has an object input schema with additionalProperties disabled.
        for t in r["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"]["additionalProperties"])

    def test_bad_jsonrpc_version(self):
        r = mcp.dispatch({"id": 1, "method": "initialize"}, self.factory)
        self.assertEqual(r["error"]["code"], -32600)

    def test_unknown_method(self):
        r = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "does/not/exist"}, self.factory)
        self.assertEqual(r["error"]["code"], -32601)

    def test_unknown_tool(self):
        r = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "portal_nope", "arguments": {}}}, self.factory)
        self.assertEqual(r["error"]["code"], -32601)


class TestToolCalls(McpBase):
    def test_register_and_send_flow(self):
        _, reg = self.call("portal_register_session", {"product": "claude", "runtime_session_id": "B"})
        self.assertEqual(reg["session_id"], "claude:B")
        result, sent = self.call("portal_send_message",
                                 {"source_session_id": "codex:A", "dest_session_id": "claude:B",
                                  "body": "hi", "authorship": "user", "idempotency_key": "k1"})
        self.assertFalse(result["isError"])
        self.assertEqual(sent["status"], "queued")
        _, inbox = self.call("portal_list_inbox",
                             {"dest_session_id": "claude:B", "deliver": True, "boundary": "stop"})
        self.assertEqual(inbox[0]["status"], "delivered")
        _, ack = self.call("portal_acknowledge", {"message_id": sent["message_id"], "by": "claude:B"})
        self.assertEqual(ack["status"], "acknowledged")

    def test_prohibited_field_rejected(self):
        result, payload = self.call("portal_send_message",
                                    {"source_session_id": "codex:A", "dest_session_id": "claude:B",
                                     "body": "hi", "authorship": "user", "reasoning": "hidden"})
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "validation_error")

    def test_unknown_argument_rejected(self):
        result, payload = self.call("portal_send_message",
                                    {"source_session_id": "codex:A", "dest_session_id": "claude:B",
                                     "body": "hi", "authorship": "user", "bogus": 1})
        self.assertTrue(result["isError"])
        self.assertIn("unknown argument", payload["error"])

    def test_missing_required_argument_rejected(self):
        result, payload = self.call("portal_send_message",
                                    {"source_session_id": "codex:A", "body": "hi", "authorship": "user"})
        self.assertTrue(result["isError"])
        self.assertIn("missing required", payload["error"])

    def test_enum_violation_rejected(self):
        result, payload = self.call("portal_send_message",
                                    {"source_session_id": "codex:A", "dest_session_id": "claude:B",
                                     "body": "hi", "authorship": "robot"})
        self.assertTrue(result["isError"])

    def test_secret_body_rejected_via_mcp(self):
        result, payload = self.call("portal_send_message",
                                    {"source_session_id": "codex:A", "dest_session_id": "claude:B",
                                     "body": "token ghp_" + "a" * 30, "authorship": "user"})
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "validation_error")

    def test_not_found_is_structured_error(self):
        result, payload = self.call("portal_get_message_status", {"message_id": "msg_missing"})
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "not_found")

    def test_health(self):
        _, health = self.call("portal_health", {})
        self.assertTrue(health["ok"])
        self.assertIn("schema_version", health)


class TestStdioLoop(McpBase):
    def test_full_stdio_roundtrip(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            "not json at all",
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "portal_health", "arguments": {}}}),
        ]
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()
        mcp.serve(stdin=stdin, stdout=stdout, conn_factory=self.factory)
        out_lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
        # initialize + tools/list + parse-error + health = 4 responses (notification is silent).
        ids = [o.get("id") for o in out_lines]
        self.assertIn(1, ids)
        self.assertIn(2, ids)
        self.assertIn(3, ids)
        self.assertTrue(any(o.get("error", {}).get("code") == -32700 for o in out_lines))


if __name__ == "__main__":
    unittest.main()
