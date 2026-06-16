#!/usr/bin/env python3
"""Krippendorff's alpha for nominal data — stdlib only.

Reference: Krippendorff, K. (2011). "Computing Krippendorff's Alpha-Reliability."
http://repository.upenn.edu/asc_papers/43/

For nominal data with all items rated by all coders, alpha is computed from
the coincidence matrix using:

    alpha = 1 - (D_o / D_e)

where D_o is observed disagreement (sum of off-diagonal coincidences /
total coincidences) and D_e is expected disagreement under the
hypothesis of chance assignment (computed from marginal frequencies).

For nominal data the metric delta(c, k) is 0 if c == k, else 1.
"""

from __future__ import annotations

import sys
from collections import Counter


def krippendorff_alpha(coder_labels: list[dict[str, str]]) -> float:
    # Labels are compared case-sensitively. Callers that expect 'positive'/'POSITIVE' to be equivalent should casefold before calling.
    """Compute Krippendorff's alpha for nominal data.

    Args:
        coder_labels: list of {item_id: code} dicts, one per coder.
            All coders must have labelled the SAME set of item_ids
            (missing-data handling not supported here).
            None labels are coerced to the sentinel string "<NULL>" so that
            (a) the sort over categories does not crash on mixed str/None,
            and (b) None vs None counts as agreement.

    Returns:
        alpha in (-inf, 1.0]. 1.0 = perfect agreement.

    Raises:
        ValueError: if fewer than 2 coders, or if item_id sets do not match.
    """
    if len(coder_labels) < 2:
        raise ValueError("Krippendorff's alpha requires at least 2 coders")

    # Validate identical item-id sets across coders
    item_ids = set(coder_labels[0].keys())
    for i, c in enumerate(coder_labels[1:], start=1):
        if set(c.keys()) != item_ids:
            raise ValueError(
                f"Coder {i} item_ids differ from coder 0; "
                f"missing-data handling not supported"
            )

    if not item_ids:
        raise ValueError("No items to score")

    # Build the coincidence matrix.
    # For each item with m coders, every ordered pair (c_a, c_b) where a != b
    # contributes 1/(m-1) to coincidence[c_a][c_b].
    # See Krippendorff 2011 eq. (5).
    coincidence: dict[tuple[str, str], float] = {}
    categories: set[str] = set()
    m = len(coder_labels)
    pair_weight = 1.0 / (m - 1)

    for item in item_ids:
        # Coerce None to a sentinel string so sorted(categories) cannot crash on mixed str/None.
        labels = [("<NULL>" if coder[item] is None else str(coder[item])) for coder in coder_labels]
        categories.update(labels)
        for a_idx, a_val in enumerate(labels):
            for b_idx, b_val in enumerate(labels):
                if a_idx == b_idx:
                    continue
                key = (a_val, b_val)
                coincidence[key] = coincidence.get(key, 0.0) + pair_weight

    # Marginal totals n_c = sum_k coincidence[c][k]
    n_c: dict[str, float] = {c: 0.0 for c in categories}
    for (a, b), v in coincidence.items():
        n_c[a] += v
    n_total = sum(n_c.values())

    if n_total == 0:
        raise ValueError("Empty coincidence matrix")

    # Observed disagreement D_o: sum of off-diagonal mass / n_total.
    # For nominal: delta(c, k) = 1 if c != k.
    d_o_num = 0.0
    for (a, b), v in coincidence.items():
        if a != b:
            d_o_num += v
    d_o = d_o_num / n_total

    # Expected disagreement D_e under chance:
    # D_e = (1 / (n_total - 1)) * sum_{c != k} (n_c * n_k) / n_total
    # i.e. = sum_{c != k} n_c * n_k / (n_total * (n_total - 1))
    cats = sorted(categories)
    d_e_num = 0.0
    for i, c in enumerate(cats):
        for k in cats[i + 1:]:
            # Pair (c, k) and (k, c) both contribute, hence factor 2.
            d_e_num += 2.0 * n_c[c] * n_c[k]
    if n_total <= 1:
        raise ValueError("Insufficient data: n_total <= 1")
    d_e = d_e_num / (n_total * (n_total - 1))

    if d_e == 0:
        # All coders gave the same single label across all items → perfect.
        return 1.0

    return 1.0 - (d_o / d_e)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _split(s: str) -> list[str]:
    return s.split()


