#!/usr/bin/env python3
"""
Gemini image client — the maintained, zero-dependency core for generating (and
editing) images with Google's Gemini models, plus text/conversation helpers.

This is the canonical fork of the older `gemini-client` plugin. It is a strict
superset: same text/conversation/list-models surface, plus an upgraded image
path that fixes the gaps the plugin had:

  1. Reference-image INPUT  — attach one or more images to the request so the
     model can edit/compose/restyle them (`--input-image`, repeatable).
  2. Multi-image OUTPUT     — when the model returns >1 image, all are saved
     (`out.png`, `out-2.png`, …) instead of silently overwriting one file.
  3. Safety-block DIAGNOSTICS — a blocked or empty response is explained
     (blockReason / finishReason / safetyRatings) instead of a generic
     "no image returned".
  4. Verified model menu    — defaults and choices grounded in the live API
     (see MODELS below), not stale docs.
  5. Best-available default — omitting a model auto-selects the highest-fidelity
     image model your key can access (cached 24h; explicit --model always wins).

Zero dependencies: standard library only (urllib, base64, json, argparse).
Importable: `generate`, `generate_image`, `resolve_best_image_model`,
`extract_text`, `extract_images`, `extract_usage`, `list_models`, and the
`GeminiError` exception are public, so projects can vendor this file and build
thin wrappers on top of it.

Authentication (checked in this order):
  1. --api-key CLI flag  /  api_key= argument
  2. GEMINI_API_KEY environment variable
  3. GOOGLE_API_KEY environment variable
  4. .env file in the current working directory, then next to this script

Usage examples:
  # Generate an image
  python gemini_image.py --image -o castle.png "A watercolor medieval castle"

  # Custom model / aspect ratio / resolution
  python gemini_image.py --image -o wide.png --model gemini-3-pro-image \\
      --aspect-ratio 16:9 --image-size 2K "Panoramic alpine valley at dawn"

  # Edit / compose from reference images (repeat --input-image up to the
  # model's limit; Gemini 3 Pro accepts up to 8, 3.1 Flash up to 14)
  python gemini_image.py --image -o edited.png \\
      -i room.png -i sofa.png "Place this sofa in this room, daylight"

  # Auto-select the best available image model for your key (omit --model)
  python gemini_image.py --image -o peonies.png "A botanical oil painting of peonies"

  # Plain text (the skill is image-first but a full client)
  python gemini_image.py "Explain diffusion models in one paragraph"

  # List the image-capable models your key can see
  python gemini_image.py --list-models --images-only
"""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash"
# GA workhorse ("Nano Banana"): cheap, fast, stable. Safe default.
DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"

# Image-capable models verified against the live /models endpoint (2026-06).
# Override with --model. This list backs help text + --images-only filtering;
# IMAGE_MODEL_PREFERENCE (below) is the quality RANKING the resolver uses.
IMAGE_MODELS = [
    "gemini-2.5-flash-image",        # GA, high-volume workhorse
    "gemini-3-pro-image",            # GA, highest fidelity, up to 8 ref images
    "gemini-3-pro-image-preview",    # preview alias of the above
    "gemini-3.1-flash-image",        # GA, extreme aspect ratios + 512, video-in
    "gemini-3.1-flash-image-preview",
    # imagen-4.0-* exist too but use the separate :predict endpoint, not
    # generateContent — out of scope for this generateContent-based client.
]

# Verified aspect ratios (all current image models). 3.1-flash additionally
# accepts the extreme ratios 1:4, 4:1, 1:8, 8:1.
ASPECT_RATIOS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4",
                 "9:16", "16:9", "21:9",
                 "1:4", "4:1", "1:8", "8:1"]  # extremes: gemini-3.1-flash-image only
# "512" is only honoured by gemini-3.1-flash-image; the K sizes are universal.
IMAGE_SIZES = ["512", "1K", "2K", "4K"]

_INPUT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic",
}
_EXT_FROM_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

# ── Best-available-model resolution ──────────────────────────────────────────

# Sentinel for "caller passed no model" (→ resolve best) vs. "caller chose a
# model" (→ use verbatim). A plain-string default can't tell these apart, so an
# explicit `gemini-2.5-flash-image` would be indistinguishable from the default.
_RESOLVE = object()

# Set True only inside main() so the auto-select disclosure prints for CLI use
# while importable/library callers stay silent (stdout / --json never polluted).
_CLI_MODE = False

