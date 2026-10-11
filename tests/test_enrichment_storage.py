"""Mocked persistence checks preserve model rows and raw-source boundaries."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from enrichment.storage import EnrichmentStore
from tests.test_enrichment_provider import OUTPUT, PRODUCT, USAGE


class SemanticStorageTests(unittest.TestCase):
    def store(self):
        normalizer = MagicMock()
        normalizer.raw.database.__getitem__.side_effect = lambda name: MagicMock(name=name)
        return EnrichmentStore(normalizer)

    def test_results_index_does_not_deduplicate_source_identities(self):
        store = self.store()
        store.ensure_indexes()
        indexes = store.results.create_index.call_args_list
        self.assertEqual(indexes[0].args[0], [("run_id", 1), ("output_index", 1)])
        self.assertTrue(indexes[0].kwargs["unique"])
        self.assertEqual(indexes[1].args[0], [("run_id", 1), ("source_record_id", 1)])
        self.assertFalse(indexes[1].kwargs.get("unique", False))

    def test_duplicate_outputs_keep_distinct_ordinals_and_original_model_predictions(self):
        store = self.store()
        first, second = copy.deepcopy(OUTPUT), copy.deepcopy(OUTPUT)
        second["primary_use"] = "unknown"
        raw_second = copy.deepcopy(OUTPUT)
        raw_second["evidence"][1]["excerpt"] = "Missing source excerpt"
        original_checks = [{"field": "primary_use", "code": "excerpt_not_found", "label": "drink_beverages"}]
        details = {"model": "gpt-4.1-mini", "usage": USAGE,
                   "model_outputs": [first, raw_second], "original_evidence_checks_by_output": [[], original_checks]}
        store.save_batch("run-1", [first, second], details, {"opaque-001": []}, offset=4)
        operations = store.results.bulk_write.call_args.args[0]
        self.assertEqual(len(operations), 2)
        for ordinal, operation in enumerate(operations, 4):
            self.assertEqual(operation._filter, {"run_id": "run-1", "output_index": ordinal})
            document = operation._doc["$setOnInsert"]
            self.assertEqual(document["source_record_id"], "opaque-001")
            self.assertEqual(document["output_index"], ordinal)
            self.assertEqual(document["usage_batch"], USAGE)
        self.assertEqual(operations[1]._doc["$setOnInsert"]["original_model_output"], raw_second)
        self.assertEqual(operations[1]._doc["$setOnInsert"]["original_evidence_checks"], original_checks)
        self.assertEqual(operations[1]._doc["$setOnInsert"]["output"], second)
        store.normalizer.raw.update_one.assert_not_called()
        store.normalizer.raw.bulk_write.assert_not_called()

    def test_negative_or_boolean_offsets_fail_before_write(self):
        for offset in (-1, True):
            store = self.store()
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                store.save_batch("run", [OUTPUT], {"model": "gpt-4.1-mini", "usage": USAGE}, {}, offset=offset)
            store.results.bulk_write.assert_not_called()

    def test_failed_run_stores_allowlisted_error_metadata_only(self):
        store = self.store()
        store.fail_run("run", "private-category", "private-secret-error")
        update = store.runs.update_one.call_args.args[1]["$set"]
        self.assertEqual(update["status"], "failed")
        self.assertEqual(update["failure"], {"category": "local", "code": "verification_failed"})
        self.assertNotIn("private", str(update))
        store.fail_run("run", "quota_or_rate_limit", "http_429")
        self.assertEqual(store.runs.update_one.call_args.args[1]["$set"]["failure"],
                         {"category": "quota_or_rate_limit", "code": "http_429"})

    def test_source_preparation_verifies_repeatable_seed_and_normalized_identity(self):
        store = self.store()
        digest = {"dataset_id": "semantic-v1-development", "count": 1, "sha256": "digest"}
        store.normalizer.seed.return_value = digest
        store.normalizer.raw_digest.return_value = {"count": 1, "sha256": "digest"}
        store.normalizer.run_results.return_value = [{"source_record_id": PRODUCT["source_record_id"],
            "status": "normalized", "product": copy.deepcopy(PRODUCT["normalized_product"])}]
        with tempfile.TemporaryDirectory() as directory, patch("enrichment.storage.run_fixture", return_value="norm-run"):
            result = store.prepare_sources([PRODUCT], "development", directory)
            self.assertTrue((Path(directory) / "development/records.jsonl").exists())
        self.assertTrue(result["seed_repeatable"])
        self.assertEqual(result["normalization_run_id"], "norm-run")
        self.assertEqual(store.normalizer.seed.call_count, 2)

    def test_source_preparation_rejects_changed_normalization_or_final_holdout(self):
        for split, normalized in (("final", PRODUCT["normalized_product"]), ("development", {"product_id": "wrong"})):
            store = self.store()
            store.normalizer.seed.return_value = {"count": 1, "sha256": "digest"}
            store.normalizer.run_results.return_value = [{"source_record_id": PRODUCT["source_record_id"],
                                                        "status": "normalized", "product": normalized}]
            with tempfile.TemporaryDirectory() as directory, patch("enrichment.storage.run_fixture", return_value="norm-run"), \
                    self.subTest(split=split), self.assertRaises(ValueError):
                store.prepare_sources([PRODUCT], split, directory)
            if split == "final":
                store.normalizer.seed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
