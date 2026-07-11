#!/usr/bin/env python3
"""portal_admin.py — operator CLI for the session portal.

A thin, human-facing wrapper over portal_core / portal_adapters for health checks,
session registration, manual send/inbox/ack (used by the controlled forward test),
stale-lock recovery, TTL sweeps, and reversible uninstall. Everything prints JSON so it
is scriptable. It never writes a transcript or resumes a live session.

Examples:
  python portal_admin.py health
  python portal_admin.py register --product claude --session <id> --label "demo"
  python portal_admin.py send --from codex:A --to claude:B --body "hi" --authorship user
  python portal_admin.py inbox --session claude:B --deliver --boundary stop
  python portal_admin.py ack --message <id> --by claude:B
  python portal_admin.py events --message <id>
  python portal_admin.py recover-locks
  python portal_admin.py uninstall --yes        # removes the DB (reversible: just re-init)

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
                                          label=args.label, accepts_steering=args.accepts_steering))


def cmd_list_sessions(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.list_sessions(conn, product=args.product,
                                       registered_only=args.registered_only))


def cmd_send(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.send_message(conn, args.from_session, args.to_session, args.body,
                                      authorship=args.authorship, kind=args.kind,
                                      authorized=args.authorized, idempotency_key=args.key,
                                      ttl_seconds=args.ttl, forward_of=args.forward_of))


def cmd_inbox(args) -> int:
    with _closing(_conn()) as conn:
        return _out(core.list_inbox(conn, args.session, status=args.status, deliver=args.deliver,
                                    boundary=args.boundary, max_deliver=args.max_deliver))


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
    r.add_argument("--accepts-steering", action="store_true", dest="accepts_steering")
    r.set_defaults(func=cmd_register)

    ls = sub.add_parser("list-sessions")
    ls.add_argument("--product", choices=core.VALID_PRODUCTS)
    ls.add_argument("--registered-only", action="store_true", dest="registered_only")
    ls.set_defaults(func=cmd_list_sessions)

    s = sub.add_parser("send")
    s.add_argument("--from", dest="from_session", required=True, help="source session_id (product:id)")
    s.add_argument("--to", dest="to_session", required=True, help="dest session_id (product:id)")
    s.add_argument("--body", required=True)
    s.add_argument("--authorship", required=True, choices=core.VALID_AUTHORSHIP)
    s.add_argument("--kind", default="note", choices=core.VALID_KINDS)
    s.add_argument("--authorized", action="store_true")
    s.add_argument("--key", help="idempotency key")
    s.add_argument("--ttl", type=int, help="ttl seconds")
    s.add_argument("--forward-of", dest="forward_of")
    s.set_defaults(func=cmd_send)

    i = sub.add_parser("inbox")
    i.add_argument("--session", required=True, help="dest session_id")
    i.add_argument("--status")
    i.add_argument("--deliver", action="store_true")
    i.add_argument("--boundary", choices=sorted(core.SAFE_PULL_BOUNDARIES))
    i.add_argument("--max-deliver", type=int, dest="max_deliver")
    i.set_defaults(func=cmd_inbox)

    a = sub.add_parser("ack")
    a.add_argument("--message", required=True)
    a.add_argument("--by")
    a.add_argument("--note")
    a.set_defaults(func=cmd_ack)

    c = sub.add_parser("cancel")
    c.add_argument("--message", required=True)
    c.add_argument("--by")
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
