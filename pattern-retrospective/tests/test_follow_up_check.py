#!/usr/bin/env python3
"""Tests for follow_up_check.py -- closure of a finding by supersedes link.

The defect these cover: the registry is append-only, so a resolved finding is
retracted by appending a successor row carrying `supersedes: <old-id>`. Nothing
ever updated the superseded row, so it kept `follow_up_status: pending` forever
and was reported as permanently past-due. A report that flags completed work as
late trains the reader to ignore it.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))

import follow_up_check as fuc  # noqa: E402


def row(
    finding_id,
    status="pending",
    target_date=None,
    supersedes=None,
    project="demo",
    claim="A claim long enough to be realistic.",
):
    out = {
        "finding_id": finding_id,
        "retro_date": finding_id[:10],
        "retro_path": "research/studies/r.md",
        "project": project,
        "category": "demo",
        "claim": claim,
        "confidence": 0.6,
        "evidence_supporting": 3,
        "evidence_contradicting": 0,
        "proposed_action": "do the thing",
        "target_date": target_date,
        "follow_up_status": status,
        "appended_at": "2026-01-01T00:00:00Z",
        "appended_by": "test",
    }
    if supersedes is not None:
        out["supersedes"] = supersedes
    return out


def write_registry(dirpath, rows, name="retro-findings.jsonl"):
    path = Path(dirpath) / name
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return path


def run(paths, asof, **kwargs):
    """filter_rows over real files; returns (ids_listed, suppressed_count)."""
    pairs = list(fuc.iter_rows([Path(p) for p in paths]))
    kept, suppressed = fuc.filter_rows(
        pairs,
        date.fromisoformat(asof),
        include_shipped=kwargs.pop("include_shipped", False),
        include_superseded=kwargs.pop("include_superseded", False),
    )
    assert not kwargs, "unexpected kwargs: {}".format(kwargs)
    return [r["finding_id"] for r in fuc.sort_rows(kept)], suppressed


class TestSupersedesClosure(unittest.TestCase):
    def test_superseded_row_is_not_listed_or_past_due(self):
        """The headline defect: a resolved finding reported as 27 days overdue."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-06-02-001", "pending", "2026-06-30"),
                    row("2026-07-03-001", "shipped", None, supersedes="2026-06-02-001"),
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(listed, [])
        self.assertEqual(suppressed, 1)

    def test_genuinely_open_findings_still_listed(self):
        """The fix must not silence everything -- open rows still surface."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-07-03-002", "pending", "2026-07-31"),
                    row(
                        "2026-07-03-003",
                        "pending",
                        "2026-07-31",
                        supersedes="2026-07-03-002",
                    ),
                    row("2026-07-27-004", "pending", "2026-08-31"),
                    row("2026-07-27-005", "in-progress", "2026-08-31"),
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(
            sorted(listed), ["2026-07-03-003", "2026-07-27-004", "2026-07-27-005"]
        )
        self.assertEqual(suppressed, 1)

    def test_open_past_due_row_still_reported_overdue(self):
        """Closure must not swallow a real overdue item."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(d, [row("2026-01-01-001", "pending", "2026-01-31")])
            pairs = list(fuc.iter_rows([reg]))
            kept, _ = fuc.filter_rows(
                pairs, date.fromisoformat("2026-02-10"), include_shipped=False
            )
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["_past_due"])
        self.assertEqual(kept[0]["_days_overdue"], 10)

    def test_chain_closes_every_ancestor(self):
        """A -> B -> C: only the newest tip stays open."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-01-01-001", "pending", "2026-01-31"),
                    row("2026-02-01-001", "pending", "2026-02-28", supersedes="2026-01-01-001"),
                    row("2026-03-01-001", "pending", "2026-12-31", supersedes="2026-02-01-001"),
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(listed, ["2026-03-01-001"])
        self.assertEqual(suppressed, 2)

    def test_include_superseded_shows_them_again(self):
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-06-02-001", "pending", "2026-06-30"),
                    row("2026-07-03-001", "pending", "2026-12-31", supersedes="2026-06-02-001"),
                ],
            )
            listed, suppressed = run([reg], "2026-07-27", include_superseded=True)
        self.assertEqual(sorted(listed), ["2026-06-02-001", "2026-07-03-001"])
        self.assertEqual(suppressed, 1)

    def test_superseded_row_shown_is_still_not_past_due(self):
        """--include-superseded reveals the row; it must not resurrect the false overdue."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-06-02-001", "pending", "2026-06-30"),
                    row("2026-07-03-001", "shipped", None, supersedes="2026-06-02-001"),
                ],
            )
            pairs = list(fuc.iter_rows([reg]))
            kept, _ = fuc.filter_rows(
                pairs,
                date.fromisoformat("2026-07-27"),
                include_shipped=False,
                include_superseded=True,
            )
        self.assertEqual(len(kept), 1)
        self.assertFalse(kept[0]["_past_due"])
        self.assertEqual(kept[0]["_days_overdue"], 0)
        self.assertTrue(kept[0]["_closed_by_supersedes"])


