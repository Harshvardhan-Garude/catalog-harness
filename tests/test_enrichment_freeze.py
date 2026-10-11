import copy
import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enrichment import freeze


class EnrichmentFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixtures = self.root / "fixtures"
        self.artifacts = self.root / "artifacts"
        self.source = self.root / "source.py"
        self.source.write_text("frozen source version one\n")
        self.source_patch = patch.dict(freeze.SOURCE_FILES, {"test/source.py": self.source}, clear=True)
        self.source_patch.start()

    def tearDown(self):
        self.source_patch.stop()
        self.temporary.cleanup()

    def test_preparation_is_idempotent_and_freezes_every_split_without_evaluating(self):
        manifest = freeze.prepare(self.fixtures, self.artifacts)
        snapshot = (self.artifacts / "freeze.json").read_bytes()
        self.assertEqual(freeze.prepare(self.fixtures, self.artifacts), manifest)
        self.assertEqual((self.artifacts / "freeze.json").read_bytes(), snapshot)
        self.assertEqual(freeze.verify(self.fixtures, self.artifacts), manifest)
        expected = {"fixtures/{}/{}.jsonl".format(folder, split)
                    for folder in ("agent_visible", "evaluator_only")
                    for split in ("development", "validation", "final")}
        self.assertTrue(expected <= set(manifest["sha256"]))
        self.assertEqual(manifest["split_counts"], {"development": 24, "validation": 12, "final": 24})
        self.assertFalse(manifest["final_holdout_evaluated"])
        self.assertEqual(manifest["model"], "gpt-4.1-mini")
        self.assertEqual(manifest["resource_caps"]["max_live_calls"], 20)
        self.assertEqual(manifest["resource_caps"]["planned_live_calls"], 19)
        self.assertEqual(manifest["resource_caps"]["automatic_retries"], 0)
        self.assertEqual(stat.S_IMODE((self.artifacts / "freeze.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.artifacts.stat().st_mode), 0o700)
        # Keys and example source contents never appear in the manifest.
        rendered = json.dumps(manifest)
        self.assertNotIn("acceptable_categories", rendered)
        self.assertNotIn("frozen source version one", rendered)

    def test_source_or_answer_key_change_is_rejected_without_replacing_freeze(self):
        freeze.prepare(self.fixtures, self.artifacts)
        original = (self.artifacts / "freeze.json").read_bytes()
        self.source.write_text("changed semantic instructions\n")
        with self.assertRaisesRegex(ValueError, "differ"):
            freeze.verify(self.fixtures, self.artifacts)
        with self.assertRaisesRegex(ValueError, "differ"):
            freeze.prepare(self.fixtures, self.artifacts)
        self.assertEqual((self.artifacts / "freeze.json").read_bytes(), original)
        self.source.write_text("frozen source version one\n")
        label_path = self.fixtures / "evaluator_only" / "final.jsonl"
        label_path.write_text(label_path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "differ"):
            freeze.verify(self.fixtures, self.artifacts)
        self.assertEqual((self.artifacts / "freeze.json").read_bytes(), original)

    def test_verification_never_regenerates_missing_input(self):
        freeze.prepare(self.fixtures, self.artifacts)
        missing = self.fixtures / "agent_visible" / "validation.jsonl"
        missing.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            freeze.verify(self.fixtures, self.artifacts)
        self.assertFalse(missing.exists())
        with self.assertRaisesRegex(ValueError, "missing"):
            freeze.prepare(self.fixtures, self.artifacts)
        self.assertFalse(missing.exists())

    def test_contract_and_manifest_edits_are_rejected(self):
        freeze.prepare(self.fixtures, self.artifacts)
        changed = copy.deepcopy(freeze.RESOURCE_CAPS)
        changed["max_live_calls"] = 21
        with patch.object(freeze, "RESOURCE_CAPS", changed):
            with self.assertRaisesRegex(ValueError, "differ"):
                freeze.verify(self.fixtures, self.artifacts)
        path = self.artifacts / "freeze.json"
        original = path.read_text()
        path.write_text("not JSON\n")
        with self.assertRaisesRegex(ValueError, "invalid"):
            freeze.verify(self.fixtures, self.artifacts)
        self.assertEqual(path.read_text(), "not JSON\n")
        path.write_text(original)
        self.assertEqual(freeze.verify(self.fixtures, self.artifacts)["resource_caps"]["max_live_calls"], 20)

    def test_manifest_digest_is_order_independent_but_content_sensitive(self):
        left = {"b": {"y": 2, "x": 1}, "a": 3}
        right = {"a": 3, "b": {"x": 1, "y": 2}}
        self.assertEqual(freeze.manifest_digest(left), freeze.manifest_digest(right))
        expected = hashlib.sha256(b'{"a":3,"b":{"x":1,"y":2}}').hexdigest()
        self.assertEqual(freeze.manifest_digest(left), expected)
        right["a"] = 4
        self.assertNotEqual(freeze.manifest_digest(left), freeze.manifest_digest(right))


if __name__ == "__main__":
    unittest.main()
