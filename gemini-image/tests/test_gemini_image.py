#!/usr/bin/env python3
"""
Tests for gemini_image.py — focused on the best-available-model resolver.

Fully mocked: NO network, NO real API key, NO billed calls. Run with:
    python -m unittest discover -s gemini-image/tests
or  python gemini-image/tests/test_gemini_image.py
"""

import base64
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_GI_PATH = os.path.join(_HERE, "..", "scripts", "gemini_image.py")
_spec = importlib.util.spec_from_file_location("gemini_image_under_test", _GI_PATH)
gi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gi)


def _model(name, methods=("generateContent",)):
    return {"name": f"models/{name}", "supportedGenerationMethods": list(methods)}


# A realistic live-ish /models fixture for this account.
FIXTURE = [
    _model("gemini-2.5-flash"),
    _model("gemini-2.5-flash-image"),
    _model("gemini-3-pro-image"),
    _model("gemini-3-pro-image-preview"),
    _model("gemini-3.1-flash-image"),
    _model("gemini-3.1-flash-image-preview"),
    # Imagen is image-named but uses :predict, not generateContent → must be dropped.
    _model("imagen-4.0-generate-001", methods=("predict",)),
]


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="gi-cache-test-")
        self._orig_cache_dir = gi._CACHE_DIR
        gi._CACHE_DIR = self._tmp
        self._orig_list_models = gi.list_models
        self._orig_cli = gi._CLI_MODE
        gi._CLI_MODE = False
        self._orig_pin = os.environ.pop("GEMINI_IMAGE_DEFAULT", None)

    def tearDown(self):
        gi._CACHE_DIR = self._orig_cache_dir
        gi.list_models = self._orig_list_models
        gi._CLI_MODE = self._orig_cli
        os.environ.pop("GEMINI_IMAGE_DEFAULT", None)
        if self._orig_pin is not None:
            os.environ["GEMINI_IMAGE_DEFAULT"] = self._orig_pin
        for f in os.listdir(self._tmp):
            os.remove(os.path.join(self._tmp, f))
        os.rmdir(self._tmp)

    # ---- sort key (sharp, not wrapped in the resolver's try/except) ----------
    def test_sort_key_tier_then_ga_then_version(self):
        k = gi._image_model_sort_key
        # pro beats flash beats legacy-flash, regardless of recency
        self.assertGreater(k("gemini-3-pro-image"), k("gemini-3.1-flash-image"))
        self.assertGreater(k("gemini-3.1-flash-image"), k("gemini-2.5-flash-image"))
        # GA beats preview at the same tier
        self.assertGreater(k("gemini-3-pro-image"), k("gemini-3-pro-image-preview"))
        # version parsed numerically: a future gemini-4 pro outranks gemini-3 pro
        self.assertGreater(k("gemini-4-pro-image"), k("gemini-3-pro-image"))
        # numeric, not lexical: 3.10 > 3.2
        self.assertGreater(k("gemini-3.10-pro-image"), k("gemini-3.2-pro-image"))
        # a newer flash never outranks an existing GA pro
        self.assertGreater(k("gemini-3-pro-image"), k("gemini-4-flash-image"))

    # ---- ranking against a live list -----------------------------------------
    def test_resolves_best_quality_default(self):
        gi.list_models = lambda api_key=None: FIXTURE
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), "gemini-3-pro-image")

    def test_imagen_predict_models_filtered_out(self):
        # Only imagen (predict) present → no drivable image model → static floor.
        gi.list_models = lambda api_key=None: [_model("imagen-4.0-generate-001", methods=("predict",))]
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), gi.DEFAULT_IMAGE_MODEL)

    def test_future_ga_pro_auto_adopted(self):
        fut = FIXTURE + [_model("gemini-4-pro-image")]
        gi.list_models = lambda api_key=None: fut
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), "gemini-4-pro-image")

    def test_future_flash_does_not_outrank_ga_pro(self):
        fut = FIXTURE + [_model("gemini-4-flash-image")]
        gi.list_models = lambda api_key=None: fut
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), "gemini-3-pro-image")

    def test_ga_pro_variant_beats_higher_versioned_curated_flash(self):
        # Finding #1, Case 1: a GA pro NOT in the curated list (so curated best
        # is a flash) whose version is <= the flash's must still win on tier.
        models = [_model("gemini-2.5-flash-image"), _model("gemini-3.1-flash-image"),
                  _model("gemini-3.1-flash-image-preview"), _model("gemini-3-pro-image-hd")]
        gi.list_models = lambda api_key=None: models
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), "gemini-3-pro-image-hd")

    def test_ga_pro_variant_beats_curated_preview_pro(self):
        # Finding #1, Case 2: a GA pro not in the curated list must beat a curated
        # *preview* pro on the ga component, not be masked by it.
        models = [_model("gemini-3.1-flash-image"), _model("gemini-3-pro-image-preview"),
                  _model("gemini-3-pro-image-max")]
        gi.list_models = lambda api_key=None: models
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), "gemini-3-pro-image-max")

    def test_non_dict_cache_json_is_a_miss(self):
        # Finding #2: valid JSON whose top-level is not a dict must be a miss, not
        # an AttributeError that pins the key to the floor forever.
        key_hash = "0123456789abcdef"
        with open(gi._model_cache_path(key_hash), "w") as f:
            f.write("[1, 2, 3]")
        self.assertIsNone(gi._read_model_cache(key_hash))

    def test_non_dict_cache_does_not_pin_to_floor(self):
        import hashlib
        key_hash = hashlib.sha256(b"k1").hexdigest()[:16]
        with open(gi._model_cache_path(key_hash), "w") as f:
            f.write("[1, 2, 3]")
        gi.list_models = lambda api_key=None: FIXTURE
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), "gemini-3-pro-image")

    def test_preview_only_pro_selected_when_no_ga_peer(self):
        models = [_model("gemini-2.5-flash-image"),
                  _model("gemini-3-pro-image-preview")]  # no GA pro
        gi.list_models = lambda api_key=None: models
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"),
                         "gemini-3-pro-image-preview")

    # ---- infallible fallback -------------------------------------------------
    def test_list_models_error_falls_back_to_default(self):
        def boom(api_key=None):
            raise gi.GeminiError("boom")
        gi.list_models = boom
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), gi.DEFAULT_IMAGE_MODEL)

    def test_empty_available_falls_back_to_default(self):
        gi.list_models = lambda api_key=None: [_model("gemini-2.5-flash")]  # text only
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), gi.DEFAULT_IMAGE_MODEL)

    # ---- env pin -------------------------------------------------------------
    def test_env_pin_wins_over_resolution_and_skips_network(self):
        os.environ["GEMINI_IMAGE_DEFAULT"] = "pinned-model-xyz"
        def boom(api_key=None):
            raise AssertionError("network must not be hit when pinned")
        gi.list_models = boom
        self.assertEqual(gi.resolve_best_image_model(api_key="k1"), "pinned-model-xyz")

    # ---- caching -------------------------------------------------------------
    def test_cache_hit_avoids_second_fetch(self):
        calls = []
        gi.list_models = lambda api_key=None: (calls.append(1), FIXTURE)[1]
        first = gi.resolve_best_image_model(api_key="k1")
        second = gi.resolve_best_image_model(api_key="k1")
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "second resolve should hit cache, not refetch")

    def test_corrupt_cache_is_a_miss_not_an_error(self):
        key_hash = __import__("hashlib").sha256(b"k1").hexdigest()[:16]
        with open(gi._model_cache_path(key_hash), "w") as f:
            f.write("{ this is not valid json")
        self.assertIsNone(gi._read_model_cache(key_hash))

    def test_expired_cache_is_a_miss(self):
        key_hash = "deadbeefdeadbeef"
        gi._write_model_cache(key_hash, "gemini-3-pro-image", ["gemini-3-pro-image"])
        # backdate ts beyond TTL
        path = gi._model_cache_path(key_hash)
        with open(path) as f:
            entry = json.load(f)
        entry["ts"] = int(time.time()) - gi._MODEL_CACHE_TTL - 10
        with open(path, "w") as f:
            json.dump(entry, f)
        self.assertIsNone(gi._read_model_cache(key_hash))

    def test_stale_ranking_fingerprint_is_a_miss(self):
        # review-loop finding: a cache written under a different ranking must be a
        # miss, so editing IMAGE_MODEL_PREFERENCE never serves a stale default for
        # up to the 24h TTL.
        key_hash = "feedfacefeedface"
        gi._write_model_cache(key_hash, "gemini-3-pro-image", ["gemini-3-pro-image"])
        path = gi._model_cache_path(key_hash)
        with open(path) as f:
            entry = json.load(f)
        entry["rank_fp"] = "stale-fingerprint"
        with open(path, "w") as f:
            json.dump(entry, f)
        self.assertIsNone(gi._read_model_cache(key_hash))

    def test_fresh_cache_round_trips(self):
        key_hash = "cafebabecafebabe"
        gi._write_model_cache(key_hash, "gemini-3-pro-image", ["a", "b"])
        entry = gi._read_model_cache(key_hash)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["best"], "gemini-3-pro-image")

    # ---- key privacy ---------------------------------------------------------
    def test_plaintext_key_never_persisted(self):
        secret = "AIzaSyTOTALLYSECRETKEY0123456789"
        gi.list_models = lambda api_key=None: FIXTURE
        gi.resolve_best_image_model(api_key=secret)
        files = os.listdir(self._tmp)
        self.assertTrue(files)
        for name in files:
            self.assertNotIn(secret, name)
            with open(os.path.join(self._tmp, name)) as f:
                self.assertNotIn(secret, f.read())

    def test_per_key_isolation_two_files(self):
        gi.list_models = lambda api_key=None: FIXTURE
        gi.resolve_best_image_model(api_key="keyAAA")
        gi.resolve_best_image_model(api_key="keyBBB")
        self.assertEqual(len(os.listdir(self._tmp)), 2)