class TestScoping(unittest.TestCase):
    def test_link_does_not_close_same_id_in_another_registry(self):
        """finding_ids are date-derived, not project-scoped -- they collide across projects."""
        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / "a"
            b = Path(d) / "b"
            a.mkdir()
            b.mkdir()
            reg_a = write_registry(
                a,
                [
                    row("2026-07-03-002", "pending", "2026-07-31", project="alpha"),
                    row(
                        "2026-07-03-003",
                        "pending",
                        "2026-12-31",
                        supersedes="2026-07-03-002",
                        project="alpha",
                    ),
                ],
            )
            # Same finding_id, different project, nothing supersedes it there.
            reg_b = write_registry(
                b, [row("2026-07-03-002", "pending", "2026-07-31", project="beta")]
            )
            listed, suppressed = run([reg_a, reg_b], "2026-07-27")
        self.assertEqual(listed.count("2026-07-03-002"), 1, "beta's row must survive")
        self.assertEqual(suppressed, 1, "only alpha's row is closed")

    def test_same_file_named_twice_is_read_once(self):
        """Two spellings of one path must not double every row and every count."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-06-02-001", "pending", "2026-06-30"),
                    row("2026-07-03-001", "pending", "2026-12-31", supersedes="2026-06-02-001"),
                ],
            )
            (Path(d) / "sub").mkdir()
            indirect = Path(d) / "sub" / ".." / reg.name
            args = fuc.build_argparser().parse_args(
                ["--registries", "{},{}".format(reg, indirect)]
            )
            paths = fuc.resolve_registries(args)
            listed, suppressed = run(paths, "2026-07-27")
        self.assertEqual(len(paths), 1, "duplicate spellings collapse to one registry")
        self.assertEqual(listed, ["2026-07-03-001"])
        self.assertEqual(suppressed, 1)


class TestMalformedInput(unittest.TestCase):
    def test_self_supersede_does_not_close_itself(self):
        """Only reachable by hand-editing; a row must not silence itself."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [row("2026-01-01-001", "pending", "2026-01-31", supersedes="2026-01-01-001")],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(listed, ["2026-01-01-001"])
        self.assertEqual(suppressed, 0)

    def test_dangling_supersedes_target_is_harmless(self):
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [row("2026-07-03-003", "pending", "2026-07-31", supersedes="2020-01-01-999")],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(listed, ["2026-07-03-003"])
        self.assertEqual(suppressed, 0)

    def test_non_string_supersedes_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            bad = row("2026-07-03-003", "pending", "2026-07-31")
            bad["supersedes"] = 12345
            reg = write_registry(d, [row("2026-07-03-002", "pending", "2026-07-31"), bad])
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(sorted(listed), ["2026-07-03-002", "2026-07-03-003"])
        self.assertEqual(suppressed, 0)

    def test_link_from_a_row_with_no_finding_id_cannot_hide_an_open_finding(self):
        """Hiding an open finding is the worst outcome; a corrupt row must not cause it."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-01-01-001", "pending", "2026-01-31"),
                    {"supersedes": "2026-01-01-001"},
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(listed, ["2026-01-01-001"])
        self.assertEqual(suppressed, 0)

    def test_link_from_a_row_with_non_string_finding_id_does_not_crash(self):
        """A hand-edited `"finding_id": 123` used to raise AttributeError mid-report."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-01-01-001", "pending", "2026-01-31"),
                    {"finding_id": 123, "supersedes": "2026-01-01-001"},
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(listed, ["2026-01-01-001"])
        self.assertEqual(suppressed, 0)

    def test_link_from_an_ill_formed_finding_id_cannot_hide_an_open_finding(self):
        """A string id is not enough -- it must look like a finding id."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-01-01-001", "pending", "2026-01-31"),
                    {"finding_id": "x", "supersedes": "2026-01-01-001"},
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(listed, ["2026-01-01-001"])
        self.assertEqual(suppressed, 0)

    def test_link_to_an_ill_formed_target_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-01-01-001", "pending", "2026-01-31", supersedes="nonsense"),
                    row("2026-02-01-001", "pending", "2026-12-31"),
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(sorted(listed), ["2026-01-01-001", "2026-02-01-001"])
        self.assertEqual(suppressed, 0)

    def test_a_well_formed_row_with_non_string_finding_id_is_never_closed(self):
        with tempfile.TemporaryDirectory() as d:
            broken = row("2026-01-01-001", "pending", "2026-01-31")
            broken["finding_id"] = 123
            reg = write_registry(
                d,
                [
                    broken,
                    row("2026-02-01-001", "pending", "2026-12-31", supersedes="2026-01-01-001"),
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(suppressed, 0)
        # The row with the broken id is warned about, not listed; the row that
        # tried to supersede it is a real finding and stays open.
        self.assertEqual(listed, ["2026-02-01-001"])

    def test_null_supersedes_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            explicit_null = row("2026-07-03-003", "pending", "2026-07-31", supersedes=None)
            explicit_null["supersedes"] = None
            reg = write_registry(
                d, [row("2026-07-03-002", "pending", "2026-07-31"), explicit_null]
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(sorted(listed), ["2026-07-03-002", "2026-07-03-003"])
        self.assertEqual(suppressed, 0)


class TestMalformedFieldTypes(unittest.TestCase):
    """A malformed *field* must not do worse than a malformed *line*.

    `iter_rows` warns and continues past a bad line; before this, a bad field
    type took down the whole report with an AttributeError or TypeError.
    """

    def _render_end_to_end(self, rows, asof="2026-07-27"):
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(d, rows)
            pairs = list(fuc.iter_rows([reg]))
            kept, suppressed = fuc.filter_rows(
                pairs, date.fromisoformat(asof), include_shipped=False
            )
            ordered = fuc.sort_rows(kept)
            md = fuc.render_markdown(ordered, date.fromisoformat(asof), suppressed)
            js = json.loads(fuc.render_json(ordered))
        return md, js

    def test_non_string_status_does_not_crash(self):
        bad = row("2026-01-01-001", "pending", "2026-01-31")
        bad["follow_up_status"] = 123
        md, _ = self._render_end_to_end([bad])
        self.assertIn("2026-01-01-001", md)

    def test_non_string_claim_does_not_crash(self):
        bad = row("2026-01-01-001", "pending", "2026-01-31")
        bad["claim"] = 42
        md, _ = self._render_end_to_end([bad])
        self.assertIn("42", md)

    def test_mixed_type_sort_keys_do_not_crash(self):
        """Tuple comparison raises TypeError when one row's key is an int."""
        a = row("2026-01-01-001", "pending", "2026-01-31")
        b = row("2026-01-02-001", "pending", "2026-01-31")
        b["retro_date"] = 20260102
        md, js = self._render_end_to_end([a, b])
        self.assertEqual(len(js), 2)
        self.assertIn("2026-01-01-001", md)

    def test_non_string_category_and_project_do_not_crash(self):
        bad = row("2026-01-01-001", "pending", "2026-01-31")
        bad["category"] = None
        bad["project"] = 7
        md, _ = self._render_end_to_end([bad])
        self.assertIn("2026-01-01-001", md)


