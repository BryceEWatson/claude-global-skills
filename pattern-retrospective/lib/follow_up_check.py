#!/usr/bin/env python3
"""Surface pending / in-progress / past-due retro findings at the start of a retro.

Scans one project registry (default) or all registries matching the convention
glob and prints a markdown table or JSON array. Always exits 0 -- this is a
report, not a gate.

Registry resolution precedence:
    1. --registries <comma-separated-paths>  (explicit override)
    2. --all                                  (convention-glob across projects)
    3. --project-root <path>                  (single project)
    4. (default) nearest ancestor of pwd containing .git/

Convention glob: C:/Users/Bryce/Projects/*/reports/_data/retro-findings.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

CONVENTION_GLOB = "C:/Users/Bryce/Projects/*/reports/_data/retro-findings.jsonl"
REGISTRY_REL = Path("reports") / "_data" / "retro-findings.jsonl"

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in-progress"
STATUS_SHIPPED = "shipped"
STATUS_ABANDONED = "abandoned"
STATUS_SUPERSEDED = "superseded"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = {
    STATUS_SHIPPED,
    STATUS_ABANDONED,
    STATUS_SUPERSEDED,
    STATUS_CANCELLED,
}


def find_project_root_from_cwd() -> Path:
    """Walk up from cwd looking for a .git/ directory."""
    cur = Path.cwd().resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    print(
        "error: no --project-root, --all, or --registries given and no .git/ "
        "ancestor found from cwd",
        file=sys.stderr,
    )
    sys.exit(2)


def resolve_registries(args):
    """Return the list of registry paths to scan, per precedence rules."""
    if args.registries:
        paths = [Path(p.strip()) for p in args.registries.split(",") if p.strip()]
        return paths
    if args.all:
        return [Path(p) for p in sorted(glob.glob(CONVENTION_GLOB))]
    if args.project_root:
        return [Path(args.project_root) / REGISTRY_REL]
    return [find_project_root_from_cwd() / REGISTRY_REL]


def parse_asof(s):
    if s is None:
        return datetime.now(timezone.utc).date()
    try:
        return date.fromisoformat(s)
    except ValueError as exc:
        print("error: --asof must be YYYY-MM-DD (" + str(exc) + ")", file=sys.stderr)
        sys.exit(2)


def parse_target_date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def iter_rows(paths):
    """Stream rows from each registry line-by-line; warn on malformed lines."""
    for path in paths:
        if not path.exists():
            print("warn: registry not found: " + str(path), file=sys.stderr)
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        print(
                            "warn: malformed JSON at " + str(path) + ":" + str(lineno) + ": " + str(exc),
                            file=sys.stderr,
                        )
                        continue
                    if not isinstance(row, dict):
                        print(
                            "warn: non-object row at " + str(path) + ":" + str(lineno),
                            file=sys.stderr,
                        )
                        continue
                    yield row
        except OSError as exc:
            print("warn: could not read " + str(path) + ": " + str(exc), file=sys.stderr)


def classify(row, asof):
    """Return (is_past_due, days_overdue)."""
    status = (row.get("follow_up_status") or "").strip()
    td = parse_target_date(row.get("target_date"))
    if td is None:
        return False, 0
    if status in TERMINAL_STATUSES:
        return False, 0
    if td < asof:
        return True, (asof - td).days
    return False, 0


def filter_rows(rows, asof, include_shipped):
    out = []
    for row in rows:
        status = (row.get("follow_up_status") or "").strip()
        past_due, days_overdue = classify(row, asof)

        keep = False
        if past_due:
            keep = True
        elif status in (STATUS_PENDING, STATUS_IN_PROGRESS):
            keep = True
        elif include_shipped and status == STATUS_SHIPPED:
            keep = True

        if not keep:
            continue

        annotated = dict(row)
        annotated["_past_due"] = past_due
        annotated["_days_overdue"] = days_overdue
        out.append(annotated)
    return out


def sort_rows(rows):
    """Sort: past-due first (most overdue first), then in-progress, then pending (oldest first)."""
    def key(r):
        status = (r.get("follow_up_status") or "").strip()
        past_due = bool(r.get("_past_due"))
        if past_due:
            return (0, -int(r.get("_days_overdue", 0)), r.get("retro_date", ""), r.get("finding_id", ""))
        if status == STATUS_IN_PROGRESS:
            bucket = 1
        elif status == STATUS_PENDING:
            bucket = 2
        else:
            bucket = 3
        return (bucket, 0, r.get("retro_date", ""), r.get("finding_id", ""))

    return sorted(rows, key=key)


def truncate(s, n):
    s = (s or "").replace("\n", " ").replace("\r", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def md_escape(s):
    return (s or "").replace("|", "\\|")


def render_markdown(rows, asof):
    header = (
        "| finding_id | project | retro_date | category | status | "
        "target_date | days_overdue | claim |"
    )
    sep = "|---|---|---|---|---|---|---|---|"
    lines = [
        "# Retro follow-up -- as of " + asof.isoformat(),
        "",
        "_" + str(len(rows)) + " row(s) -- past-due first, then in-progress, then pending._",
        "",
        header,
        sep,
    ]
    if not rows:
        lines.append("| _(no rows)_ |  |  |  |  |  |  |  |")
        return "\n".join(lines) + "\n"

    for r in rows:
        td = r.get("target_date") or ""
        days = r.get("_days_overdue", 0) or 0
        days_str = str(days) if days else ""
        cells = [
            r.get("finding_id", ""),
            r.get("project", ""),
            r.get("retro_date", ""),
            r.get("category", ""),
            r.get("follow_up_status", ""),
            td,
            days_str,
            truncate(r.get("claim", ""), 80),
        ]
        lines.append("| " + " | ".join(md_escape(str(x)) for x in cells) + " |")
    return "\n".join(lines) + "\n"


def render_json(rows):
    out = []
    for r in rows:
        out.append(
            {
                "finding_id": r.get("finding_id"),
                "project": r.get("project"),
                "retro_date": r.get("retro_date"),
                "category": r.get("category"),
                "claim": r.get("claim"),
                "follow_up_status": r.get("follow_up_status"),
                "target_date": r.get("target_date"),
                "days_overdue": int(r.get("_days_overdue", 0) or 0),
                "proposed_action": r.get("proposed_action"),
            }
        )
    return json.dumps(out, indent=2, ensure_ascii=False)


def build_argparser():
    p = argparse.ArgumentParser(
        prog="follow_up_check.py",
        description=(
            "Scan retro-findings registry(ies) and report pending / in-progress / "
            "past-due findings. Always exits 0."
        ),
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="Scan only this project's registry. Default if neither --all nor --registries: nearest .git/ ancestor of pwd.",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="Scan all registries via convention glob: " + CONVENTION_GLOB,
    )
    scope.add_argument(
        "--registries",
        type=str,
        default=None,
        help="Comma-separated explicit paths to registry files (overrides convention).",
    )
    p.add_argument(
        "--include-shipped",
        action="store_true",
        help="Include rows with follow_up_status: shipped in output (default: only pending / in-progress / past-due).",
    )
    p.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format (default: markdown).",
    )
    p.add_argument(
        "--asof",
        type=str,
        default=None,
        help="'Today' date for past-due calculation, YYYY-MM-DD. Default: today's UTC date.",
    )
    return p


def main(argv=None):
    args = build_argparser().parse_args(argv)
    asof = parse_asof(args.asof)

    paths = resolve_registries(args)
    if not paths:
        print("warn: no registries resolved", file=sys.stderr)
        if args.format == "json":
            print("[]")
        else:
            print(render_markdown([], asof), end="")
        return 0

    existing = [p for p in paths if p.exists()]
    if not existing:
        print("warn: no registry files found (looked at " + str(len(paths)) + " path(s))", file=sys.stderr)
        if args.format == "json":
            print("[]")
        else:
            print(render_markdown([], asof), end="")
        return 0

    rows = list(iter_rows(paths))
    filtered = filter_rows(rows, asof, include_shipped=args.include_shipped)
    sorted_rows = sort_rows(filtered)

    if args.format == "json":
        print(render_json(sorted_rows))
    else:
        print(render_markdown(sorted_rows, asof), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