def test_textbook_fixture() -> tuple[bool, float, str]:
    """4 coders, 12 items, nominal labels {a,b,c,d,e}.

    Manual hand-derivation (verified independently 2026-05-23):

      Data layout (each row = one coder, 12 items):
        c1: a b c c d d a b c d d c
        c2: a b c c d d a b c e d c
        c3: a b c c d d a b c d d c
        c4: a b c c d d a b c d e c

      Items with disagreement:
        item 9 (0-indexed): d,e,d,d  -> 1 outlier
        item 10:            d,d,d,e  -> 1 outlier
      All other 10 items: 4 coders agree exactly.

      Per item: ordered pairs = m*(m-1) = 12.
      Per one-outlier item: 6 ordered pairs disagree (outlier paired
      with each of the 3 others, in both directions).

      Total ordered pairs        = 12 items * 12 pairs/item = 144
      Disagreeing ordered pairs  = 2 items * 6              = 12
      Pair weight                = 1 / (m - 1)              = 1/3
      Coincidence mass total n   = 144 * 1/3                = 48

      Label counts across all 48 coder-item cells:
        n_a = 8, n_b = 8, n_c = 16, n_d = 14, n_e = 2   (sum 48)

      D_o = (disagree_pairs * pair_weight) / n
          = (12 * 1/3) / 48
          = 4 / 48
          = 0.08333...

      D_e = sum_{c != k} n_c * n_k / (n * (n - 1))
          numerator = 2 * (8*8 + 8*16 + 8*14 + 8*2
                          + 8*16 + 8*14 + 8*2
                          + 16*14 + 16*2
                          + 14*2)
                    = 2 * 860 = 1720
          D_e = 1720 / (48 * 47) = 1720 / 2256 = 0.76241...

      alpha = 1 - D_o/D_e = 1 - 0.08333/0.76241 = 0.8907

    NOTE: The phase brief suggested ~0.808, but the hand calculation
    above (and the implementation below) both yield 0.891 for THIS
    exact dataset. The 0.808 value likely refers to a different
    fixture (Krippendorff's most-cited 0.691 fixture uses missing
    data; the Hayes-Krippendorff 2007 fixture has a different label
    distribution). Stored expected = 0.891 for the data above.
    """
    c1 = _split("a b c c d d a b c d d c")
    c2 = _split("a b c c d d a b c e d c")
    c3 = _split("a b c c d d a b c d d c")
    c4 = _split("a b c c d d a b c d e c")
    items = [f"i{i}" for i in range(12)]
    coders = [
        {k: v for k, v in zip(items, c1)},
        {k: v for k, v in zip(items, c2)},
        {k: v for k, v in zip(items, c3)},
        {k: v for k, v in zip(items, c4)},
    ]
    expected = 0.891  # see docstring derivation
    alpha = krippendorff_alpha(coders)
    ok = abs(alpha - expected) < 0.01
    return ok, alpha, f"alpha={alpha:.3f}"


def test_all_agree() -> tuple[bool, float, str]:
    """3 coders, 5 items, identical labels → alpha == 1.0."""
    labels = {f"i{i}": c for i, c in enumerate("abcde")}
    coders = [dict(labels), dict(labels), dict(labels)]
    alpha = krippendorff_alpha(coders)
    ok = alpha > 0.999
    return ok, alpha, f"alpha={alpha:.3f}"


def test_systematic_disagreement() -> tuple[bool, float, str]:
    """2 coders, 4 items, every label differs → alpha < 0."""
    c1 = {"i0": "a", "i1": "b", "i2": "c", "i3": "d"}
    c2 = {"i0": "b", "i1": "a", "i2": "d", "i3": "c"}
    alpha = krippendorff_alpha([c1, c2])
    ok = alpha < 0
    return ok, alpha, f"alpha={alpha:.3f}"


def test_single_coder_raises() -> tuple[bool, float, str]:
    """Fewer than 2 coders → ValueError."""
    try:
        krippendorff_alpha([{"i0": "a"}])
    except ValueError:
        return True, 0.0, "raised ValueError"
    return False, 0.0, "did not raise"


def test_none_labels_no_crash() -> tuple[bool, float, str]:
    """None labels are coerced to a sentinel; alpha computes without crashing."""
    c1 = {"i0": "a", "i1": None, "i2": "c"}
    c2 = {"i0": "a", "i1": None, "i2": "c"}
    alpha = krippendorff_alpha([c1, c2])
    # Both coders fully agree (including on the None-as-sentinel cell) -> alpha ~= 1.0.
    ok = alpha > 0.999
    return ok, alpha, f"alpha={alpha:.3f}"


def _run_tests() -> int:
    tests = [
        ("test_textbook_fixture", test_textbook_fixture),
        ("test_all_agree", test_all_agree),
        ("test_systematic_disagreement", test_systematic_disagreement),
        ("test_single_coder_raises", test_single_coder_raises),
        ("test_none_labels_no_crash", test_none_labels_no_crash),
    ]
    passed = 0
    for name, fn in tests:
        ok, _alpha, detail = fn()
        status = "PASS" if ok else "FAIL"
        print(f"{name}: {status} ({detail})")
        if ok:
            passed += 1
    print(f"{passed}/{len(tests)} tests passed")
    return 0 if passed == len(tests) else 1


def _usage() -> int:
    print("usage: python krippendorff_alpha.py --test", file=sys.stderr)
    print("       (importable: from krippendorff_alpha import krippendorff_alpha)",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--test":
        sys.exit(_run_tests())
    sys.exit(_usage())
