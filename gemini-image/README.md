# gemini-image

Generate and edit images with Google's Gemini API from a single, zero-dependency
Python file. Works as a Claude Code skill **and** as a standalone CLI.

- Python **standard library only** — no `pip install` required.
- Reference-image input, multi-image output, safety-block diagnostics.
- Auto-selects the best available image model, cached per API key.

## Setup

You need a Google Gemini API key ([aistudio.google.com](https://aistudio.google.com/apikey)).

```bash
# Option A: environment variable
export GEMINI_API_KEY=your-key-here      # or GOOGLE_API_KEY

# Option B: a .env file next to the script (or in your cwd)
cp gemini-image/.env.example gemini-image/.env   # then edit it
```

The key is never written to disk; the model cache stores only a hash of it.

## Use

```bash
# Generate
python gemini-image/scripts/gemini_image.py "a watercolor fox in a misty forest" -o fox.png

# Edit / image-to-image (pass one or more reference images)
python gemini-image/scripts/gemini_image.py "make it nighttime" -i fox.png -o fox_night.png
```

As a Claude Code skill, copy the directory to `~/.claude/skills/` and it triggers
on image-generation requests.

## Contract & tests

`SPEC.md` is the shared API contract (model IDs, config shape, conformance). Run
the mocked test suite (no network, no real key, no billed calls):

```bash
python -m unittest discover -s gemini-image/tests -p 'test_*.py'
```
