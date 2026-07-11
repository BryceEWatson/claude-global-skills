#!/usr/bin/env python3
"""portal_mcp.py — local stdio MCP server for the session portal.

Speaks newline-delimited JSON-RPC 2.0 over stdin/stdout (the MCP stdio transport). It
binds nothing to the network. Every tool validates its input against a JSON schema and
calls a portal_core function; message content is treated as untrusted data and is never
executed. The dispatch() function is pure (dict in, dict out) so the protocol layer is
tested without spawning a process.

Exposed tools (issue #16 semantics; repo-prefixed names):
    portal_list_sessions, portal_get_session, portal_register_session,
    portal_send_message, portal_list_inbox, portal_acknowledge,
    portal_cancel_message, portal_get_message_status, portal_message_events,
    portal_health

stdlib only; Windows-safe.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable

import portal_core as core

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "session-portal", "version": "1.0.0"}

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
          "Register or update a session so it can be discovered and addressed.",
          {"product": {"type": "string", "enum": list(core.VALID_PRODUCTS)},
           "runtime_session_id": _ID,
           "cwd": {"type": "string", "maxLength": 1024},
           "label": {"type": "string", "maxLength": core.MAX_LABEL_LEN},
           "accepts_steering": {"type": "boolean"}},
          ["product", "runtime_session_id"]),
    _tool("portal_list_sessions", "List known sessions (optionally filtered by product).",
          {"product": {"type": "string", "enum": list(core.VALID_PRODUCTS)},
           "registered_only": {"type": "boolean"}},
          []),
    _tool("portal_get_session", "Get one session's record.",
          {"session_id": _ID}, ["session_id"]),
    _tool("portal_send_message",
          "Queue a message to another session's inbox. Idempotent when idempotency_key is "
          "supplied. Steering requires explicit authorization. Content is untrusted data.",
          {"source_session_id": _ID, "dest_session_id": _ID,
           "body": {"type": "string", "maxLength": core.MAX_BODY_BYTES * 2},
           "authorship": {"type": "string", "enum": list(core.VALID_AUTHORSHIP)},
           "kind": {"type": "string", "enum": list(core.VALID_KINDS)},
           "authorized": {"type": "boolean"},
           "idempotency_key": _ID,
           "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 30 * 24 * 3600},
           "forward_of": _ID},
          ["source_session_id", "dest_session_id", "body", "authorship"]),
    _tool("portal_list_inbox",
          "List a session's inbox. With deliver=true this is the safe PULL at a boundary: "
          "queued messages become delivered at the given boundary.",
          {"dest_session_id": _ID,
           "status": {"type": "string", "enum": list(core.TERMINAL_STATES) +
                      [core.STATUS_QUEUED, core.STATUS_DELIVERED]},
           "deliver": {"type": "boolean"},
           "boundary": {"type": "string", "enum": sorted(core.SAFE_PULL_BOUNDARIES)},
           "max_deliver": {"type": "integer", "minimum": 1, "maximum": 1000}},
          ["dest_session_id"]),
    _tool("portal_acknowledge", "Acknowledge a delivered message (idempotent).",
          {"message_id": _ID, "by": _ID, "note": {"type": "string", "maxLength": 500}},
          ["message_id"]),
    _tool("portal_cancel_message", "Cancel a queued/delivered message before acknowledgement.",
          {"message_id": _ID, "by": _ID, "reason": {"type": "string", "maxLength": 500}},
          ["message_id"]),
    _tool("portal_get_message_status", "Get one message's current record.",
          {"message_id": _ID}, ["message_id"]),
    _tool("portal_message_events", "Get the full lifecycle audit trail for a message.",
          {"message_id": _ID}, ["message_id"]),
    _tool("portal_health", "Portal health: schema version, counts, leases, DB path.", {}, []),
]
_TOOL_NAMES = {t["name"] for t in TOOLS}


# --------------------------------------------------------------------------- #
# Tool handlers (each: (conn, args) -> json-able result)
# --------------------------------------------------------------------------- #
def _h_register(conn, a):
    return core.register_session(conn, a["product"], a["runtime_session_id"],
                                 cwd=a.get("cwd"), label=a.get("label"),
                                 accepts_steering=bool(a.get("accepts_steering", False)))


def _h_list_sessions(conn, a):
    return core.list_sessions(conn, product=a.get("product"),
                              registered_only=bool(a.get("registered_only", False)))


def _h_get_session(conn, a):
    return core.get_session(conn, a["session_id"])


def _h_send(conn, a):
    return core.send_message(
        conn, a["source_session_id"], a["dest_session_id"], a["body"],
        authorship=a["authorship"], kind=a.get("kind", "note"),
        authorized=bool(a.get("authorized", False)), idempotency_key=a.get("idempotency_key"),
        ttl_seconds=a.get("ttl_seconds"), forward_of=a.get("forward_of"))


def _h_list_inbox(conn, a):
    return core.list_inbox(conn, a["dest_session_id"], status=a.get("status"),
                           deliver=bool(a.get("deliver", False)), boundary=a.get("boundary"),
                           max_deliver=a.get("max_deliver"))


def _h_ack(conn, a):
    return core.acknowledge(conn, a["message_id"], by=a.get("by"), note=a.get("note"))


def _h_cancel(conn, a):
    return core.cancel_message(conn, a["message_id"], by=a.get("by"), reason=a.get("reason"))


def _h_status(conn, a):
    return core.get_message_status(conn, a["message_id"])


def _h_events(conn, a):
    return core.message_events(conn, a["message_id"])


def _h_health(conn, a):
    return core.health(conn)


HANDLERS: dict[str, Callable[[Any, dict], Any]] = {
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
    rather than silently flipping a security gate (authorized / accepts_steering) open."""
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
    prohibited fields out), declared type + bounds, and enum membership. No coercion — a
    type mismatch is a hard error. Deep semantic validation still lives in core."""
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


def dispatch(request: dict, conn_factory: Callable[[], Any]) -> dict | None:
    """Handle one JSON-RPC request dict. Returns a response dict, or None for
    notifications (no id). conn_factory yields an initialized DB connection."""
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
                result = HANDLERS[name](conn, args)
            finally:
                conn.close()
            payload = json.dumps(result, ensure_ascii=False, default=str)
            return _rpc_result(req_id, {"content": [{"type": "text", "text": payload}],
                                        "isError": False})
        except core.PortalError as e:
            payload = json.dumps({"error": e.message, "code": e.code}, ensure_ascii=False)
            return _rpc_result(req_id, {"content": [{"type": "text", "text": payload}],
                                        "isError": True})
        except Exception as e:  # unexpected: still a structured tool error, never a crash
            payload = json.dumps({"error": str(e), "code": "internal_error"})
            return _rpc_result(req_id, {"content": [{"type": "text", "text": payload}],
                                        "isError": True})
    if is_notification:
        return None
    return _rpc_error(req_id, -32601, f"method not found: {method}")


def _default_conn_factory():
    conn = core.connect()
    core.init_db(conn)
    return conn


def serve(stdin=None, stdout=None, conn_factory=None) -> int:
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
        response = dispatch(request, conn_factory)
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