class TestUnknownStatusIsOpen(unittest.TestCase):
    """An unrecognized status must never be quieter than a recognized-open one.

    The allow-list this replaced kept only {pending, in-progress}, so a typo, a
    capitalization, an empty value or a hand-edited non-string made a genuinely
    open finding vanish with no warning and no suppressed count -- worse than the
    supersedes suppression this file is careful to count.
    """

    def _listed_with_status(self, status):
        with tempfile.TemporaryDirectory() as d:
            r = row("2026-07-01-001", "pending", "2026-12-31")
            r["follow_up_status"] = status
            reg = write_registry(d, [r])
            return run([reg], "2026-07-27")[0]

    def test_typo_status_is_still_listed(self):
        self.assertEqual(self._listed_with_status("in_progress"), ["2026-07-01-001"])

    def test_wrong_case_status_is_still_listed(self):
        self.assertEqual(self._listed_with_status("Pending"), ["2026-07-01-001"])

    def test_empty_status_is_still_listed(self):
        self.assertEqual(self._listed_with_status(""), ["2026-07-01-001"])

    def test_non_string_status_is_still_listed(self):
        """Coercing this to a string must not turn a loud crash into a silent hide."""
        self.assertEqual(self._listed_with_status(123), ["2026-07-01-001"])

    def test_missing_status_key_is_still_listed(self):
        with tempfile.TemporaryDirectory() as d:
            r = row("2026-07-01-001", "pending", "2026-12-31")
            del r["follow_up_status"]
            reg = write_registry(d, [r])
            listed, _ = run([reg], "2026-07-27")
        self.assertEqual(listed, ["2026-07-01-001"])

    def test_terminal_statuses_still_close(self):
        """The deny-list must not accidentally surface genuinely closed work."""
        for status in ("abandoned", "superseded", "cancelled", "shipped"):
            with self.subTest(status=status):
                self.assertEqual(self._listed_with_status(status), [])

    def test_shipped_still_honors_include_shipped(self):
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(d, [row("2026-07-01-001", "shipped", "2026-12-31")])
            listed, _ = run([reg], "2026-07-27", include_shipped=True)
        self.assertEqual(listed, ["2026-07-01-001"])


