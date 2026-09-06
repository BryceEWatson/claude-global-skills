# Gemini image-generation — shared contract (SPEC)

The single source of truth for how Bryce's projects call the Gemini image API.
The Python core in this skill is the reference implementation; the TypeScript
callers (ShopForge, ShopSmith\_v2, ShopSmithCowork, shopforge\_v4) each have their
own embedded code but **must conform to this contract**. When the API changes,
update this file first, then the reference core, then reconcile the callers.

> Provenance: model IDs, aspect ratios, and sizes below are **[VERIFIED]**
> against the live `/v1beta/models` endpoint (2026-06). The `responseFormat.image`
> shape is **[DERIVED]** from Google docs and is *not yet verified against a live
> generate call* — see §3.

## 1. Endpoint & auth

```
POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
Header: x-goog-api-key: <API_KEY>      # NOT ?key= ; both work, this is canonical
Content-Type: application/json
```

Key resolution order (all callers): explicit arg → `GEMINI_API_KEY` →
`GOOGLE_API_KEY` → `.env`. Never hard-code; never log the key.

## 2. Models [VERIFIED 2026-07]

| Model ID | Status | Notes |
|---|---|---|
| `gemini-3-pro-image` | **GA** | "Nano Banana Pro". Highest fidelity; ≤6 object / ≤5 character reference images; 1K/2K/4K. The resolver's **auto-selected default**. |
| `gemini-3-pro-image-preview` | preview | Alias `nano-banana-pro-preview`. |
| `gemini-3.1-flash-image` | **GA** | "Nano Banana 2". Extreme ratios, `512` size, 4K, ≤10 object / ≤4 character / ≤3 style ref images, video-in. The resolver's offline/error **fallback floor** (since 2026-07). |
| `gemini-3.1-flash-image-preview` | preview | |
| `gemini-3.1-flash-lite-image` | **GA** (2026-06-30) | "Nano Banana 2 Lite". Cheapest/fastest (~$0.0336/image); **`1K` only** (400 otherwise), standard ratios only, ≤14 object ref images, **no** character-consistency or style references. |
| `gemini-2.5-flash-image` | GA — **deprecated** | "Nano Banana". Shutdown **2026-10-02**; replacement `gemini-3.1-flash-image`. Was the fallback floor until 2026-07. |
| `imagen-4.0-{generate,ultra,fast}-001` | **deprecated** | Shutdown **2026-08-17**. Also a **different endpoint** (`:predict`); out of scope for the generateContent contract. |

> Correction vs. earlier web research: `gemini-3-pro-image` and
> `gemini-3.1-flash-image` are now **GA**, not preview-only. Prefer the GA IDs
> for production; keep `-preview` only where a feature is preview-gated.
> **Retired:** `gemini-2.0-flash-exp-image-generation` /
> `gemini-2.0-flash-preview-image-generation` — low resolution (~680×1024), being
> deprecated. Any caller still on these should move to `gemini-3.1-flash-image`
> or `gemini-3-pro-image`. (See conformance — shopforge\_v4.)
>
> **2026-07 note — "newest" ≠ "best".** The most recent image release
> (`gemini-3.1-flash-lite-image`, GA 2026-06-30) is the *cheapest* tier, not a new
> state of the art. `gemini-3-pro-image` remains the fidelity flagship, so the
> auto-selected default is unchanged. A caller that blindly adopts "the latest
> model id" would silently downgrade its own output quality.

## 2.1 Default-model resolution (reference core behaviour)

When a caller supplies **no** model, the reference Python core resolves the best
image model available to the active key rather than hard-coding one. TS callers
are not required to implement this (they pass explicit models), but it is the
recommended default behaviour and documented here so the contract is one place.

**Precedence:** explicit `--model`/`model=` > `GEMINI_IMAGE_DEFAULT` env pin >
resolved-best (cache → live `/models`) > static floor `gemini-3.1-flash-image`.
Explicit model bypasses resolution and the `/models` call entirely.