# Curated quality ranking, BEST FIRST. The /models response carries no tier or
# quality field (every 3.x image model reports version "3.0"), so this order is
# human-curated: pro outranks flash and GA outranks preview REGARDLESS of
# recency (gemini-3.1 is newer than gemini-3 but a lower-fidelity flash tier).
# The resolver consults this first, then lets a genuinely-newer GA *pro* model
# from the live list win automatically (see resolve_best_image_model).
IMAGE_MODEL_PREFERENCE = [
    "gemini-3-pro-image",             # flagship, GA — the "absolute best"
    "gemini-3-pro-image-preview",     # preview peer (only if no GA pro visible)
    "gemini-3.1-flash-image",         # balanced GA flash (~half cost, close to pro)
    "gemini-3.1-flash-image-preview",
    "gemini-2.5-flash-image",         # GA legacy floor == DEFAULT_IMAGE_MODEL
]

_MODEL_CACHE_TTL = 86400  # 24h: a freshly-GA model is discoverable within a day
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "gemini-image-cache")

# Cache validity is tied to the RANKING LOGIC, not just the schema version, so
# editing the ranking invalidates stale cached defaults instead of serving them
# for up to the TTL. Preference-list edits fold in automatically; bump the literal
# token below whenever the _image_model_sort_key tier/GA gates change.
_RANK_FINGERPRINT = hashlib.sha1(
    ("rank-logic-1|" + ",".join(IMAGE_MODEL_PREFERENCE)).encode()
).hexdigest()[:12]


class GeminiError(Exception):
    """Raised on any API/auth/IO failure. CLI converts this to exit(1)."""


# ── .env loading ─────────────────────────────────────────────────────────────

def _load_dotenv() -> None:
    """Inject .env values into os.environ without overwriting existing vars.

    Searches the cwd first, then the directory containing this script.
    """
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
        break  # stop after the first .env found


_load_dotenv()


# ── Auth + transport ─────────────────────────────────────────────────────────

def _get_api_key(override: Optional[str] = None) -> str:
    """Resolve the API key from argument, environment, or .env file."""
    key = override or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise GeminiError(
            "No API key found. Provide it via --api-key, the GEMINI_API_KEY "
            "(or GOOGLE_API_KEY) environment variable, or a .env file."
        )
    return key