class TestSupersedesCycles(unittest.TestCase):
    """A cycle has no surviving successor, so honoring its links hides everything."""

    def test_mutual_supersedes_hides_neither(self):
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-07-01-001", "pending", "2026-12-31", supersedes="2026-07-01-002"),
                    row("2026-07-01-002", "pending", "2026-12-31", supersedes="2026-07-01-001"),
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(sorted(listed), ["2026-07-01-001", "2026-07-01-002"])
        self.assertEqual(suppressed, 0)

    def test_three_node_cycle_hides_none(self):
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-07-01-001", "pending", "2026-12-31", supersedes="2026-07-01-002"),
                    row("2026-07-01-002", "pending", "2026-12-31", supersedes="2026-07-01-003"),
                    row("2026-07-01-003", "pending", "2026-12-31", supersedes="2026-07-01-001"),
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertEqual(len(listed), 3)
        self.assertEqual(suppressed, 0)

    def test_a_clean_chain_into_a_cycle_still_closes_normally(self):
        """Only links ON the cycle are dropped; a legitimate successor still closes."""
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-01-01-001", "pending", "2026-12-31", supersedes="2026-07-01-002"),
                    row("2026-07-01-002", "pending", "2026-12-31", supersedes="2026-07-01-003"),
                    row("2026-07-01-003", "pending", "2026-12-31", supersedes="2026-07-01-002"),
                ],
            )
            listed, suppressed = run([reg], "2026-07-27")
        self.assertNotIn("2026-07-01-002", listed, "closed by the off-cycle row")
        self.assertIn("2026-01-01-001", listed)
        self.assertIn("2026-07-01-003", listed, "cycle members must not vanish")

    def test_cycle_detector_on_plain_graphs(self):
        self.assertEqual(fuc.ids_on_supersedes_cycles({"a": {"b"}, "b": {"c"}}), set())
        self.assertEqual(fuc.ids_on_supersedes_cycles({"a": {"b"}, "b": {"a"}}), {"a", "b"})
        self.assertEqual(fuc.ids_on_supersedes_cycles({}), set())


class TestByteOrderMark(unittest.TestCase):
    """PowerShell 5.1 writes UTF-8 with a BOM; U+FEFF is not whitespace."""

    def test_bom_does_not_drop_the_first_row(self):
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-07-01-001", "pending", "2026-12-31"),
                    row("2026-07-01-002", "pending", "2026-12-31"),
                ],
            )
            reg.write_bytes(b"\xef\xbb\xbf" + reg.read_bytes())
            listed, _ = run([reg], "2026-07-27")
        self.assertEqual(sorted(listed), ["2026-07-01-001", "2026-07-01-002"])


class TestUnparseableLines(unittest.TestCase):
    def _listed(self, extra_lines):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "retro-findings.jsonl"
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row("2026-01-01-001", "pending", "2026-01-31")) + "\n")
                for line in extra_lines:
                    fh.write(line + "\n")
            return run([path], "2026-07-27")[0]

    def test_ordinary_bad_json_is_skipped_not_fatal(self):
        self.assertEqual(self._listed(["{not json"]), ["2026-01-01-001"])

    def test_oversized_integer_is_skipped_not_fatal(self):
        """CPython caps int-from-string at 4300 digits and raises a plain
        ValueError -- NOT JSONDecodeError, so `except JSONDecodeError` missed it
        and one bad line killed the entire report."""
        self.assertEqual(self._listed(['{"x": ' + "1" * 4301 + "}"]), ["2026-01-01-001"])

    def test_non_object_row_is_skipped_not_fatal(self):
        self.assertEqual(self._listed(["[1, 2, 3]", '"a string"']), ["2026-01-01-001"])