class OverrideAndWiringTest(unittest.TestCase):
    """generate_image override semantics (mock generate; no network/files-on-network)."""

    def setUp(self):
        self._orig_generate = gi.generate
        self._orig_resolve = gi.resolve_best_image_model
        self._orig_list_models = gi.list_models
        self._tmpfile = os.path.join(tempfile.mkdtemp(prefix="gi-out-"), "out.png")

    def tearDown(self):
        gi.generate = self._orig_generate
        gi.resolve_best_image_model = self._orig_resolve
        gi.list_models = self._orig_list_models

    def _fake_image_response(self):
        data = base64.b64encode(b"PNGBYTES").decode("ascii")
        return {"candidates": [{"content": {"parts": [
            {"inline_data": {"mime_type": "image/png", "data": data}}]}}]}

    def test_explicit_model_bypasses_resolver_and_network(self):
        captured = {}

        def fake_generate(prompt, **kw):
            captured["model"] = kw.get("model")
            return self._fake_image_response()
        gi.generate = fake_generate
        gi.list_models = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("explicit model must not trigger /models"))
        gi.resolve_best_image_model = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("explicit model must not call the resolver"))

        res = gi.generate_image("a prompt", self._tmpfile,
                                api_key="k", model="explicit-model-9")
        self.assertEqual(captured["model"], "explicit-model-9")
        self.assertEqual(res["output_paths"], [self._tmpfile])

    def test_omitted_model_invokes_resolver(self):
        captured = {}

        def fake_generate(prompt, **kw):
            captured["model"] = kw.get("model")
            return self._fake_image_response()
        gi.generate = fake_generate
        gi.resolve_best_image_model = lambda api_key=None, **k: "RESOLVED-BEST"

        gi.generate_image("a prompt", self._tmpfile, api_key="k")  # no model=
        self.assertEqual(captured["model"], "RESOLVED-BEST")

    def test_default_param_is_lazy_sentinel_not_a_resolved_string(self):
        # Guards against resolution leaking into a default-arg expression (which
        # would fire a network call at import time). The default must be _RESOLVE.
        import inspect
        sig = inspect.signature(gi.generate_image)
        self.assertIs(sig.parameters["model"].default, gi._RESOLVE)

    def test_empty_string_model_resolves_like_cli(self):
        # Finding #4: a falsy explicit model must resolve on the library path too
        # (the CLI's `args.model or _RESOLVE` already does), not build a bad URL.
        captured = {}

        def fake_generate(prompt, **kw):
            captured["model"] = kw.get("model")
            return self._fake_image_response()
        gi.generate = fake_generate
        gi.resolve_best_image_model = lambda api_key=None, **k: "RESOLVED"
        gi.generate_image("p", self._tmpfile, api_key="k", model="")
        self.assertEqual(captured["model"], "RESOLVED")

    def test_jpeg_response_saved_with_correct_extension(self):
        # Real models (gemini-3-pro-image) return JPEG; a .png request must NOT
        # produce a .png file that actually holds JPEG bytes (caught by the live
        # smoke test, invisible to PNG-mocked tests).
        data = base64.b64encode(b"\xff\xd8\xff\xe0JFIFDATA").decode("ascii")
        resp = {"candidates": [{"content": {"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": data}}]}}]}
        gi.generate = lambda prompt, **kw: resp
        out = os.path.join(tempfile.mkdtemp(prefix="gi-ext-"), "pic.png")
        res = gi.generate_image("p", out, api_key="k", model="x")
        self.assertTrue(res["output_path"].endswith(".jpg"))
        self.assertFalse(os.path.exists(out))           # the misleading .png never created
        self.assertTrue(os.path.exists(res["output_path"]))

    def test_save_failure_raises_clean_geminierror(self):
        # Finding #3: a bad output path (parent is a file) must raise GeminiError,
        # not an uncaught OSError, after the (billed) generate() call.
        d = tempfile.mkdtemp(prefix="gi-save-")
        blocker = os.path.join(d, "sub")
        with open(blocker, "w"):
            pass  # a FILE where a directory component is needed
        gi.generate = lambda prompt, **kw: self._fake_image_response()
        with self.assertRaises(gi.GeminiError):
            gi.generate_image("p", os.path.join(blocker, "img.png"),
                              api_key="k", model="explicit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
