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

Convention glob: ~/Projects/*/reports/_data/retro-findings.jsonl

Closure by supersedes link
--------------------------
The registry is append-only (see lib/README.md), so a resolved finding is
retracted by appending a successor row carrying ``supersedes: <old-id>`` -- the
old row keeps whatever ``follow_up_status`` it was written with, forever. This
reader therefore derives closure: a row that another row in the SAME registry
supersedes is treated exactly as ``follow_up_status: superseded`` and is not
listed (and never counted past-due). Without this, every superseded finding is
reported as permanently overdue, which trains the reader to ignore the report.

Scoping matters: ``finding_id`` is date-derived (``YYYY-MM-DD-NNN``) and NOT
project-qualified, so the same id can exist in two projects' registries. The
supersedes set is keyed per resolved registry path so a link in one project can
never close a same-numbered finding in another.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

CONVENTION_GLOB = str(Path.home() / "Projects" / "*" / "reports" / "_data" / "retro-findings.jsonl")
REGISTRY_REL = Path("reports") / "_data" / "retro-findings.jsonl"

# Same shape register_finding.py writes and validates against _schema.json.
FINDING_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{3}$")

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


def dedupe_paths(paths):
    """Drop repeats of the same file, keeping the first spelling of each.

    `--registries a.jsonl,./a.jsonl` names one registry twice; reading it twice
    would double every row and every suppression count.
    """
    seen = set()
    out = []
    for p in paths:
        key = registry_key(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def resolve_registries(args):
    """Return the list of registry paths to scan, per precedence rules."""
    if args.registries:
        paths = [Path(p.strip()) for p in args.registries.split(",") if p.strip()]
        return dedupe_paths(paths)
    if args.all:
        return dedupe_paths([Path(p) for p in sorted(glob.glob(CONVENTION_GLOB))])
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


def registry_key(path):
    """Stable identity for one registry file, so two paths to the same file agree."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def iter_rows(paths):
    """Stream (registry_key, row) from each registry; warn on malformed lines."""
    for path in paths:
        if not path.exists():
            print("warn: registry not found: " + str(path), file=sys.stderr)
            continue
        key = registry_key(path)
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError as exc:
                        # ValueError, not JSONDecodeError (which subclasses it):
                        # an integer past CPython's 4300-digit conversion limit
                        # raises a plain ValueError, and one bad line must never
                        # take down the whole report.
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
                    yield key, row
        except OSError as exc:
            print("warn: could not read " + str(path) + ": " + str(exc), file=sys.stderr)


def text_field(row, key):
    """A row field as a stripped string, whatever junk the row actually holds.

    Rows are read from a file that may have been hand-edited, so a field can be
    any JSON type. `iter_rows` already warns-and-continues on a malformed line;
    a malformed *field* must not do worse and crash the whole report.
    """
    value = row.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value).strip()
    return value.strip()


def finding_id_of(row):
    """The row's finding_id, or None if it isn't a well-formed one.

    Well-formed means `YYYY-MM-DD-NNN` -- what register_finding.py writes and
    what `_schema.json` requires. Anything else is not a finding this reader
    will let participate in closing another row.
    """
    fid = row.get("finding_id")
    if not isinstance(fid, str):
        return None
    fid = fid.strip()
    return fid if FINDING_ID_RE.match(fid) else None


def collect_superseded(pairs):
    """Map registry_key -> set of finding_ids that some other row supersedes.

    Keyed per registry because finding_ids are date-derived, not project-scoped:
    two projects can both hold a `2026-07-03-002`.

    Closure only ever links two WELL-FORMED finding ids (`YYYY-MM-DD-NNN`, the
    shape `register_finding.py` writes). Hiding an open finding is the worst
    thing this reader can do, so a corrupt or hand-written object does not get
    to do it on the strength of a `supersedes` key alone -- it must itself look
    like a finding. A self-referential link is likewise ignored, so a single bad
    row cannot silence itself. Every rejected link warns rather than passing in
    silence: an unexplained disappearance is the same class of defect as the
    false-overdue this closure logic exists to fix.
    """
    out = {}
    for key, row in pairs:
        sup = row.get("supersedes")
        if not isinstance(sup, str):
            continue
        sup = sup.strip()
        if not sup:
            continue
        own = finding_id_of(row)
        if own is None:
            print(
                "warn: ignoring 'supersedes: " + sup + "' -- the row claiming it "
                "has no well-formed finding_id, in " + str(key),
                file=sys.stderr,
            )
            continue
        if not FINDING_ID_RE.match(sup):
            print(
                "warn: " + own + " supersedes '" + sup + "', which is not a "
                "well-formed finding_id, in " + str(key),
                file=sys.stderr,
            )
            continue
        if sup == own:
            print(
                "warn: ignoring self-referential 'supersedes' on " + own
                + " in " + str(key),
                file=sys.stderr,
            )
            continue
        out.setdefault(key, set()).add(sup)
    return out


def is_superseded(key, row, superseded):
    """True when another row in the same registry supersedes this one."""
    fid = finding_id_of(row)
    if fid is None:
        return False
    return fid in superseded.get(key, ())


def classify(row, asof, closed=False):
    """Return (is_past_due, days_overdue). A closed row is never past-due."""
    status = text_field(row, "follow_up_status")
    td = parse_target_date(row.get("target_date"))
    if td is None:
        return False, 0
    if closed or status in TERMINAL_STATUSES:
        return False, 0
    if td < asof:
        return True, (asof - td).days
    return False, 0


def filter_rows(pairs, asof, include_shipped, include_superseded=False):
    """Return (kept_rows, suppressed_count).

    `suppressed_count` counts rows that would otherwise have been listed but are
    closed by a supersedes link from another row in the same registry. It is
    reported so the suppression is visible rather than silent.
    """
    pairs = list(pairs)
    superseded = collect_superseded(pairs)

    out = []
    suppressed = 0
    for key, row in pairs:
        status = text_field(row, "follow_up_status")
        closed = is_superseded(key, row, superseded)

        past_due, days_overdue = classify(row, asof, closed=closed)

        keep = False
        if past_due:
            keep = True
        elif status in (STATUS_PENDING, STATUS_IN_PROGRESS):
            keep = True
        elif include_shipped and status == STATUS_SHIPPED:
            keep = True

        if not keep:
            continue
        if closed:
            suppressed += 1
            if not include_superseded:
                continue

        annotated = dict(row)
        annotated["_past_due"] = past_due
        annotated["_days_overdue"] = days_overdue
        annotated["_closed_by_supersedes"] = closed
        out.append(annotated)
    return out, suppressed


def sort_rows(rows):
    """Sort: past-due first (most overdue first), then in-progress, then pending (oldest first)."""
    def key(r):
        status = text_field(r, "follow_up_status")
        past_due = bool(r.get("_past_due"))
        if past_due:
            return (0, -int(r.get("_days_overdue", 0)), text_field(r, "retro_date"), text_field(r, "finding_id"))
        if status == STATUS_IN_PROGRESS:
            bucket = 1
        elif status == STATUS_PENDING:
            bucket = 2
        else:
            bucket = 3
        return (bucket, 0, text_field(r, "retro_date"), text_field(r, "finding_id"))

    return sorted(rows, key=key)


def truncate(s, n):
    s = (s or "").replace("\n", " ").replace("\r", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def md_escape(s):
    return (s or "").replace("|", "\\|")


def summary_line(rows, suppressed):
    text = (
        "_" + str(len(rows)) + " row(s) -- past-due first, then in-progress, "
        "then pending."
    )
    if suppressed:
        text += (
            " " + str(suppressed) + " row(s) closed by a supersedes link "
            "(re-run with --include-superseded to see them)."
        )
    return text + "_"


def render_markdown(rows, asof, suppressed=0):
    header = (
        "| finding_id | project | retro_date | category | status | "
        "target_date | days_overdue | claim |"
    )
    sep = "|---|---|---|---|---|---|---|---|"
    lines = [
        "# Retro follow-up -- as of " + asof.isoformat(),
        "",
        summary_line(rows, suppressed),
        "",
        header,
        sep,
    ]
    if not rows:
        lines.append("| _(no rows)_ |  |  |  |  |  |  |  |")
        return "\n".join(lines) + "\n"

    for r in rows:
        td = text_field(r, "target_date")
        days = r.get("_days_overdue", 0) or 0
        days_str = str(days) if days else ""
        status = text_field(r, "follow_up_status")
        if r.get("_closed_by_supersedes"):
            status = str(status) + " (closed: superseded)"
        cells = [
            text_field(r, "finding_id"),
            text_field(r, "project"),
            text_field(r, "retro_date"),
            text_field(r, "category"),
            status,
            td,
            days_str,
            truncate(text_field(r, "claim"), 80),
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
                "closed_by_supersedes": bool(r.get("_closed_by_supersedes")),
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
        "--include-superseded",
        action="store_true",
        help=(
            "Also list rows closed by a supersedes link from another row in the "
            "same registry (default: hidden, but counted in the summary line)."
        ),
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

    pairs = list(iter_rows(paths))
    filtered, suppressed = filter_rows(
        pairs,
        asof,
        include_shipped=args.include_shipped,
        include_superseded=args.include_superseded,
    )
    sorted_rows = sort_rows(filtered)

    if args.format == "json":
        if suppressed and not args.include_superseded:
            print(
                "note: " + str(suppressed) + " row(s) hidden -- closed by a "
                "supersedes link; re-run with --include-superseded to see them",
                file=sys.stderr,
            )
        print(render_json(sorted_rows))
    else:
        print(render_markdown(sorted_rows, asof, suppressed), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