def _make_request(url: str, api_key: str, payload: Optional[dict] = None,
                  method: str = "GET", timeout: int = 120,
                  max_retries: int = 4) -> dict:
    """Authenticated request with exponential backoff on 429 / 5xx / transport
    errors. Raises GeminiError on non-retryable failure or exhausted retries."""
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8") if payload is not None else None

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            retryable = e.code == 429 or 500 <= e.code < 600
            if retryable and attempt < max_retries:
                delay = min(2 ** attempt * 2, 30)  # 2, 4, 8, 16s (4 retries; cap 30s)
                print(f"HTTP {e.code} (attempt {attempt + 1}/{max_retries + 1}); "
                      f"retrying in {delay}s…", file=sys.stderr)
                time.sleep(delay)
                attempt += 1
                continue
            raise GeminiError(f"HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            if attempt < max_retries:
                delay = min(2 ** attempt * 2, 30)
                print(f"Connection error: {e.reason}; retrying in {delay}s…",
                      file=sys.stderr)
                time.sleep(delay)
                attempt += 1
                continue
            raise GeminiError(f"Connection error: {e.reason}")


def _stream_request(url: str, api_key: str, payload: dict, timeout: int = 120):
    """Stream an SSE response, yielding text chunks."""
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise GeminiError(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
    except urllib.error.URLError as e:
        raise GeminiError(f"Connection error: {e.reason}")

    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace")
        if line.startswith("data: "):
            json_str = line[6:].strip()
            if not json_str:
                continue
            try:
                chunk = json.loads(json_str)
            except json.JSONDecodeError:
                continue
            for candidate in chunk.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    if part.get("text"):
                        yield part["text"]
    resp.close()


# ── Input-image helpers ──────────────────────────────────────────────────────

def _image_part(path: str) -> dict:
    """Read an image file into an inline_data request part (base64)."""
    if not os.path.isfile(path):
        raise GeminiError(f"Input image not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    mime = _INPUT_MIME.get(ext) or mimetypes.guess_type(path)[0]
    if not mime or not mime.startswith("image/"):
        raise GeminiError(f"Unsupported input image type for {path!r} (mime={mime}).")
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return {"inline_data": {"mime_type": mime, "data": encoded}}


# ── Public API ───────────────────────────────────────────────────────────────

def list_models(api_key: Optional[str] = None) -> list[dict]:
    """List models the key can see."""
    return _make_request(f"{BASE_URL}/models", _get_api_key(api_key)).get("models", [])


def _image_model_sort_key(model_id: str):
    """Rank an image-model id as (tier_rank, ga_rank, version) — higher better.

    Tier is read from the marketing name (pro > flash > lite/nano/fast/mini);
    recency only breaks ties WITHIN a tier, so a newer flash never outranks a GA
    pro. GA (no preview/exp/dated-preview suffix) outranks preview. The version
    is parsed numerically so (4, 0) > (3, 1) and (3, 10) > (3, 2) — never lexed.
    """
    mid = model_id.lower()
    # Tokenize on '-' and '.' so we match whole tier words — NOT substrings.
    # (Substring matching is a trap: "mini" is inside "geMINI", which would
    #  mis-tag every Gemini model as the lowest tier.)
    tokens = set(re.split(r"[-.]", mid))
    small = {"lite", "nano", "fast", "mini"}
    if "pro" in tokens and "flash" not in tokens and not (tokens & small):
        tier_rank = 3
    elif "flash" in tokens and "lite" not in tokens:
        tier_rank = 2
    elif tokens & small:
        tier_rank = 1
    else:
        tier_rank = 2  # unknown shape: treated as mid, never the flagship slot
    is_preview = (bool(tokens & {"preview", "exp", "experimental"})
                  or re.search(r"-\d\d-\d{4}", mid) is not None)
    ga_rank = 0 if is_preview else 1
    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", mid)
    version = (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)
    return (tier_rank, ga_rank, version)


def _model_cache_path(key_hash: str) -> str:
    return os.path.join(_CACHE_DIR, f"models-{key_hash}.json")


def _read_model_cache(key_hash: str) -> Optional[dict]:
    """Return a fresh, valid cache entry, or None on any miss / corruption."""
    try:
        with open(_model_cache_path(key_hash), encoding="utf-8") as f:
            entry = json.load(f)
        if not isinstance(entry, dict):
            return None  # valid JSON but wrong shape → miss, so it gets rewritten
        if (entry.get("v") == 1 and entry.get("key_hash") == key_hash
                and entry.get("rank_fp") == _RANK_FINGERPRINT
                and entry.get("best")
                and (time.time() - entry["ts"]) <= _MODEL_CACHE_TTL):
            return entry
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return None


def _write_model_cache(key_hash: str, best: str, available: list) -> None:
    """Best-effort ATOMIC cache write (os.replace); any failure is swallowed."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        entry = {"v": 1, "ts": int(time.time()), "key_hash": key_hash,
                 "rank_fp": _RANK_FINGERPRINT, "best": best, "available": available}
        final = _model_cache_path(key_hash)
        tmp = f"{final}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(tmp, final)  # atomic: a concurrent reader never sees a partial file
    except OSError:
        pass


def resolve_best_image_model(api_key: Optional[str] = None, *,
                             force_refresh: bool = False) -> str:
    """Resolve the best image model available to this API key. NEVER raises.

    Degradation ladder (each rung falls through to the next, never to an error):
    GEMINI_IMAGE_DEFAULT env pin → fresh per-key cache → live /models ranked →
    static DEFAULT_IMAGE_MODEL. The whole body is wrapped so resolution can never
    make generation fail; worst case equals the old hard-coded default exactly.

    An explicit model passed to the CLI/library bypasses this entirely (handled
    by the caller via the _RESOLVE sentinel), so this only runs for the default.
    """
    try:
        pin = os.environ.get("GEMINI_IMAGE_DEFAULT")
        if pin:
            return pin  # operator hard-pin; skips network + ranking

        key = _get_api_key(api_key)
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:16]  # key never stored raw

        if not force_refresh:
            entry = _read_model_cache(key_hash)
            if entry:
                return entry["best"]  # hot path: sub-ms file read, no network

        # The one network call. Filter live models to image generators we can
        # actually drive via generateContent (drops Imagen :predict + text).
        available = set()
        for m in list_models(key):
            mid = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if re.search(r"(image|imagen)", mid) and "generateContent" in methods:
                available.add(mid)

        if not available:
            return DEFAULT_IMAGE_MODEL

        # Curated fast path: the highest-ranked KNOWN model that's available.
        best = next((mid for mid in IMAGE_MODEL_PREFERENCE if mid in available), None)
        # Future-model path: a genuinely-better GA *pro* auto-wins with no code
        # edit; the tier/GA gates block promoting a flash/preview/experimental.
        comparator = max(available, key=_image_model_sort_key)
        c_tier, c_ga, _c_ver = _image_model_sort_key(comparator)
        if best is None:
            best = comparator
        # Compare the FULL sort key (tier > ga > version), NOT version alone —
        # else an available GA pro could lose to a higher-versioned curated
        # flash/preview and we'd hand back a lower-tier model.
        elif (c_tier == 3 and c_ga == 1
              and _image_model_sort_key(comparator) > _image_model_sort_key(best)):
            best = comparator

        _write_model_cache(key_hash, best, sorted(available))

        if _CLI_MODE and best != DEFAULT_IMAGE_MODEL:
            print(
                f"Auto-selected best available image model: {best}. Override with "
                f"--model <id> or GEMINI_IMAGE_DEFAULT (e.g. "
                f"--model gemini-3.1-flash-image for cheaper/faster, "
                f"--model gemini-2.5-flash-image for cheapest).",
                file=sys.stderr,
            )
        return best
    except Exception:
        return DEFAULT_IMAGE_MODEL  # belt-and-suspenders: resolution never breaks generation


def generate(
    prompt: str,
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    system: Optional[str] = None,
    conversation: Optional[list[dict]] = None,
    input_images: Optional[list[str]] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    top_k: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    stop_sequences: Optional[list[str]] = None,
    response_modalities: Optional[list[str]] = None,
    image_config: Optional[dict] = None,
    stream: bool = False,
    timeout: int = 120,
) -> "dict | str":
    """Call generateContent (or streamGenerateContent).

    input_images : list[str], optional
        Paths to reference images attached to the final user turn (for image
        editing / multi-image composition).
    image_config : dict, optional
        e.g. {"aspectRatio": "16:9", "imageSize": "2K"}. Sent under the
        generationConfig.imageConfig key — the shape proven across production
        callers and accepted by the live API. (The newer responseFormat.image
        shape is documented in SPEC.md as a future migration.)
    """
    key = _get_api_key(api_key)

    user_parts: list[dict] = []
    if prompt:
        user_parts.append({"text": prompt})
    for img in (input_images or []):
        user_parts.append(_image_part(img))

    if conversation:
        contents = list(conversation)
        if user_parts:
            contents.append({"role": "user", "parts": user_parts})
    else:
        contents = [{"role": "user", "parts": user_parts or [{"text": ""}]}]

    payload: dict = {"contents": contents}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    gen_config: dict = {}
    if temperature is not None:
        gen_config["temperature"] = temperature
    if top_p is not None:
        gen_config["topP"] = top_p
    if top_k is not None:
        gen_config["topK"] = top_k
    if max_output_tokens is not None:
        gen_config["maxOutputTokens"] = max_output_tokens
    if stop_sequences:
        gen_config["stopSequences"] = stop_sequences
    if response_modalities:
        gen_config["responseModalities"] = response_modalities
    if image_config:
        gen_config["imageConfig"] = image_config
    if gen_config:
        payload["generationConfig"] = gen_config

    if stream:
        url = f"{BASE_URL}/models/{model}:streamGenerateContent?alt=sse"
        chunks = []
        for text in _stream_request(url, key, payload, timeout):
            chunks.append(text)
            print(text, end="", flush=True)
        print()
        return "".join(chunks)

    url = f"{BASE_URL}/models/{model}:generateContent"
    return _make_request(url, key, payload, method="POST", timeout=timeout)


def _diagnose_no_image(response: dict) -> str:
    """Explain why a response carried no image (block / safety / finishReason)."""
    pf = response.get("promptFeedback") or {}
    if pf.get("blockReason"):
        ratings = ", ".join(
            f"{r.get('category', '?')}={r.get('probability', '?')}"
            for r in pf.get("safetyRatings", [])
        )
        extra = f" (safety: {ratings})" if ratings else ""
        return f"Prompt blocked — blockReason={pf['blockReason']}{extra}."

    candidates = response.get("candidates") or []
    if not candidates:
        return "No candidates returned — the prompt was likely blocked upstream."

    cand = candidates[0]
    fr = cand.get("finishReason")
    if fr and fr != "STOP":  # incl. MAX_TOKENS: with no image part that IS the failure
        ratings = ", ".join(
            f"{r.get('category', '?')}={r.get('probability', '?')}"
            for r in cand.get("safetyRatings", [])
        )
        extra = f" (safety: {ratings})" if ratings else ""
        return f"No image — finishReason={fr}{extra}."
    return ("Model returned no image part (it may have replied with text only; "
            "check the response text or rephrase the prompt).")


def extract_images(response: dict) -> list[dict]:
    """All images in a response, as [{"mime_type": str, "data": bytes}, …]."""
    images = []
    try:
        parts = response["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        return images
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and "data" in inline:
            mime = inline.get("mimeType") or inline.get("mime_type", "image/png")
            images.append({"mime_type": mime, "data": base64.b64decode(inline["data"])})
    return images


def _numbered_path(output_path: str, index: int, ext: str) -> str:
    """Apply the CONTENT-correct extension to the caller's base name, so a `.png`
    request never yields a file holding JPEG bytes (the image models pick the
    output format, e.g. gemini-3-pro-image returns JPEG). First image -> base+ext;
    subsequent -> base-2+ext, base-3+ext, …
    """
    base, _ = os.path.splitext(output_path)
    if index == 0:
        return base + ext
    return f"{base}-{index + 1}{ext}"


def generate_image(
    prompt: str,
    output_path: str = "output.png",
    *,
    api_key: Optional[str] = None,
    model=_RESOLVE,
    system: Optional[str] = None,
    input_images: Optional[list[str]] = None,
    aspect_ratio: str = "1:1",
    image_size: str = "1K",
    timeout: int = 120,
) -> dict:
    """Generate (or edit) one or more images and save them to disk.

    `model` defaults to the _RESOLVE sentinel, which selects the best image
    model available to the key (resolve_best_image_model). Pass an explicit
    model= string to bypass resolution entirely.

    Returns a dict:
        output_paths : list[str]  — every saved file (>=0)
        output_path  : str | None — the first saved file (back-compat)
        mime_type    : str | None
        text         : str        — any accompanying model text
        blocked      : bool       — True if no image came back
        diagnostic   : str | None — why, when blocked is True
        raw          : dict       — full API response
    """
    if model is _RESOLVE or not model:  # falsy (None/"") resolves, matching the CLI
        model = resolve_best_image_model(api_key=api_key)
    response = generate(
        prompt,
        api_key=api_key,
        model=model,
        system=system,
        input_images=input_images,
        response_modalities=["TEXT", "IMAGE"],
        image_config={"aspectRatio": aspect_ratio, "imageSize": image_size},
        timeout=timeout,
    )

    result = {"output_paths": [], "output_path": None, "mime_type": None,
              "text": "", "blocked": False, "diagnostic": None, "raw": response}

    try:
        parts = response["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        parts = []

    img_index = 0
    for part in parts:
        if part.get("text"):
            result["text"] += part["text"]
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and "data" in inline:
            img_bytes = base64.b64decode(inline["data"])
            mime = inline.get("mimeType") or inline.get("mime_type", "image/png")
            result["mime_type"] = mime
            ext = _EXT_FROM_MIME.get(mime, ".png")
            path = _numbered_path(output_path, img_index, ext)
            if img_index == 0:
                req = os.path.splitext(output_path)[1].lower()
                req = ".jpg" if req == ".jpeg" else req
                if req and req != ext:
                    print(f"Note: model returned {mime}; saved as {path} (not "
                          f"{req} — the extension matches the actual format).",
                          file=sys.stderr)
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(img_bytes)
            except OSError as e:
                raise GeminiError(f"Failed to save image to {path!r}: {e}")
            result["output_paths"].append(path)
            print(f"Image saved to: {path}", file=sys.stderr)
            img_index += 1

    if result["output_paths"]:
        result["output_path"] = result["output_paths"][0]
    else:
        result["blocked"] = True
        result["diagnostic"] = _diagnose_no_image(response)
    return result


def extract_text(response: dict) -> str:
    """Concatenated text from a response."""
    try:
        parts = response["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError):
        return ""


def extract_usage(response: dict) -> dict:
    """Token usage metadata."""
    return response.get("usageMetadata", {})


# ── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate images (and text) with Google Gemini.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("prompt", nargs="?", default=None, help="Text prompt")
    parser.add_argument("--api-key", default=None,
                        help="Gemini API key (else GEMINI_API_KEY / GOOGLE_API_KEY / .env)")
    parser.add_argument("--model", default=None,
                        help=f"Model ID (default: {DEFAULT_MODEL} for text; for "
                             f"--image, the best available image model is "
                             f"auto-detected — override here or via "
                             f"GEMINI_IMAGE_DEFAULT)")
    parser.add_argument("--system", default=None, help="System instruction")
    parser.add_argument("--conversation", default=None,
                        help="Path to a JSON file with a multi-turn conversation")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--max-tokens", type=int, default=None, help="Max output tokens")
    parser.add_argument("--stop", nargs="*", default=None, help="Stop sequences")
    parser.add_argument("--stream", action="store_true", help="Stream text output")
    parser.add_argument("--list-models", action="store_true", help="List models and exit")
    parser.add_argument("--images-only", action="store_true",
                        help="With --list-models, show only image-capable models")
    parser.add_argument("--json", action="store_true", help="Print raw JSON response")
    parser.add_argument("--timeout", type=int, default=120, help="Request timeout (s)")

    img = parser.add_argument_group("image generation")
    img.add_argument("--image", action="store_true", help="Generate an image")
    img.add_argument("--output", "-o", default="output.png",
                     help="Output path (default: output.png; extra images get -2, -3 …)")
    img.add_argument("--input-image", "-i", action="append", default=None,
                     metavar="PATH",
                     help="Reference image to edit/compose from (repeatable)")
    img.add_argument("--aspect-ratio", default="1:1", choices=ASPECT_RATIOS,
                     help="Aspect ratio (default: 1:1)")
    img.add_argument("--image-size", default="1K", choices=IMAGE_SIZES,
                     help="Resolution (default: 1K; '512' only on gemini-3.1-flash-image)")
    return parser


def _run(args) -> int:
    key = _get_api_key(args.api_key)

    if args.list_models:
        models = list_models(key)
        if args.images_only:
            wanted = tuple(IMAGE_MODELS)
            models = [m for m in models
                      if m.get("name", "").replace("models/", "") in wanted
                      or "image" in m.get("name", "").lower()]
        for m in models:
            print(f"  {m.get('name', ''):45s}  {m.get('description', '')[:80]}")
        return 0

    if not args.prompt and not args.conversation and not args.input_image:
        raise GeminiError("Provide a prompt, --conversation file, or --input-image.")

    if args.image:
        result = generate_image(
            prompt=args.prompt or "",
            output_path=args.output,
            api_key=key,
            model=args.model or _RESOLVE,
            system=args.system,
            input_images=args.input_image,
            aspect_ratio=args.aspect_ratio,
            image_size=args.image_size,
            timeout=args.timeout,
        )
        if args.json:
            redacted = {k: v for k, v in result.items() if k != "raw"}
            print(json.dumps(redacted, indent=2))
        elif result["text"]:
            print(result["text"])
        if result["blocked"]:
            print(f"Warning: no image returned. {result['diagnostic']}", file=sys.stderr)
            return 2
        return 0

    convo = None
    if args.conversation:
        with open(args.conversation) as f:
            convo = json.load(f)

    result = generate(
        prompt=args.prompt or "",
        api_key=key,
        model=args.model or DEFAULT_MODEL,
        system=args.system,
        conversation=convo,
        input_images=args.input_image,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        max_output_tokens=args.max_tokens,
        stop_sequences=args.stop,
        stream=args.stream,
        timeout=args.timeout,
    )
    if args.stream:
        return 0
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    text = extract_text(result)
    if text:
        print(text)
    else:
        print("No text in response. Raw:", json.dumps(result, indent=2), file=sys.stderr)
    usage = extract_usage(result)
    if usage:
        print(f"\n--- Tokens: prompt={usage.get('promptTokenCount', '?')}, "
              f"completion={usage.get('candidatesTokenCount', '?')}, "
              f"total={usage.get('totalTokenCount', '?')} ---", file=sys.stderr)
    return 0


def main() -> None:
    global _CLI_MODE
    _CLI_MODE = True  # enable the one-line auto-select disclosure for CLI use
    args = _build_parser().parse_args()
    try:
        sys.exit(_run(args))
    except GeminiError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
