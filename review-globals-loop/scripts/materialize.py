#!/usr/bin/env python3
"""materialize.py - privacy-guarded JSON materializer for agent-written corpus
artifacts.

The orchestrating agent runs this skill with Bash but NO Edit/Write tools, and
must write several intermediate JSON files that have NO emitting helper:
`fleet-frictions.json` (Step 3), `proposals.json` (Step 6), the per-batch
`claims-pack-b<b>.json` slices and `findings-captured.json` (Step 8), and the
per-survivor proposal files fed to `ledger_store.py upsert` (Step 9b). Every one
of these carries VERBATIM mined cross-project operator turns (a friction's /
proposal's `evidence[].text` IS the verbatim turn). Writing them via raw Bash
redirection (`> file`, `python -c`, a heredoc) would bypass the shared privacy
guard entirely, so an agent that wrote one into the cwd (a tracked/remoted repo)
would leak private cross-project corpus into a committable tree.

This helper closes that gap so the SKILL.md "helpers refuse to write any
corpus/evidence artifact to an unsafe path" guarantee actually covers the
agent-written files too: it reads JSON from stdin and writes it to --out ONLY
after routing the resolved --out through the shared, fail-closed
`_guards.assert_safe_out` (exit 7) — identical refusal behavior to
corpus_retrieve.py and claimpack.py: it refuses any path inside the ~/.claude
config tree EXCEPT this skill's own .local-state/, refuses any path inside a git
working tree, and refuses on any error. So a stray --out can never leak private
turns into a tracked repo or into ~/.claude config.

Stdlib only. UTF-8 in/out so verbatim turns with em-dashes / smart quotes /
arrows are preserved and don't crash on a cp1252 (Windows) machine.

Usage:
    <produce JSON on stdout> | python <SKILL_ROOT>/scripts/materialize.py \
        --out <SKILL_ROOT>/.local-state/runs/<id>/proposals.json

Exit codes:
    0 ok
    2 usage error: missing/invalid --out (argparse)
    5 stdin was empty or not valid JSON
    7 refused: unsafe --out (inside ~/.claude config tree or a git working tree,
      or safety could not be proven) — shared privacy guard
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Shared fail-closed privacy guard (single source of truth). scripts/ is one
# level up from lib/ — mirror corpus_retrieve.py's import bootstrap exactly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import _guards  # noqa: E402

EXIT_OK = 0
EXIT_ARGS = 5


def eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="materialize.py",
        description="Read JSON from stdin and write it to --out, but ONLY after "
                    "the privacy guard proves --out is safe (fail-closed exit 7).",
    )
    p.add_argument(
        "--out", required=True,
        help="REQUIRED: write the JSON here; refuses a git-tree or ~/.claude "
             "config-tree path, except this skill's .local-state/ (privacy guard).",
    )
    return p.parse_args(argv)


def main(argv):
    # Machine default may be cp1252 -> force utf-8 on every stream so verbatim
    # corpus with em-dash / smart-quote / arrow is read and written intact.
    for s in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:
                pass

    args = parse_args(argv)

    # Privacy guard runs UNCONDITIONALLY and BEFORE we consume stdin or touch the
    # filesystem: a leak destination must write nothing. Fail-closed shared guard
    # refuses ~/.claude (except this skill's .local-state/) and any git working
    # tree, and refuses on any error (exit 7).
    out_path = _guards.assert_safe_out(args.out)

    # Read verbatim JSON from stdin (utf-8). Decode from the raw buffer so the
    # encoding is explicit regardless of how the inherited stdin was configured.
    try:
        # decode utf-8-sig so a leading BOM is tolerated — the same "tolerate a
        # BOM" idiom the other helpers get from their encoding="utf-8-sig" reads.
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
    except Exception as e:
        eprint(f"error: could not read utf-8 JSON from stdin: {e}")
        return EXIT_ARGS

    if not raw.strip():
        eprint("error: stdin was empty; expected a JSON document to materialize")
        return EXIT_ARGS

    # Validate it parses — a malformed heredoc/redirection fails loudly here
    # rather than silently writing corrupt JSON that breaks a downstream helper.
    # (A leading BOM was already stripped by the utf-8-sig decode above.)
    try:
        data = json.loads(raw)
    except Exception as e:
        eprint(f"error: stdin was not valid JSON: {e}")
        return EXIT_ARGS

    # Write to the guard-resolved path only (utf-8, ensure_ascii=False so the
    # verbatim turns stay verbatim — same convention as claimpack.py).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    kind = type(data).__name__
    n = len(data) if isinstance(data, (list, dict)) else 1
    eprint(f"wrote {out_path} ({kind}, {n} top-level item{'s' if n != 1 else ''})")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        eprint(f"error: {e}")
        sys.exit(1)
