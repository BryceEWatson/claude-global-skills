#!/usr/bin/env python3
"""portal_admin.py — operator CLI for the session portal.

This is the OPERATOR's tool: it is the root of trust that mints principal tokens and issues
capability grants. Run locally by a human. It also offers health checks, discovery, manual
(operator-privileged) send/inbox/ack for debugging, stale-lock recovery, TTL sweeps, and
reversible uninstall. Everything prints JSON so it is scriptable. It never writes a
transcript or resumes a live session.

Identity model:
  issue-principal   mint a bearer token for a session (shown ONCE); put it in that session's
                    MCP config as SESSION_PORTAL_TOKEN. The MCP server derives all identity
                    from it — sessions can no longer assert who they are.
  grant             issue an operator capability (send-steer / accept-steering /
                    speak-as-user), scoped + expiring + revocable. Replaces the old caller
                    `--authorized` / `--accepts-steering` booleans.

Examples:
  python portal_admin.py issue-principal --product claude --session <id> --label demo
  python portal_admin.py grant --to claude:B --capability accept-steering --scope codex:A --ttl 3600
  python portal_admin.py send --from codex:A --to claude:B --body "hi" --authorship agent   # operator-privileged
  python portal_admin.py inbox --session claude:B --deliver --at-boundary --by claude:B
  python portal_admin.py health

stdlib only; Windows-safe.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import portal_core as core
import portal_adapters as adapters


def _out(obj) -> int:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    return 0


def _conn():
    conn = core.connect()
    core.init_db(conn)
    return conn


def cmd_health(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.health(conn))


def cmd_register(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.register_session(conn, args.product, args.session, cwd=args.cwd,
                                          label=args.label))


def cmd_issue_principal(args) -> int:
    with _closing(_conn()) as conn:
        res = core.issue_principal(conn, args.product, args.session, ttl_seconds=args.ttl,
                                   label=args.label)
        res["_warning"] = ("copy this token now — it is shown ONCE and stored only as a hash. "
                           "Put it in this session's MCP config as SESSION_PORTAL_TOKEN.")
        return _out(res)


def cmd_revoke_principal(args) -> int:
    with _closing(_conn()) as conn:
        return _out({"revoked": core.revoke_principal(conn, args.session), "session": args.session})


def cmd_list_principals(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.list_principals(conn))


def cmd_grant(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.grant_capability(conn, args.to_session, args.capability,
                                          scope=args.scope, ttl_seconds=args.ttl))


def cmd_revoke_grant(args) -> int:
    with _closing(_conn()) as conn:
        return _out({"revoked": core.revoke_grant(conn, args.id), "id": args.id})


def cmd_list_grants(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.list_grants(conn, grantee=args.grantee,
                                     include_inactive=args.include_inactive))


def cmd_list_sessions(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.list_sessions(conn, product=args.product,
                                       registered_only=args.registered_only))


def cmd_send(args) -> int:
    """Operator-privileged send: the operator asserts the source identity (it is the root of
    trust). Steering / user-authorship still require a live grant on the source."""
    with _closing(_conn()) as conn:
        return _out(core.send_message(conn, args.from_session, args.to_session, args.body,
                                      authorship=args.authorship, kind=args.kind,
                                      idempotency_key=args.key, ttl_seconds=args.ttl,
                                      forward_of=args.forward_of))


def cmd_inbox(args) -> int:
    """Operator-privileged inbox drain. --at-boundary is the operator vouching that the
    recipient is genuinely between turns (the MCP path derives this from runtime evidence;
    here the human asserts it explicitly)."""
    with _closing(_conn()) as conn:
        return _out(core.list_inbox(conn, args.session, status=args.status, deliver=args.deliver,
                                    at_boundary=args.at_boundary, boundary_reason="operator-asserted",
                                    max_deliver=args.max_deliver))


def cmd_ack(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.acknowledge(conn, args.message, by=args.by, note=args.note))


def cmd_cancel(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.cancel_message(conn, args.message, by=args.by, reason=args.reason))


def cmd_fail(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.fail_message(conn, args.message, error=args.error))


def cmd_status(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.get_message_status(conn, args.message))


def cmd_events(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.message_events(conn, args.message))


def cmd_expire(args) -> int:
    with _closing(_conn()) as conn:
        return _out({"expired": core.expire_due(conn)})


def cmd_recover_locks(args) -> int:
    with _closing(_conn()) as conn:
        return _out({"reclaimed_leases": core.reclaim_expired_leases(conn)})


def cmd_codex_status(args) -> int:
    return _out(adapters.codex_native_status())


def cmd_uninstall(args) -> int:
    """Remove the portal database (reversible: the next command re-creates an empty one).
    Refuses without --yes so it is never accidental."""
    if not args.yes:
        return _out({"ok": False, "note": "pass --yes to remove the DB", "db_path": str(core.db_path())})
    p = core.db_path()
    removed = []
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
            removed.append(str(f))
    return _out({"ok": True, "removed": removed})


class _closing:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()
        return False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="portal_admin.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("health").set_defaults(func=cmd_health)

    r = sub.add_parser("register")
    r.add_argument("--product", required=True, choices=core.VALID_PRODUCTS)
    r.add_argument("--session", required=True, help="runtime session id")
    r.add_argument("--cwd")
    r.add_argument("--label")
    r.set_defaults(func=cmd_register)

    ip = sub.add_parser("issue-principal", help="mint a bearer token for a session (shown once)")
    ip.add_argument("--product", required=True, choices=core.VALID_PRODUCTS)
    ip.add_argument("--session", required=True, help="runtime session id")
    ip.add_argument("--ttl", type=int, help="token lifetime seconds (default 12h)")
    ip.add_argument("--label")
    ip.set_defaults(func=cmd_issue_principal)

    rp = sub.add_parser("revoke-principal")
    rp.add_argument("--session", required=True, help="session_id (product:id)")
    rp.set_defaults(func=cmd_revoke_principal)

    sub.add_parser("list-principals").set_defaults(func=cmd_list_principals)

    g = sub.add_parser("grant", help="issue an operator capability grant")
    g.add_argument("--to", dest="to_session", required=True, help="grantee session_id (product:id)")
    g.add_argument("--capability", required=True, choices=core.VALID_CAPABILITIES)
    g.add_argument("--scope", default="*", help="counterparty session_id, or * for any (default *)")
    g.add_argument("--ttl", type=int, help="grant lifetime seconds (default 1h)")
    g.set_defaults(func=cmd_grant)

    rg = sub.add_parser("revoke-grant")
    rg.add_argument("--id", type=int, required=True)
    rg.set_defaults(func=cmd_revoke_grant)

    lg = sub.add_parser("list-grants")
    lg.add_argument("--grantee")
    lg.add_argument("--include-inactive", action="store_true", dest="include_inactive")
    lg.set_defaults(func=cmd_list_grants)

    ls = sub.add_parser("list-sessions")
    ls.add_argument("--product", choices=core.VALID_PRODUCTS)
    ls.add_argument("--registered-only", action="store_true", dest="registered_only")
    ls.set_defaults(func=cmd_list_sessions)

    s = sub.add_parser("send", help="operator-privileged send (asserts source identity)")
    s.add_argument("--from", dest="from_session", required=True, help="source session_id (product:id)")
    s.add_argument("--to", dest="to_session", required=True, help="dest session_id (product:id)")
    s.add_argument("--body", required=True)
    s.add_argument("--authorship", default="agent", choices=core.VALID_AUTHORSHIP)
    s.add_argument("--kind", default="note", choices=core.VALID_KINDS)
    s.add_argument("--key", help="idempotency key")
    s.add_argument("--ttl", type=int, help="ttl seconds")
    s.add_argument("--forward-of", dest="forward_of")
    s.set_defaults(func=cmd_send)

    i = sub.add_parser("inbox", help="operator-privileged inbox drain")
    i.add_argument("--session", required=True, help="dest session_id")
    i.add_argument("--status")
    i.add_argument("--deliver", action="store_true")
    i.add_argument("--at-boundary", action="store_true", dest="at_boundary",
                   help="operator vouches the recipient is between turns")
    i.add_argument("--max-deliver", type=int, dest="max_deliver")
    i.set_defaults(func=cmd_inbox)

    a = sub.add_parser("ack")
    a.add_argument("--message", required=True)
    a.add_argument("--by", required=True, help="recipient session_id acknowledging")
    a.add_argument("--note")
    a.set_defaults(func=cmd_ack)

    c = sub.add_parser("cancel")
    c.add_argument("--message", required=True)
    c.add_argument("--by", required=True, help="a party (source/dest) session_id")
    c.add_argument("--reason")
    c.set_defaults(func=cmd_cancel)

    f = sub.add_parser("fail")
    f.add_argument("--message", required=True)
    f.add_argument("--error", required=True, help="reason the message failed")
    f.set_defaults(func=cmd_fail)

    st = sub.add_parser("status")
    st.add_argument("--message", required=True)
    st.set_defaults(func=cmd_status)

    ev = sub.add_parser("events")
    ev.add_argument("--message", required=True)
    ev.set_defaults(func=cmd_events)

    sub.add_parser("expire").set_defaults(func=cmd_expire)
    sub.add_parser("recover-locks").set_defaults(func=cmd_recover_locks)
    sub.add_parser("codex-status").set_defaults(func=cmd_codex_status)

    u = sub.add_parser("uninstall")
    u.add_argument("--yes", action="store_true")
    u.set_defaults(func=cmd_uninstall)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except core.PortalError as e:
        print(json.dumps({"error": e.message, "code": e.code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
