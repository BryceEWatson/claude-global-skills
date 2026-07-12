#!/usr/bin/env python3
"""portal_mcp.py — local stdio MCP server for the session portal (authenticated).

Speaks newline-delimited JSON-RPC 2.0 over stdin/stdout (the MCP stdio transport). It binds
nothing to the network. Message content is treated as untrusted data and is never executed.

Authentication: the server is launched by exactly ONE session, which supplies a bearer token
in the SESSION_PORTAL_TOKEN environment variable (the operator mints it with
`portal_admin issue-principal` and puts it in that session's MCP config). At each tool call
the server resolves the token to a PRINCIPAL (product:runtime_session_id) and derives all
identity from it:
  * `portal_send_message` sends AS the principal — there is no caller-supplied source.
  * `portal_list_inbox` drains only the PRINCIPAL's own inbox.
  * `portal_acknowledge` / `portal_cancel_message` act AS the principal, on its own messages.
  * message reads are limited to messages the principal is a party to.
There is no caller-supplied `source_session_id`, `authorized`, `accepts_steering`,
`boundary`, or `process_alive`: authorization is by operator grant and boundary/liveness are
derived from runtime-owned evidence. `portal_health` and the protocol methods are the only
things callable without a token.

dispatch() is pure (dict in, dict out) so the protocol layer is tested without a process.
stdlib only; Windows-safe.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

import portal_core as core
import portal_state as pstate

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "session-portal", "version": "2.0.0"}

_ID = {"type": "string", "maxLength": 128}


def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": desc,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS: list[dict] = [
    _tool("portal_register_session",
          "Announce THIS session (the authenticated principal) so it can be discovered and "
          "addressed. Identity comes from the bound token, not from arguments.",
          {"cwd": {"type": "string", "maxLength": 1024},
           "label": {"type": "string", "maxLength": core.MAX_LABEL_LEN}},
          []),
    _tool("portal_list_sessions", "List known sessions (optionally filtered by product).",
          {"product": {"type": "string", "enum": list(core.VALID_PRODUCTS)},
           "registered_only": {"type": "boolean"}},
          []),
    _tool("portal_get_session", "Get one session's discovery record.",
          {"session_id": _ID}, ["session_id"]),
    _tool("portal_send_message",
          "Queue a message to another session's inbox, sent AS the authenticated principal "
          "(no caller-supplied source). Idempotent when idempotency_key is supplied. "
          "authorship='user' needs an operator speak-as-user grant; kind='steer' needs an "
          "operator send-steer grant for the destination. Content is untrusted data.",
          {"dest_session_id": _ID,
           "body": {"type": "string", "maxLength": core.MAX_BODY_BYTES * 2},
           "authorship": {"type": "string", "enum": list(core.VALID_AUTHORSHIP)},
           "kind": {"type": "string", "enum": list(core.VALID_KINDS)},
           "idempotency_key": _ID,
           "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 30 * 24 * 3600},
           "forward_of": _ID},
          ["dest_session_id", "body"]),
    _tool("portal_list_inbox",
          "List and (with deliver=true) drain THIS principal's own inbox. Delivering is the "
          "safe PULL: queued messages become delivered because the authenticated recipient "
          "itself is pulling them — the only proof of receipt. No boundary argument: the "
          "boundary is the pull itself, annotated with runtime-derived state.",
          {"status": {"type": "string", "enum": list(core.TERMINAL_STATES) +
                      [core.STATUS_QUEUED, core.STATUS_DELIVERED]},
           "deliver": {"type": "boolean"},
           "max_deliver": {"type": "integer", "minimum": 1, "maximum": 1000}},
          []),
    _tool("portal_acknowledge", "Acknowledge a delivered message addressed to this principal "
          "(idempotent). The acknowledger identity comes from the bound token.",
          {"message_id": _ID, "note": {"type": "string", "maxLength": 500}},
          ["message_id"]),
    _tool("portal_cancel_message", "Cancel a queued/delivered message this principal is a "
          "party to, before acknowledgement.",
          {"message_id": _ID, "reason": {"type": "string", "maxLength": 500}},
          ["message_id"]),
    _tool("portal_get_message_status", "Get one message's current record (must be a party to it).",
          {"message_id": _ID}, ["message_id"]),
    _tool("portal_message_events", "Get a message's lifecycle audit trail (must be a party to it).",
          {"message_id": _ID}, ["message_id"]),
    _tool("portal_health", "Portal health: schema version, counts, leases, DB path. No token needed.",
          {}, []),
]
_TOOL_NAMES = {t["name"] for t in TOOLS}

# Tools that require an authenticated principal (everything that touches identity or message
# data). Only health is callable without a token.
_NO_AUTH_TOOLS = {"portal_health"}


# --------------------------------------------------------------------------- #
# Tool handlers (each: (conn, args, principal) -> json-able result). `principal` is the
# authenticated session_id, or None for the no-auth tools.
# --------------------------------------------------------------------------- #
def _h_register(conn, a, principal):
    return core.register_session(conn, core.product_of(principal), core.runtime_of(principal),
                                 cwd=a.get("cwd"), label=a.get("label"))


def _h_list_sessions(conn, a, principal):
    return core.list_sessions(conn, product=a.get("product"),
                              registered_only=bool(a.get("registered_only", False)))


def _h_get_session(conn, a, principal):
    return core.get_session(conn, a["session_id"])


def _h_send(conn, a, principal):
    return core.send_message(
        conn, principal, a["dest_session_id"], a["body"],
        authorship=a.get("authorship", "agent"), kind=a.get("kind", "note"),
        idempotency_key=a.get("idempotency_key"), ttl_seconds=a.get("ttl_seconds"),
        forward_of=a.get("forward_of"))


def _h_list_inbox(conn, a, principal):
    deliver = bool(a.get("deliver", False))
    at_boundary = False
    reason = ""
    if deliver:
        # The authenticated recipient is pulling its OWN inbox: that pull IS the safe turn
        # boundary. We additionally read its runtime-owned log to annotate (never to fetch a
        # caller-asserted boundary). A self-pull can only affect the caller's own session, so
        # it is always allowed; the annotation records the observed runtime state.
        cls = pstate.classify(core.product_of(principal), core.runtime_of(principal))
        at_boundary = True
        reason = f"authenticated self-pull; runtime state={cls['state']}"
    return core.list_inbox(conn, principal, status=a.get("status"), deliver=deliver,
                           at_boundary=at_boundary, boundary_reason=reason,
                           max_deliver=a.get("max_deliver"))


def _h_ack(conn, a, principal):
    return core.acknowledge(conn, a["message_id"], by=principal, note=a.get("note"))


def _h_cancel(conn, a, principal):
    return core.cancel_message(conn, a["message_id"], by=principal, reason=a.get("reason"))


def _assert_party(conn, message_id, principal):
    row = core.get_message_status(conn, message_id)
    if principal not in (row["source_session_id"], row["dest_session_id"]):
        raise core.AuthorizationError(f"{principal} is not a party to message {message_id}")
    return row


def _h_status(conn, a, principal):
    return _assert_party(conn, a["message_id"], principal)


def _h_events(conn, a, principal):
    _assert_party(conn, a["message_id"], principal)
    return core.message_events(conn, a["message_id"])


def _h_health(conn, a, principal):
    return core.health(conn)


HANDLERS: dict[str, Callable[[Any, dict, Any], Any]] = {
    "portal_register_session": _h_register,
    "portal_list_sessions": _h_list_sessions,
    "portal_get_session": _h_get_session,
    "portal_send_message": _h_send,
    "portal_list_inbox": _h_list_inbox,
    "portal_acknowledge": _h_ack,
    "portal_cancel_message": _h_cancel,
    "portal_get_message_status": _h_status,
    "portal_message_events": _h_events,
    "portal_health": _h_health,
}


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch (pure; testable without stdio)
# --------------------------------------------------------------------------- #
def _rpc_error(req_id, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _rpc_result(req_id, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _check_type(key: str, spec: dict, value) -> None:
    """Enforce the declared JSON-schema type and bounds. Critically, this does NOT coerce:
    a boolean field must be a real JSON boolean, so a truthy string like "false" is REJECTED
    rather than silently flipping a gate open."""
    t = spec.get("type")
    if t == "boolean":
        if not isinstance(value, bool):
            raise core.ValidationError(f"{key!r} must be a boolean (got {type(value).__name__})")
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise core.ValidationError(f"{key!r} must be an integer")
        if "minimum" in spec and value < spec["minimum"]:
            raise core.ValidationError(f"{key!r} must be >= {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise core.ValidationError(f"{key!r} must be <= {spec['maximum']}")
    elif t == "string":
        if not isinstance(value, str):
            raise core.ValidationError(f"{key!r} must be a string")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            raise core.ValidationError(f"{key!r} exceeds maxLength {spec['maxLength']}")
        if "minLength" in spec and len(value) < spec["minLength"]:
            raise core.ValidationError(f"{key!r} below minLength {spec['minLength']}")


def _validate_args(schema: dict, args: dict) -> None:
    """Schema enforcement: required keys present, no unknown keys (the whitelist that keeps
    prohibited fields out), declared type + bounds, and enum membership. No coercion."""
    props = schema.get("properties", {})
    for key in args:
        if key not in props:
            raise core.ValidationError(f"unknown argument {key!r} (not in tool schema)")
    for req in schema.get("required", []):
        if req not in args:
            raise core.ValidationError(f"missing required argument {req!r}")
    core.validate_no_prohibited_fields(args)
    for key, spec in props.items():
        if key not in args:
            continue
        _check_type(key, spec, args[key])
        if "enum" in spec and args[key] not in spec["enum"]:
            raise core.ValidationError(f"{key!r} must be one of {spec['enum']}")


def _tool_error(req_id, message, code):
    payload = json.dumps({"error": message, "code": code}, ensure_ascii=False)
    return _rpc_result(req_id, {"content": [{"type": "text", "text": payload}], "isError": True})


def dispatch(request: dict, conn_factory: Callable[[], Any], token: str | None = None) -> dict | None:
    """Handle one JSON-RPC request dict. Returns a response dict, or None for notifications
    (no id). conn_factory yields an initialized DB connection. `token` is the bearer token
    the launching session supplied (defaults to $SESSION_PORTAL_TOKEN); it is resolved to the
    authenticated principal for identity-bearing tools."""
    if token is None:
        token = os.environ.get("SESSION_PORTAL_TOKEN")
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "invalid JSON-RPC 2.0 request")
    method = request.get("method")
    req_id = request.get("id")
    is_notification = "id" not in request

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in _TOOL_NAMES:
            return _rpc_error(req_id, -32601, f"unknown tool: {name}")
        schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
        try:
            _validate_args(schema, args)
            conn = conn_factory()
            try:
                principal = None
                if name not in _NO_AUTH_TOOLS:
                    principal = core.resolve_principal(conn, token)  # raises AuthorizationError
                result = HANDLERS[name](conn, args, principal)
            finally:
                conn.close()
            payload = json.dumps(result, ensure_ascii=False, default=str)
            return _rpc_result(req_id, {"content": [{"type": "text", "text": payload}],
                                        "isError": False})
        except core.PortalError as e:
            return _tool_error(req_id, e.message, e.code)
        except Exception as e:  # unexpected: still a structured tool error, never a crash
            return _tool_error(req_id, str(e), "internal_error")
    if is_notification:
        return None
    return _rpc_error(req_id, -32601, f"method not found: {method}")


def _default_conn_factory():
    conn = core.connect()
    core.init_db(conn)
    return conn


def serve(stdin=None, stdout=None, conn_factory=None, token: str | None = None) -> int:
    """Run the stdio loop: one JSON-RPC message per line."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    conn_factory = conn_factory or _default_conn_factory
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_rpc_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        response = dispatch(request, conn_factory, token=token)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:
                pass
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