**Degradation ladder (never raises — resolution can't break generation):**
`GEMINI_IMAGE_DEFAULT` → fresh per-key cache → live `/models` ranked → static
`gemini-3.1-flash-image`.

**Floor selection criteria (all three are binding).** The floor is what a network
blip silently falls back to, so it must be (1) **not deprecated**, (2) able to
honour **every** `--image-size` the client advertises, and (3) **well below pro
pricing** so a blip can never cause a surprise bill. As of 2026-07,
`gemini-3.1-flash-image` is the only model that clears all three; the previous
floor `gemini-2.5-flash-image` fails (1) — shutdown 2026-10-02 — and also (2),
and the cheaper `gemini-3.1-flash-lite-image` fails (2). Measured 2026-07 on a
`2K` request: pro → 2048×2048, `3.1-flash` → 2048×2048, `2.5-flash` → **silently
1024×1024**, `3.1-flash-lite` → **HTTP 400**.

**Ranking** (the `/models` response carries no tier/quality field — every 3.x
image model reports version `3.0`, so a curated order is required):
1. Filter live models to image generators we can drive: id matches `(image|imagen)`
   **and** `generateContent` ∈ `supportedGenerationMethods` (drops Imagen `:predict`).
2. Curated fast path: highest-ranked id in `IMAGE_MODEL_PREFERENCE`
   (`gemini-3-pro-image` > `…-pro-image-preview` > `gemini-3.1-flash-image` >
   `…-flash-image-preview` > `gemini-3.1-flash-lite-image` >
   `gemini-2.5-flash-image`) that is available. Lite outranks the 2.5 legacy on
   curated judgement — newer generation *and* cheaper — which the generic sort
   key alone would not produce, since it tiers `lite` below any `flash`.
3. Future-model path: a live model auto-wins **only** if it passes all gates —
   tier (`pro`, by **whole-token** match so "mini"⊄"geMINI"), GA (no
   `preview`/`exp`/dated-preview suffix), endpoint (in the filtered set), and a
   **numeric** version strictly greater than the curated best (so a new
   `gemini-4-pro-image` GA is adopted with no code edit; a newer *flash* never
   outranks a GA *pro*).

**Cache:** one JSON file per key under `tempfile.gettempdir()/gemini-image-cache/`,
24h TTL, schema `{v, rank_fp, ts, key_hash, best, available}` (`rank_fp` invalidates
the cache when the ranking logic changes, not just on a schema bump), atomic `os.replace` write,
defensive read (any corruption/expiry = miss). Scoped by `sha256(key)[:16]` — the
**plaintext key is never written**.

**Disclosure:** CLI prints one stderr line when a non-floor model is auto-selected
(cost/latency + override hint); library use is silent. The best default costs
~3.4× the old floor (~$0.039 → ~$0.134/img [DERIVED from Google list image pricing, ~2026-06; approximate]) and ~3× latency — surfaced, not silent.

**Future-family maintenance:** the comparator infers tier from the *name*, not
measured fidelity. A flagship renamed off the `*-pro-image` convention won't
auto-win (safe — falls to curated/floor); a regressed future "pro" could be
auto-crowned. Mitigations: `GEMINI_IMAGE_DEFAULT`, per-call `--model`, and a
periodic human review of new model families + `IMAGE_MODEL_PREFERENCE`.

## 3. Request body

**Canonical shape today (use this):**

```jsonc
{
  "contents": [{
    "role": "user",
    "parts": [
      { "text": "<prompt>" },
      { "inline_data": { "mime_type": "image/png", "data": "<BASE64>" } }  // optional reference image(s)
    ]
  }],
  "generationConfig": {
    "responseModalities": ["TEXT", "IMAGE"],
    "imageConfig": { "aspectRatio": "16:9", "imageSize": "2K" }
  }
}
```

- `imageConfig` is the shape **all five production callers use** and the live API
  accepts it. It is the default in the reference core. **[VERIFIED in production]**
- **Future migration [DERIVED, unverified]:** Google docs describe a newer
  `generationConfig.responseFormat.image: { aspectRatio, imageSize }` shape;
  `imageConfig` is said to be translated to it with a deprecation warning. Do
  **not** switch callers to `responseFormat.image` until it is verified against a
  live generate call and shown to work across the models in use. Track here; flip
  the default in the reference core in one change when verified.

**Aspect ratios [VERIFIED]:** `1:1 2:3 3:2 3:4 4:3 4:5 5:4 9:16 16:9 21:9`
(plus `1:4 4:1 1:8 8:1` on `gemini-3.1-flash-image`).

**Sizes [VERIFIED 2026-07] — per-model, NOT universal.** Uppercase `K` required.
The union the client accepts is `512 1K 2K 4K`, but the API is the arbiter and
the failure modes differ, so a caller must not assume a size is honoured:

| Model | `512` | `1K` | `2K` / `4K` |
|---|---|---|---|
| `gemini-3-pro-image` | ✖ | ✅ | ✅ (measured 2048×2048 at `2K`) |
| `gemini-3.1-flash-image` | ✅ | ✅ | ✅ (measured 2048×2048 at `2K`) |
| `gemini-3.1-flash-lite-image` | ✖ | ✅ | ✖ **HTTP 400** `"Image size 2K is not supported for this model"` |
| `gemini-2.5-flash-image` | ✖ | ✅ | ⚠️ **accepted but silently downgraded** — a `2K` request returned 1024×1024 |

The `gemini-2.5-flash-image` row is the dangerous one: it returns success, so a
caller that trusts its own request parameters will believe it has a 2K asset.
Verify output dimensions rather than assuming the requested size was honoured.

## 4. Response & image extraction

Image bytes come back inline, base64, inside a content part. **Handle both key
casings** — the REST API returns snake\_case `inline_data`/`mime_type`; some SDKs
surface camelCase `inlineData`/`mimeType`:

```jsonc
"candidates": [{ "content": { "parts": [
  { "text": "..." },
  { "inline_data": { "mime_type": "image/png", "data": "<base64>" } }
]}}]
```

A response may contain **multiple** image parts — save them all, do not overwrite.

## 5. Failure handling (required of every caller)

- **Retry** 429 and 5xx with exponential backoff (reference core: 4 retries,
  2→4→8→16s; cap 30s). Do **not** retry 4xx other than 429.
- **No-image diagnosis:** when no image part is returned, inspect
  `promptFeedback.blockReason`, `candidates[0].finishReason`
  (`SAFETY`/`IMAGE_SAFETY`/`PROHIBITED_CONTENT`/`RECITATION`), and `safetyRatings`,
  and surface a specific reason — never a bare "no image".
- **Content-policy reprompt** (optional, project-level): softening a first-attempt
  safety block by prepending "artistic, painterly, classical fine art" is an
  established pattern (ShopSmithCowork) but belongs in the *project*, not the core.

## 6. Watermarking

Every generated image carries an invisible **SynthID** watermark. Non-optional.
Do not claim images are watermark-free.

## 7. What stays in the project (NOT in the core)

Brand/content denylists, the "10 inviolable content rules", print-spec validation
(≥3000px, 300 DPI), Sharp compositing / infographics, green-screen mockups,
blob-store + learning loops, pack-assembly rules, prompt templates. The core does
correct generation/editing only.

## 8. Conformance (status today → migration backlog)

| Caller | Lang | Model | Config shape | Ref-image input | Multi-image save | Safety diag | Conforms? |
|---|---|---|---|---|---|---|---|
| **gemini-image core** (this skill) | Py | best-available (def); 3.1-flash floor | imageConfig | ✅ | ✅ | ✅ | reference |
| **seo-topic-funnels** | Py | 3-pro-image-preview | imageConfig (SDK) | ✖ | ✖ | partial | conforms via SDK wrapper; left as-is (cold; SDK-keyed governance makes vendoring a net downgrade) |
| **shopforge\_v4** | TS | ⚠️ `gemini-2.0-flash-preview-image-generation` (retired) | imageConfig | ✖ | ✖ | ✖ | **model upgrade needed** |
| **ShopSmith\_v2** | TS | 3-pro-image-preview | imageConfig | ✖ | n/a | classified errors | OK (legacy SDK) |
| **ShopSmithCowork** | TS | dual: 3-pro / 2.5-flash / 2.0-exp | imageConfig | ✖ | ✅ | reprompt | ⚠️ **deadline: 2.5-flash shuts down 2026-10-02** — move to `gemini-3.1-flash-image`; drop 2.0-exp path |
| **ShopForge** | TS | 3-pro-image-preview | imageConfig | ✖ | n/a | retry only | OK; could prefer GA `gemini-3-pro-image` |

Legend: "n/a" = single-image-by-design. The TS callers are **not** being merged
into the Python core (they are compiled into MCP servers / app runtimes and a
CLI can't be imported into them); they conform to this *contract*, not to shared
*code*. Revisit a shared TS package only if the duplication cost grows.

### 8.1 Build-time scripts SHOULD shell out, not conform

The "conform, don't share" rule above is scoped to **compiled runtimes**. It does
not apply to build-time scripts and CLIs, which can and should reuse the Python
core directly rather than carrying their own copy of the API shape:

```bash
python <skill>/scripts/gemini_image.py --image --json -o out.png "<prompt>"
# stdout = JSON; stderr = notices; exit 0 ok / 2 blocked / 1 error
# {"output_paths": […], "output_path": "out.jpg", "model": "gemini-3-pro-image",
#  "mime_type": "image/jpeg", "text": "", "blocked": false, "diagnostic": null}
```

Deciding factor: can the caller depend on `python` and on `~/.claude` at the
moment it generates? A locally-run generation script can; a shipped server or an
MCP server cannot. Check whether the script runs in CI before assuming it can.

Two response fields exist for callers and are the usual sources of bugs:

- **`output_path`** — the authoritative saved path. Callers must not assume the
  `-o` name they passed; the models pick their own format and `-o x.png` commonly
  returns `x.jpg`.
- **`model`** — the id that actually ran, *after* resolution. On the default path
  the core chooses the model at call time, so any caller that records provenance
  MUST take it from here. Recording a hard-coded model id alongside an
  auto-resolved generation produces an asset ledger that is quietly wrong.
