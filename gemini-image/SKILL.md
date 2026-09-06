---
name: gemini-image
description: >-
  Generate and edit images with Google's Gemini models (Nano Banana, Gemini 3 Pro
  Image, 3.1 Flash Image) from a zero-dependency Python CLI. Use to create, edit,
  restyle, or compose an image with Gemini or Google AI, including image-to-image
  with reference images. Also handles text and model-listing. The maintained
  successor to the older `gemini-client` plugin.
metadata:
  type: reference
---

# gemini-image

The maintained, machine-wide core for **image generation and editing with
Google Gemini**. One zero-dependency Python file — runs on any machine with
`python`, no per-project install. It is the canonical successor to the
`gemini-client` plugin (strict superset; that plugin is retired in its favour).

## Quick start

```bash
GEM=~/.claude/skills/gemini-image/scripts/gemini_image.py

# Generate
python "$GEM" --image -o castle.png "A watercolor painting of a medieval castle"

# Pick a model / aspect ratio / resolution
python "$GEM" --image -o hero.png --model gemini-3-pro-image \
    --aspect-ratio 2:3 --image-size 2K "Botanical oil painting of peonies"

# Edit or compose from reference image(s) — repeat -i (limits are per-model, see table below)
python "$GEM" --image -o staged.png -i room.png -i sofa.png \
    "Place this sofa in this room with warm afternoon light"

# See which image models your key can use
python "$GEM" --list-models --images-only
```

Auth resolves in order: `--api-key` → `GEMINI_API_KEY` → `GOOGLE_API_KEY` →
`.env` (cwd, then next to the script). The key is sent as `x-goog-api-key`.

## Models (verified against the live API, 2026-07)

| Model | Marketing name | Status | Use for |
|---|---|---|---|
| `gemini-3-pro-image` / `-preview` | Nano Banana Pro | GA / preview | **Auto-selected default** — highest fidelity, accurate text rendering, 1K/2K/4K, up to 6 object + 5 character reference images. Mockups, design, data-viz. ~$0.134/1K–2K, $0.24/4K. |
| `gemini-3.1-flash-image` / `-preview` | Nano Banana 2 | GA / preview | Balanced, and the **offline/error fallback floor**. Extreme aspect ratios (1:4…8:1), the `512` size, 4K, up to 10 object / 4 character / 3 style reference images, video-to-image. ~$0.067/1K. |
| `gemini-3.1-flash-lite-image` | Nano Banana 2 Lite | GA (2026-06-30) | **Cheapest and fastest** (~$0.0336/image). **1K only** — any other `--image-size` is a hard 400. Standard aspect ratios only; up to 14 object reference images, but no character-consistency or style references. High-volume drafts. |
| `gemini-2.5-flash-image` | Nano Banana | GA — **deprecated** | Shuts down **2026-10-02**. Still callable, but migrate: Google's named replacement is `gemini-3.1-flash-image`. |
| `imagen-4.0-*` | Imagen 4 | **deprecated** | Shuts down **2026-08-17**, and uses the separate `:predict` endpoint — **not** covered by this `generateContent` client. |

Aspect ratios: `1:1 2:3 3:2 3:4 4:3 4:5 5:4 9:16 16:9 21:9`; `gemini-3.1-flash-image`
adds the extreme ratios `1:4 4:1 1:8 8:1`.

Sizes are **per-model, not universal** — the CLI accepts the union and the API is
the arbiter. Measured 2026-07: only `gemini-3-pro-image` and
`gemini-3.1-flash-image` genuinely deliver 2K/4K. `gemini-3.1-flash-lite-image`
returns a 400 for anything but `1K`, and `gemini-2.5-flash-image` *accepts* `2K`
but silently returns a 1024×1024 image.

## Default model: best available, auto-selected

Omit `--model` (CLI) or `model=` (library) and the skill resolves the **best
image model your key can access** — currently `gemini-3-pro-image` — instead of
a fixed id. It stays current automatically: a future GA Pro model (e.g.
`gemini-4-pro-image`) is adopted the day your key gains access, no code change.
A newer *flash* model never displaces a GA *pro* one (newer ≠ higher fidelity).

