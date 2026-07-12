#!/usr/bin/env python3
"""Hermetic tests for portal_mcp — JSON-RPC 2.0 dispatch, AUTHENTICATED identity, tool
schemas, error responses. Every tool that touches identity or message data requires a valid
bearer token; the server derives source / inbox / acknowledger identity from the bound
principal, never from a tool argument. All against a throwaway DB.
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
        self._saved = {k: os.environ.get(k) for k in
                       ("SESSION_PORTAL_DB", "SESSION_PORTAL_HOME", "SESSION_PORTAL_TOKEN")}
        os.environ["SESSION_PORTAL_HOME"] = str(self.tmp)
        os.environ["SESSION_PORTAL_DB"] = str(self.tmp / "portal.db")
        os.environ.pop("SESSION_PORTAL_TOKEN", None)
        # Mint two principals so tests can act as each side.
        conn = self.factory()
        self.tok_B = core.issue_principal(conn, "claude", "B", ttl_seconds=3600)["token"]
        self.tok_A = core.issue_principal(conn, "codex", "A", ttl_seconds=3600)["token"]
        conn.close()

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

    def call(self, name, args, token=None, req_id=1):
        req = {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
               "params": {"name": name, "arguments": args}}
        resp = mcp.dispatch(req, self.factory, token=token)
        return resp["result"], json.loads(resp["result"]["content"][0]["text"])

    def grant(self, grantee, capability, scope="*", ttl=3600):
        conn = self.factory()
        core.grant_capability(conn, grantee, capability, scope=scope, ttl_seconds=ttl)
        conn.close()


class TestProtocol(McpBase):
    def test_initialize(self):
        r = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, self.factory)
        self.assertEqual(r["result"]["serverInfo"]["name"], "session-portal")
        self.assertIn("protocolVersion", r["result"])

    def test_tools_list_covers_required_semantics(self):
        r = mcp.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.factory)
        names = {t["name"] for t in r["result"]["tools"]}
        for required in ("portal_list_sessions", "portal_get_session", "portal_send_message",
                         "portal_list_inbox", "portal_acknowledge", "portal_cancel_message",
                         "portal_get_message_status"):
            self.assertIn(required, names)
        for t in r["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertFalse(t["inputSchema"]["additionalProperties"])
        # The old caller-supplied identity/authorization args are GONE from the send schema.
        send = next(t for t in r["result"]["tools"] if t["name"] == "portal_send_message")
        props = set(send["inputSchema"]["properties"])
        self.assertNotIn("source_session_id", props)
        self.assertNotIn("authorized", props)
        inbox = next(t for t in r["result"]["tools"] if t["name"] == "portal_list_inbox")
        iprops = set(inbox["inputSchema"]["properties"])
        self.assertNotIn("dest_session_id", iprops)
        self.assertNotIn("boundary", iprops)

    def test_bad_jsonrpc_version(self):
        r = mcp.dispatch({"id": 1, "method": "initialize"}, self.factory)
        self.assertEqual(r["error"]["code"], -32600)

    def test_unknown_tool(self):
        r = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "portal_nope", "arguments": {}}}, self.factory)
        self.assertEqual(r["error"]["code"], -32601)


class TestAuthentication(McpBase):
    def test_no_token_rejects_identity_tool(self):
        result, payload = self.call("portal_send_message",
                                    {"dest_session_id": "claude:B", "body": "hi"}, token=None)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "authorization_error")

    def test_bad_token_rejected(self):
        result, payload = self.call("portal_list_inbox", {}, token="bogus-token-1234567890")
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "authorization_error")

    def test_health_needs_no_token(self):
        result, health = self.call("portal_health", {}, token=None)
        self.assertFalse(result["isError"])
        self.assertTrue(health["ok"])
        self.assertEqual(health["delivery_model"], "pull-only")


class TestIdentityDerivation(McpBase):
    def test_send_source_is_bound_principal(self):
        # Acting with codex:A's token, the message source is codex:A — no source arg supplied.
        result, sent = self.call("portal_send_message",
                                 {"dest_session_id": "claude:B", "body": "hi",
                                  "idempotency_key": "k1"}, token=self.tok_A)
        self.assertFalse(result["isError"])
        self.assertEqual(sent["source_session_id"], "codex:A")
        self.assertEqual(sent["authorship"], "agent")  # default without a speak-as-user grant

    def test_inbox_drains_only_own(self):
        # codex:A sends to claude:B. B pulls its own inbox (its token) and delivers.
        _, sent = self.call("portal_send_message",
                            {"dest_session_id": "claude:B", "body": "hi", "idempotency_key": "k"},
                            token=self.tok_A)
        _, inbox = self.call("portal_list_inbox", {"deliver": True}, token=self.tok_B)
        self.assertEqual(inbox[0]["status"], "delivered")
        self.assertEqual(inbox[0]["dest_session_id"], "claude:B")
        # A pulling its own inbox sees nothing addressed to it.
        _, inbox_a = self.call("portal_list_inbox", {"deliver": True}, token=self.tok_A)
        self.assertEqual(inbox_a, [])

    def test_ack_is_by_principal_only(self):
        _, sent = self.call("portal_send_message",
                            {"dest_session_id": "claude:B", "body": "hi", "idempotency_key": "k"},
                            token=self.tok_A)
        self.call("portal_list_inbox", {"deliver": True}, token=self.tok_B)
        # A (the sender, not the recipient) may not acknowledge.
        result, payload = self.call("portal_acknowledge", {"message_id": sent["message_id"]},
                                    token=self.tok_A)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "authorization_error")
        # B (the recipient) can.
        result, ack = self.call("portal_acknowledge", {"message_id": sent["message_id"]},
                                token=self.tok_B)
        self.assertEqual(ack["status"], "acknowledged")
        self.assertEqual(ack["acknowledged_by"], "claude:B")

    def test_message_read_requires_party(self):
        _, sent = self.call("portal_send_message",
                            {"dest_session_id": "claude:B", "body": "secret-ish note",
                             "idempotency_key": "k"}, token=self.tok_A)
        # A third principal cannot read someone else's message.
        conn = self.factory()
        tok_C = core.issue_principal(conn, "codex", "C", ttl_seconds=3600)["token"]
        conn.close()
        result, payload = self.call("portal_get_message_status", {"message_id": sent["message_id"]},
                                    token=tok_C)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "authorization_error")


class TestGrantEnforcement(McpBase):
    def test_steer_requires_grant(self):
        result, payload = self.call("portal_send_message",
                                    {"dest_session_id": "claude:B", "body": "do X now",
                                     "kind": "steer"}, token=self.tok_A)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "authorization_error")
        # With a send-steer grant it queues (delivery still needs the dest's accept grant).
        self.grant("codex:A", core.CAP_SEND_STEER, scope="claude:B")
        result, sent = self.call("portal_send_message",
                                 {"dest_session_id": "claude:B", "body": "do X now",
                                  "kind": "steer", "idempotency_key": "s1"}, token=self.tok_A)
        self.assertFalse(result["isError"])
        self.assertEqual(sent["kind"], "steer")

    def test_user_authorship_requires_grant(self):
        result, payload = self.call("portal_send_message",
                                    {"dest_session_id": "claude:B", "body": "hi",
                                     "authorship": "user"}, token=self.tok_A)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "authorization_error")


class TestValidationStillEnforced(McpBase):
    def test_prohibited_field_rejected(self):
        result, payload = self.call("portal_send_message",
                                    {"dest_session_id": "claude:B", "body": "hi",
                                     "reasoning": "hidden"}, token=self.tok_A)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "validation_error")

    def test_unknown_argument_rejected(self):
        result, payload = self.call("portal_send_message",
                                    {"dest_session_id": "claude:B", "body": "hi", "bogus": 1},
                                    token=self.tok_A)
        self.assertTrue(result["isError"])
        self.assertIn("unknown argument", payload["error"])

    def test_missing_required_argument_rejected(self):
        result, payload = self.call("portal_send_message", {"body": "hi"}, token=self.tok_A)
        self.assertTrue(result["isError"])
        self.assertIn("missing required", payload["error"])

    def test_secret_body_rejected_via_mcp(self):
        result, payload = self.call("portal_send_message",
                                    {"dest_session_id": "claude:B", "body": "token ghp_" + "a" * 30},
                                    token=self.tok_A)
        self.assertTrue(result["isError"])
        self.assertEqual(payload["code"], "validation_error")

    def test_not_found_is_structured_error(self):
        result, payload = self.call("portal_get_message_status", {"message_id": "msg_missing"},
                                    token=self.tok_A)
        self.assertTrue(result["isError"])
        # A missing message is reported as not-a-party (no existence oracle) or not_found.
        self.assertIn(payload["code"], ("authorization_error", "not_found"))

    def test_negative_ttl_rejected(self):
        result, payload = self.call("portal_send_message",
                                    {"dest_session_id": "claude:B", "body": "x", "ttl_seconds": -999},
                                    token=self.tok_A)
        self.assertTrue(result["isError"])
        self.assertIn(">=", payload["error"])

    def test_boolean_string_not_coerced(self):
        result, payload = self.call("portal_list_inbox", {"deliver": "false"}, token=self.tok_B)
        self.assertTrue(result["isError"])
        self.assertIn("boolean", payload["error"])

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
        mcp.serve(stdin=stdin, stdout=stdout, conn_factory=self.factory, token=self.tok_B)
        out_lines = [json.loads(l) for l in stdout.getvalue().splitlines() if l.strip()]
        ids = [o.get("id") for o in out_lines]
        self.assertIn(1, ids)
        self.assertIn(2, ids)
        self.assertIn(3, ids)
        self.assertTrue(any(o.get("error", {}).get("code") == -32700 for o in out_lines))


if __name__ == "__main__":
    unittest.main()
