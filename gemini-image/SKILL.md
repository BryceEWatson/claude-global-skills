---
name: gemini-image
description: >-
  Generate and edit images with Google's Gemini models (Nano Banana / Gemini 3
  Pro Image / 3.1 Flash Image) from a zero-dependency Python CLI. Use whenever
  the user wants to create, generate, edit, restyle, or compose an image with
  Gemini / Google AI; produce listing photos, mockups, banners, icons, or
  marketing visuals via Gemini; or attach reference images for image-to-image
  editing. Also handles text/conversation/model-listing — it is the maintained
  successor to the older `gemini-client` plugin. Triggers: "generate an image
  with Gemini", "use Google AI to make a picture", "Gemini image", "nano
  banana", "edit this image with Gemini", "Imagen".
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

# Edit or compose from reference image(s) — repeat -i (3-pro up to 8, 3.1-flash up to 14)
python "$GEM" --image -o staged.png -i room.png -i sofa.png \
    "Place this sofa in this room with warm afternoon light"

# See which image models your key can use
python "$GEM" --list-models --images-only
```

Auth resolves in order: `--api-key` → `GEMINI_API_KEY` → `GOOGLE_API_KEY` →
`.env` (cwd, then next to the script). The key is sent as `x-goog-api-key`.

## Models (verified against the live API, 2026-06)

| Model | Status | Use for |
|---|---|---|
| `gemini-3-pro-image` / `-preview` | GA / preview | **Auto-selected default** — highest fidelity, accurate text rendering, up to 8 reference images. Mockups, design, data-viz. |
| `gemini-3.1-flash-image` / `-preview` | GA / preview | Balanced (~half the cost, close to Pro on most prompts). Extreme aspect ratios (1:4…8:1), the `512` size, up to 14 reference images, video-to-image. |
| `gemini-2.5-flash-image` | GA | Cheapest/fastest, lowest fidelity. The **offline/error fallback floor**. |
| `imagen-4.0-*` | GA | Pure text-to-image via the separate `:predict` endpoint — **not** covered by this `generateContent` client. |

Aspect ratios: `1:1 2:3 3:2 3:4 4:3 4:5 5:4 9:16 16:9 21:9` (3.1-flash adds the
extreme ratios). Sizes: `1K 2K 4K` universally; `512` only on `gemini-3.1-flash-image`.

## Default model: best available, auto-selected

Omit `--model` (CLI) or `model=` (library) and the skill resolves the **best
image model your key can access** — currently `gemini-3-pro-image` — instead of
a fixed id. It stays current automatically: a future GA Pro model (e.g.
`gemini-4-pro-image`) is adopted the day your key gains access, no code change.
A newer *flash* model never displaces a GA *pro* one (newer ≠ higher fidelity).

- **Explicit always wins:** `--model <id>` / `model="<id>"` skips resolution
  entirely (and the `/models` lookup).
- **Pin a default:** `GEMINI_IMAGE_DEFAULT=<id>` hard-pins (e.g. a cost-sensitive
  project pins `gemini-2.5-flash-image`); explicit `--model` still overrides it.
- **Cost/speed:** the best model costs more and is slower than the old default
  (approx. ~$0.13 vs ~$0.04/image, ~3× latency — Google list pricing, 2026-06). CLI use prints a one-line stderr notice
  when a non-default model is auto-selected; library use stays silent.
- **Never fails:** the resolution is cached per key for 24h (a temp-file keyed by
  a *hash* of the API key — the key itself is never written). If `/models` is
  unreachable it falls back to `gemini-2.5-flash-image`, so a network blip can
  never break generation or cause a surprise bill.

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
`{output_paths, output_path, mime_type, text, blocked, diagnostic, raw}`.

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