- **Explicit always wins:** `--model <id>` / `model="<id>"` skips resolution
  entirely (and the `/models` lookup).
- **Pin a default:** `GEMINI_IMAGE_DEFAULT=<id>` hard-pins (e.g. a cost-sensitive
  project pins `gemini-3.1-flash-lite-image`); explicit `--model` still overrides it.
- **Cost/speed:** the best model costs more and is slower than the floor
  (~$0.134 vs ~$0.067/1K image, ~3× latency — Google list pricing, 2026-07). CLI use prints a one-line stderr notice
  when a non-default model is auto-selected; library use stays silent.
- **Never fails:** the resolution is cached per key for 24h (a temp-file keyed by
  a *hash* of the API key — the key itself is never written). If `/models` is
  unreachable it falls back to `gemini-3.1-flash-image`, so a network blip can
  never break generation or cause a surprise bill.
- **Why that floor and not the cheapest model:** the floor has to be non-deprecated,
  honour every `--image-size` the CLI advertises, and stay well below pro pricing.
  `gemini-3.1-flash-lite-image` is cheaper but 1K-only, so a fallback would have
  turned a 2K request into a hard failure; `gemini-2.5-flash-image` is retiring on
  2026-10-02. `gemini-3.1-flash-image` is the only current model that clears all three.

## What it does beyond the old plugin

1. **Reference-image input** (`-i/--input-image`, repeatable) — image editing and
   multi-image composition, which the plugin could not do.
2. **Multi-image output** — when the model returns several images they are all
   saved (`out.png`, `out-2.png`, …) instead of overwriting one file.
3. **Safety-block diagnostics** — a blocked/empty result reports the
   `blockReason` / `finishReason` / safety ratings instead of a bare "no image".
4. **Verified model menu** + **exponential-backoff retry** (429/5xx) + a
   **`GeminiError` exception** contract so projects can vendor the file and build
   thin wrappers (CLI exit 2 = image blocked, 1 = error, 0 = ok).

## Importable

`generate`, `generate_image`, `resolve_best_image_model`, `extract_text`,
`extract_images`, `extract_usage`, `list_models`, and `GeminiError` are public.
`generate_image()` returns
`{output_paths, output_path, model, mime_type, text, blocked, diagnostic, raw}`.

## Calling it from another language

Non-Python callers reuse this skill by shelling out and reading **stdout JSON**
(`--json`), rather than reimplementing the API shape. stderr carries human
notices; exit codes are `0` ok, `2` image blocked, `1` error.

```bash
python "$GEM" --image --json -o out.png "A watercolor castle"
# stdout: {"output_paths":[…], "output_path":"out.jpg", "model":"gemini-3-pro-image", …}
```

Two fields exist specifically for callers and are easy to get wrong:

- **`output_path` — read the saved path from here, never from your own `-o`
  value.** The models choose their own output format, so `-o out.png` routinely
  comes back as `out.jpg`.
- **`model` — the id that actually ran, after resolution.** On the default path
  the model is chosen *here*, at call time, so a caller that records its own
  hard-coded guess for provenance will drift silently the first time resolution
  picks something else.

This is the right integration for **build-time scripts and CLIs**. A compiled
server or MCP runtime can't shell out to a CLI; those conform to the contract in
[`SPEC.md`](SPEC.md) instead of sharing this code.

## Notes

- Every generated image carries an invisible **SynthID** watermark (Google,
  non-optional).
- The saved file's extension matches the model's **actual** output format (the
  image models choose it — `gemini-3-pro-image` returns JPEG). A `.png` request
  that comes back as JPEG is saved as `.jpg` with a stderr note, so a file never
  lies about its contents.
- The request uses `generationConfig.imageConfig` — the shape proven across all
  current production callers and accepted by the live API. The newer
  `responseFormat.image` shape is tracked as a future migration in
  [`SPEC.md`](SPEC.md), which is the canonical contract this skill and the
  project-specific TypeScript callers (ShopForge, ShopSmith, shopforge_v4) all
  conform to.
- Per-project business logic (brand denylists, print-spec validation, Sharp
  compositing, pack rules) stays in those projects — this core only does correct
  generation/editing.
