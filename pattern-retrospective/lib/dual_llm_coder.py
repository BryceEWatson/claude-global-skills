#!/usr/bin/env python3
"""Dual-LLM coder for inter-rater reliability on retrospective findings.

Makes TWO independent Anthropic SDK calls against the same items + coding
scheme, then computes Krippendorff's alpha on the two label sets.

This is opt-in via the --dual-coder workflow in the pattern-retrospective
skill (see SKILL.md section 15). Publish gate (enforced via stderr + exit code):
    alpha >= 0.80  -> PUBLISH_GATE_PASSED       (exit 0)
    0.67 <= alpha  -> PUBLISH_GATE_EXPLORATORY  (exit 0, mark exploratory)
    alpha <  0.67  -> PUBLISH_GATE_BLOCKED      (exit 6)

Requires: ANTHROPIC_API_KEY environment variable (consumed by the anthropic SDK).
Use --dry-run-no-api for a smoke test that does not call the API or require a key.

Usage:
    python dual_llm_coder.py \\
        --items items.jsonl \\
        --coding-prompt "Code each item as positive, neutral, or negative."

JSONL input: one {"item_id": "...", "content": "..."} per line.

Output JSON (stdout or --output path):
    {
      "alpha": 0.93,
      "agreement": "5/5",
      "n_items": 5,
      "disagreements": [...],
      "request_ids": ["msg_...", "msg_..."],
      "outputs_differ_on": 0,
      "coder_1_labels": {...},
      "coder_2_labels": {...}
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Enable `from krippendorff_alpha import krippendorff_alpha` regardless of CWD.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from krippendorff_alpha import krippendorff_alpha  # noqa: E402


DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096

CODER_SYSTEM_PROMPT = (
    "You are an expert content coder. Given a JSONL list of items "
    "and a coding scheme, return a JSON object mapping each item_id "
    "to a single code label. Return ONLY the JSON object, no commentary."
)


def _try_import_anthropic():
    try:
        import anthropic  # type: ignore
        return anthropic
    except ImportError:
        print(
            "anthropic SDK not installed. Install with:\n"
            "    pip install anthropic\n",
            file=sys.stderr,
        )
        sys.exit(3)


def load_items(path: Path) -> list[dict]:
    """Stream a JSONL items file line by line."""
    items: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for ln_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Malformed JSONL at {path}:{ln_no}: {e}") from e
            if "item_id" not in rec:
                raise ValueError(f"Missing 'item_id' at {path}:{ln_no}: {rec!r}")
            items.append(rec)
    if not items:
        raise ValueError(f"No items in {path}")
    return items


def build_user_message(coding_prompt: str, items: list[dict]) -> str:
    items_jsonl = "\n".join(json.dumps(i, ensure_ascii=False) for i in items)
    return f"{coding_prompt}\n\nItems (JSONL):\n{items_jsonl}"


def build_request_payload(model: str, coding_prompt: str, items: list[dict]) -> dict:
    """Build the dict passed to client.messages.create."""
    # cache_control on both system+user blocks - call 2 hits cache for input-side savings
    user_text = build_user_message(coding_prompt, items)
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [
            {
                "type": "text",
                "text": CODER_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_text,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    }


def call_coder(client, payload: dict, coder_id: str) -> dict:
    """Make one Anthropic API call and parse {item_id: code}."""
    response = client.messages.create(**payload)
    raw_text = response.content[0].text
    try:
        labels = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{coder_id} returned non-JSON content: {raw_text[:200]!r}"
        ) from e
    if not isinstance(labels, dict):
        raise RuntimeError(
            f"{coder_id} returned non-object JSON: {type(labels).__name__}"
        )
    return {
        "coder_id": coder_id,
        "request_id": response.id,
        "labels": {str(k): str(v) for k, v in labels.items()},
    }


def run_dual(
    items: list[dict],
    coding_prompt: str,
    model: str,
    dry_run: bool,
) -> dict:
    payload = build_request_payload(model, coding_prompt, items)

    if dry_run:
        return {
            "dry_run": True,
            "model": model,
            "n_items": len(items),
            "request_payload": payload,
            "note": (
                "Two INDEPENDENT calls would be made with this exact payload; "
                "coder_1 and coder_2 must return different request_ids."
            ),
        }

    anthropic = _try_import_anthropic()
    client = anthropic.Anthropic()

    coder_1 = call_coder(client, payload, "coder_1")
    coder_2 = call_coder(client, payload, "coder_2")

    # Independence check: request_ids MUST differ.
    if coder_1["request_id"] == coder_2["request_id"]:
        raise RuntimeError(
            "Independence violation: coder_1 and coder_2 returned the same "
            f"request_id ({coder_1['request_id']!r}). "
            "Two separate client.messages.create() calls should always "
            "produce two distinct request_ids."
        )

    # Cache-hit / determinism leak warning.
    if coder_1["labels"] == coder_2["labels"]:
        print(
            "WARN: coder_1 and coder_2 produced byte-identical labels. "
            "Possible cache hit, temperature=0 determinism, or trivially "
            "easy coding task. Alpha will be 1.0 by construction; "
            "interpret with caution.",
            file=sys.stderr,
        )

    alpha = krippendorff_alpha([coder_1["labels"], coder_2["labels"]])

    item_ids = [i["item_id"] for i in items]
    disagreements: list[str] = []
    matched = 0
    for iid in item_ids:
        a = coder_1["labels"].get(iid)
        b = coder_2["labels"].get(iid)
        if a == b:
            matched += 1
        else:
            disagreements.append(iid)

    return {
        "alpha": alpha,
        "agreement": f"{matched}/{len(item_ids)}",
        "n_items": len(item_ids),
        "disagreements": disagreements,
        "request_ids": [coder_1["request_id"], coder_2["request_id"]],
        "outputs_differ_on": len(disagreements),
        "coder_1_labels": coder_1["labels"],
        "coder_2_labels": coder_2["labels"],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Run two independent Anthropic SDK calls against the same items "
            "and compute Krippendorff's alpha between the two label sets. "
            "Requires ANTHROPIC_API_KEY env var unless --dry-run-no-api is set. "
            "Exit codes: 0 = published or exploratory; 3 = missing API key; "
            "6 = publish gate blocked (alpha < 0.67)."
        )
    )
    p.add_argument(
        "--items",
        required=True,
        type=Path,
        help='Path to a JSONL file; each line {"item_id": ..., "content": ...}.',
    )
    p.add_argument(
        "--coding-prompt",
        required=True,
        help="The prompt describing the coding scheme (categorical labels).",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Anthropic model id (default: {DEFAULT_MODEL}).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output JSON file. Defaults to stdout.",
    )
    p.add_argument(
        "--dry-run-no-api",
        action="store_true",
        help=(
            "Do not call the Anthropic API. Print the request payload that "
            "would be sent. Use for structural verification only."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # ANTHROPIC_API_KEY guard: fail fast with a clear message rather than the SDK's opaque error.
    if not args.dry_run_no_api and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "error: ANTHROPIC_API_KEY not set. "
            "Run with --dry-run-no-api to skip the API call.",
            file=sys.stderr,
        )
        return 3
    items = load_items(args.items)
    result = run_dual(
        items=items,
        coding_prompt=args.coding_prompt,
        model=args.model,
        dry_run=args.dry_run_no_api,
    )
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(text)

    # Publish gate (SKILL.md section 15). Skip on dry-run since there is no alpha.
    if args.dry_run_no_api:
        return 0
    alpha = result.get("alpha")
    if alpha is None:
        return 0
    if alpha >= 0.80:
        print(f"PUBLISH_GATE_PASSED: alpha={alpha:.3f}", file=sys.stderr)
        return 0
    if alpha >= 0.67:
        print(
            f"PUBLISH_GATE_EXPLORATORY: alpha={alpha:.3f} - mark exploratory",
            file=sys.stderr,
        )
        return 0
    print(
        f"PUBLISH_GATE_BLOCKED: alpha={alpha:.3f} below threshold",
        file=sys.stderr,
    )
    return 6


if __name__ == "__main__":
    sys.exit(main())