class TestRendering(unittest.TestCase):
    def test_markdown_reports_the_suppressed_count(self):
        """Suppression must be visible, not silent -- a vanished row is its own defect."""
        out = fuc.render_markdown([], date.fromisoformat("2026-07-27"), suppressed=2)
        self.assertIn("2 row(s) closed by a supersedes link", out)
        self.assertIn("--include-superseded", out)

    def test_markdown_omits_the_note_when_nothing_suppressed(self):
        out = fuc.render_markdown([], date.fromisoformat("2026-07-27"), suppressed=0)
        self.assertNotIn("supersedes link", out)

    def test_markdown_marks_a_shown_superseded_row(self):
        r = dict(row("2026-06-02-001", "pending", "2026-06-30"))
        r["_past_due"] = False
        r["_days_overdue"] = 0
        r["_closed_by_supersedes"] = True
        out = fuc.render_markdown([r], date.fromisoformat("2026-07-27"), suppressed=1)
        self.assertIn("pending (closed: superseded)", out)

    def test_json_carries_the_closure_flag(self):
        r = dict(row("2026-06-02-001", "pending", "2026-06-30"))
        r["_past_due"] = False
        r["_days_overdue"] = 0
        r["_closed_by_supersedes"] = True
        parsed = json.loads(fuc.render_json([r]))
        self.assertTrue(parsed[0]["closed_by_supersedes"])
        self.assertEqual(parsed[0]["days_overdue"], 0)


class TestCommandLine(unittest.TestCase):
    """main() itself. Without this, a regression that passed suppressed=0 into
    render_markdown would restore silent suppression with every unit test green.
    """

    SCRIPT = LIB / "follow_up_check.py"

    def _run(self, reg, *extra):
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), "--registries", str(reg),
             "--asof", "2026-07-27", *extra],
            capture_output=True, text=True,
        )

    def _registry(self, d):
        return write_registry(
            d,
            [
                row("2026-06-02-001", "pending", "2026-06-30"),
                row("2026-07-03-001", "pending", "2026-12-31", supersedes="2026-06-02-001"),
            ],
        )

    def test_markdown_reports_the_suppression(self):
        with tempfile.TemporaryDirectory() as d:
            res = self._run(self._registry(d))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("1 row(s) closed by a supersedes link", res.stdout)
        self.assertIn("2026-07-03-001", res.stdout)
        self.assertNotIn("2026-06-02-001", res.stdout)

    def test_json_emits_the_note_on_stderr_and_keeps_a_pure_array(self):
        with tempfile.TemporaryDirectory() as d:
            res = self._run(self._registry(d), "--format", "json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("closed by a supersedes link", res.stderr)
        parsed = json.loads(res.stdout)
        self.assertEqual([r["finding_id"] for r in parsed], ["2026-07-03-001"])
        self.assertFalse(parsed[0]["closed_by_supersedes"])

    def test_include_superseded_does_not_tell_you_to_pass_the_flag_you_passed(self):
        with tempfile.TemporaryDirectory() as d:
            res = self._run(self._registry(d), "--include-superseded")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("2026-06-02-001", res.stdout)
        self.assertNotIn("re-run with --include-superseded", res.stdout)
        self.assertIn("shown only because --include-superseded", res.stdout)

    def test_unknown_status_warns_on_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            r = row("2026-07-01-001", "pending", "2026-12-31")
            r["follow_up_status"] = "in_progress"
            res = self._run(write_registry(d, [r]))
        self.assertIn("treated as OPEN and listed", res.stderr)
        self.assertIn("2026-07-01-001", res.stdout)

    def test_cycle_warns_on_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            reg = write_registry(
                d,
                [
                    row("2026-07-01-001", "pending", "2026-12-31", supersedes="2026-07-01-002"),
                    row("2026-07-01-002", "pending", "2026-12-31", supersedes="2026-07-01-001"),
                ],
            )
            res = self._run(reg)
        self.assertIn("supersedes cycle", res.stderr)
        self.assertIn("2026-07-01-001", res.stdout)
        self.assertIn("2026-07-01-002", res.stdout)

    def test_exits_zero_even_on_a_corrupt_registry(self):
        """It is a report, not a gate."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "retro-findings.jsonl"
            path.write_text("{not json\n[1,2,3]\n", encoding="utf-8")
            res = self._run(path)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
