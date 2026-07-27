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
FINDING_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{3}$", re.ASCII)

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

KNOWN_STATUSES = TERMINAL_STATUSES | {STATUS_PENDING, STATUS_IN_PROGRESS}


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
            with path.open(encoding="utf-8-sig", errors="replace") as f:
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


def is_open_status(status, include_shipped):
    """Whether a row with this status belongs in a "still open" report.

    Deliberately a DENY-list. Only the four terminal statuses close a finding;
    everything else is treated as open and surfaced -- a typo (`in_progress`),
    a different case (`Pending`), an empty or missing value, a hand-edited
    non-string. An allow-list of {pending, in-progress} made a genuinely open
    finding vanish on a one-character typo, with no warning and no count, which
    is the exact failure this file exists to prevent. Unknown means open.
    """
    if status == STATUS_SHIPPED:
        return include_shipped
    return status not in TERMINAL_STATUSES


def ids_on_supersedes_cycles(edges):
    """Finding ids sitting on a supersedes cycle. `edges` maps id -> set(ids).

    Two rows that supersede each other close each other, so BOTH vanish and no
    successor survives. Iterative DFS (no recursion limit to trip over) marking
    every node reachable on a back-edge.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color = {}
    on_cycle = set()
    for root in sorted(edges):
        if color.get(root, WHITE) != WHITE:
            continue
        color[root] = GREY
        path = [root]
        stack = [(root, iter(sorted(edges.get(root, ()))))]
        while stack:
            node, successors = stack[-1]
            nxt = next(successors, None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                path.pop()
                continue
            state = color.get(nxt, WHITE)
            if state == GREY:
                on_cycle.update(path[path.index(nxt):])
            elif state == WHITE:
                color[nxt] = GREY
                path.append(nxt)
                stack.append((nxt, iter(sorted(edges.get(nxt, ())))))
    return on_cycle


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
    edges = {}
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
        edges.setdefault(key, {}).setdefault(own, set()).add(sup)

    out = {}
    for key, per_registry in edges.items():
        cyclic = ids_on_supersedes_cycles(per_registry)
        if cyclic:
            print(
                "warn: supersedes cycle among " + ", ".join(sorted(cyclic))
                + " in " + str(key) + " -- ignoring their links so none is "
                "hidden; fix the ledger",
                file=sys.stderr,
            )
        for own, targets in per_registry.items():
            if own in cyclic:
                # Fail OPEN: a cycle has no surviving successor, so honoring
                # its links would hide every finding in it.
                continue
            out.setdefault(key, set()).update(targets)
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
    unknown_statuses = set()
    malformed = {}
    for key, row in pairs:
        if finding_id_of(row) is None:
            # Not a finding: no id to track it by, and it would render as a
            # blank table row. Warn rather than drop silently -- and warn
            # rather than invent an "open finding" the ledger does not have.
            malformed[key] = malformed.get(key, 0) + 1
            continue

        status = text_field(row, "follow_up_status")
        closed = is_superseded(key, row, superseded)

        past_due, days_overdue = classify(row, asof, closed=closed)

        if status not in KNOWN_STATUSES:
            unknown_statuses.add(status)

        keep = is_open_status(status, include_shipped)
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

    for status in sorted(unknown_statuses):
        print(
            "warn: follow_up_status " + repr(status) + " is not one of "
            + ", ".join(sorted(KNOWN_STATUSES))
            + " -- treated as OPEN and listed; fix the ledger",
            file=sys.stderr,
        )
    for key, count in sorted(malformed.items()):
        print(
            "warn: skipped " + str(count) + " row(s) with no well-formed "
            "finding_id in " + str(key) + " -- not listed; fix the ledger",
            file=sys.stderr,
        )
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


def suppression_note(suppressed, shown):
    """One wording for both output formats. Empty when nothing was closed."""
    if not suppressed:
        return ""
    if shown:
        return (
            "{} row(s) below are closed by a supersedes link and are shown only "
            "because --include-superseded was passed.".format(suppressed)
        )
    return (
        "{} row(s) closed by a supersedes link (re-run with "
        "--include-superseded to see them).".format(suppressed)
    )


def summary_line(rows, suppressed, shown=False):
    text = (
        "_" + str(len(rows)) + " row(s) -- past-due first, then in-progress, "
        "then pending."
    )
    note = suppression_note(suppressed, shown)
    if note:
        text += " " + note
    return text + "_"


def render_markdown(rows, asof, suppressed=0, shown=False):
    header = (
        "| finding_id | project | retro_date | category | status | "
        "target_date | days_overdue | claim |"
    )
    sep = "|---|---|---|---|---|---|---|---|"
    lines = [
        "# Retro follow-up -- as of " + asof.isoformat(),
        "",
        summary_line(rows, suppressed, shown),
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
        note = suppression_note(suppressed, args.include_superseded)
        if note:
            print("note: " + note, file=sys.stderr)
        print(render_json(sorted_rows))
    else:
        print(
            render_markdown(sorted_rows, asof, suppressed, args.include_superseded),
            end="",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
