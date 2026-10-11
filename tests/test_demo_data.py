"""Offline demo data, placement safety, and Atlas provenance contracts."""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_harness.storage import ROOT, fingerprint, stable_json
from demo_app.data import DemoRepository, EXPERIMENT_ID, PLACEMENTS_COLLECTION, _raw_digest
from tests.test_enrichment_provider import OUTPUT, PRODUCT


class Cursor(list):
    def sort(self, *args):
        return self

    def limit(self, count):
        return Cursor(self[:count])


class Collection:
    def __init__(self, rows=()):
        self.rows = copy.deepcopy(list(rows))
        self.writes = []

    def find(self, query, projection=None):
        return Cursor(copy.deepcopy([row for row in self.rows if all(row.get(key) == value for key, value in query.items())]))

    def find_one(self, query, projection=None):
        found = self.find(query)
        return found[0] if found else None

    def create_index(self, *args, **kwargs):
        pass

    def update_one(self, query, update, upsert=False):
        self.writes.append(copy.deepcopy((query, update)))
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                row.update(copy.deepcopy(update["$set"]))
                return
        if upsert:
            self.rows.append(copy.deepcopy(update["$set"]))


def saved_data(root):
    root = Path(root)
    shutil.copytree(ROOT / "demo_app/profiles", root / "demo_app/profiles")
    (root / "enrichment").mkdir()
    shutil.copyfile(ROOT / "enrichment/taxonomy.json", root / "enrichment/taxonomy.json")
    artifact = root / "artifacts/enrichment-v1"
    artifact.mkdir(parents=True)
    policies = {"baseline": {"version": "semantic-policy-v1-baseline", "instruction_addendum": "", "review_behavior": "report_only"},
                "candidate": {"version": "semantic-policy-v1-candidate", "instruction_addendum": "Check conflicts.", "review_behavior": "abstain_invalid_evidence"}}
    report = {"experiment_id": EXPERIMENT_ID, "status": "completed", "atlas_persistence_verified": True,
              "raw_unchanged": True, "freeze_sha256": "frozen", "policies": policies,
              "decision": {"accepted": True, "reason": "validation_improved_without_more_unsupported_assertions"},
              "sources": {}, "runs": {}, "usage": {"model": "gpt-4.1-mini-2025-04-14"}}
    all_raw, all_normalized, all_semantic, run_docs = [], [], [], []
    for split, count in (("development", 24), ("validation", 12)):
        public, raw = [], []
        for index in range(count):
            product = copy.deepcopy(PRODUCT)
            identity = "demo-{}-{:03d}".format(split, index)
            product["source_record_id"] = identity
            product["normalized_product"]["product_id"] = "P-" + identity
            public.append(product)
            raw.append({"dataset_id": "semantic-v1-" + split, "supplier_id": "northstar", "source_record_id": identity,
                        "payload": {"sku": "P-" + identity, "title": product["fields"]["name"], "weight": 250,
                                    "weight_unit": "g", "description": product["fields"]["description"],
                                    "supplier_notes": product["fields"]["supplier_notes"]}})
            all_normalized.append({"run_id": "norm-" + split, "source_record_id": identity, "dataset_id": "semantic-v1-" + split,
                                   "product": product["normalized_product"], "status": "normalized"})
        all_raw.extend(raw)
        source = {"dataset_id": "semantic-v1-" + split, "normalization_run_id": "norm-" + split, "raw_digest": _raw_digest(raw)}
        report["sources"][split] = source
        source_directory = artifact / "normalizer-sources" / split
        source_directory.mkdir(parents=True)
        (source_directory / "records.jsonl").write_text("".join(stable_json(row) + "\n" for row in raw))
        for variant in ("baseline", "candidate"):
            stage = variant + "_" + split
            run = {"run_id": stage, "split": split, "overall": {"total_expected": count}, "policy_version": policies[variant]["version"]}
            report["runs"][stage] = run
            run_docs.append({"_id": stage, "experiment_id": EXPERIMENT_ID, "split": split, "variant": variant,
                             "status": "evaluated", "freeze_sha256": "frozen", "policy_id": fingerprint(policies[variant]),
                             "evaluation": copy.deepcopy(run), "source": source})
            folder = artifact / stage
            folder.mkdir()
            (folder / "evaluation.json").write_text(json.dumps(run))
            for start in range(0, count, 4):
                batch_products = public[start:start + 4]
                outputs = [dict(copy.deepcopy(OUTPUT), source_record_id=product["source_record_id"]) for product in batch_products]
                checks = {row["source_record_id"]: [] for row in outputs}
                details = {"model": report["usage"]["model"], "policy_version": run["policy_version"],
                           "input": {"products": batch_products}, "model_outputs": outputs}
                (folder / ("batch-{:02d}.json".format(start // 4 + 1))).write_text(json.dumps({"outputs": outputs, "details": details, "evidence_checks": checks}))
                all_semantic.extend({"run_id": stage, "source_record_id": row["source_record_id"], "output": row,
                                     "original_model_output": copy.deepcopy(row), "evidence_checks": [],
                                     "output_index": start + index, "model": report["usage"]["model"]}
                                    for index, row in enumerate(outputs))
    (artifact / "report.json").write_text(json.dumps(report))
    normalizer = MagicMock()
    normalizer.raw = Collection(all_raw)
    placements = Collection()
    normalizer.raw.database = {PLACEMENTS_COLLECTION: placements}
    normalizer.results = Collection(all_normalized)
    store = MagicMock()
    store.normalizer = normalizer
    store.experiments = Collection([{"_id": EXPERIMENT_ID, "report": report}])
    store.runs = Collection(run_docs)
    store.results = Collection(all_semantic)
    return report, store, placements


class DemoDataTests(unittest.TestCase):
    def test_local_snapshot_is_explicit_immutable_and_does_not_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            saved_data(directory)
            with patch("demo_app.data.connect") as connector:
                repo = DemoRepository(directory, prefer_atlas=False)
                first = repo.state()
                first["products"][0]["fields"]["name"] = "mutated"
                second = repo.state()
                connector.assert_not_called()
            self.assertEqual(second["source"]["kind"], "local_fallback")
            self.assertEqual(len(second["products"]), 36)
            self.assertEqual(second["products"][0]["fields"]["name"], PRODUCT["fields"]["name"])
            self.assertIn("Check conflicts.", second["policy_instructions"]["candidate"])
            self.assertIsNone(second["final_evaluation"])
            with self.assertRaisesRegex(ValueError, "local saved"):
                repo.accept(repo.preview("homeware")["preview_id"])

    def test_atlas_snapshot_checks_provenance_and_cache_avoids_refresh_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            _, store, _ = saved_data(directory)
            client, connector = MagicMock(), MagicMock()
            connector.return_value = (client, store.normalizer)
            with patch("demo_app.data.EnrichmentStore", return_value=store):
                repo = DemoRepository(directory, connector=connector)
            self.assertEqual(repo.state()["source"]["kind"], "atlas")
            self.assertEqual(len(repo.state()["products"]), 36)
            connector.assert_called_once()
            repo.close()
            client.close.assert_called_once()

    def test_wrong_atlas_run_cannot_mix_into_local_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            _, store, _ = saved_data(directory)
            store.runs.rows[0]["experiment_id"] = "other-experiment"
            client = MagicMock()
            with patch("demo_app.data.EnrichmentStore", return_value=store):
                repo = DemoRepository(directory, connector=lambda: (client, store.normalizer))
            self.assertEqual(repo.state()["source"]["kind"], "local_fallback")
            self.assertIn("Atlas unavailable", repo.state()["source"]["label"])
            client.close.assert_called_once()

    def test_mutated_local_raw_is_rejected_and_missing_data_error_is_useful(self):
        with tempfile.TemporaryDirectory() as directory:
            saved_data(directory)
            path = Path(directory) / "artifacts/enrichment-v1/normalizer-sources/development/records.jsonl"
            text = path.read_text().replace("Office mug", "altered-credential-marker", 1)
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, "restore artifacts") as raised:
                DemoRepository(directory, prefer_atlas=False)
            self.assertNotIn("credential-marker", str(raised.exception))

    def test_preview_changes_store_paths_and_preserves_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            saved_data(directory)
            repo = DemoRepository(directory, prefer_atlas=False)
            before = repo.state()["products"]
            first, second = repo.preview("homeware"), repo.preview("gift")
            self.assertEqual(first["counts"]["ready"], 36)
            self.assertNotEqual(first["records"][0]["store_category"], second["records"][0]["store_category"])
            self.assertEqual(repo.state()["products"], before)
            product = repo.products[0]
            product["candidate"]["output"]["category"] = "unknown"
            third = repo.preview("homeware", [product["source_record_id"]])
            self.assertEqual(third["records"][0]["status"], "needs_review")
            self.assertIsNone(third["records"][0]["store_category"])
            with self.assertRaises(ValueError):
                repo.preview("../../.env")

    def test_atlas_accept_writes_only_demo_collection_with_source_links_and_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            report, store, placements = saved_data(directory)
            with patch("demo_app.data.EnrichmentStore", return_value=store):
                repo = DemoRepository(directory, connector=lambda: (MagicMock(), store.normalizer))
            preview = repo.preview("gift")
            chosen = [preview["records"][0]["source_record_id"]]
            accepted = repo.accept(preview["preview_id"], chosen)
            self.assertEqual(accepted["accepted_count"], 1)
            self.assertEqual(len(placements.rows), 1)
            row = placements.rows[0]
            self.assertEqual(row["semantic_run_id"], "candidate_development")
            self.assertEqual(row["profile_hash"], preview["profile_hash"])
            self.assertEqual(row["normalization_run_id"], "norm-development")
            self.assertEqual(row["source_raw_digest"], report["sources"]["development"]["raw_digest"])
            self.assertEqual(store.normalizer.raw.writes, [])
            self.assertEqual(store.normalizer.results.writes, [])
            self.assertEqual(store.results.writes, [])
            repo.accept(preview["preview_id"], chosen)
            self.assertEqual(len(placements.rows), 1)

    def test_changed_profile_policy_and_review_rows_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            _, store, placements = saved_data(directory)
            with patch("demo_app.data.EnrichmentStore", return_value=store):
                repo = DemoRepository(directory, connector=lambda: (MagicMock(), store.normalizer))
            identity = repo.products[0]["source_record_id"]
            preview = repo.preview("homeware", [identity])
            path = Path(directory) / "demo_app/profiles/homeware.json"
            profile = json.loads(path.read_text())
            profile["description"] += " Updated."
            path.write_text(json.dumps(profile))
            with self.assertRaisesRegex(ValueError, "profile changed"):
                repo.accept(preview["preview_id"])
            preview = repo.preview("homeware", [identity])
            store.experiments.rows[0]["report"]["policies"]["candidate"]["instruction_addendum"] += " New policy."
            with self.assertRaisesRegex(ValueError, "policy changed"):
                repo.accept(preview["preview_id"])
            self.assertEqual(placements.writes, [])

    def test_unmapped_and_bad_citation_rows_require_review_without_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            saved_data(directory)
            repo = DemoRepository(directory, prefer_atlas=False)
            identity = repo.products[0]["source_record_id"]
            path = Path(directory) / "demo_app/profiles/homeware.json"
            profile = json.loads(path.read_text())
            profile["rules"] = [rule for rule in profile["rules"] if rule["category"] != "drinking_cup"]
            path.write_text(json.dumps(profile))
            result = repo.preview("homeware", [identity])
            self.assertEqual(result["records"][0]["status"], "unmapped")
            repo.products[0]["candidate"]["output"]["evidence"][0]["excerpt"] = "invented quote"
            result = repo.preview("gift", [identity])
            self.assertEqual(result["records"][0]["status"], "needs_review")
            self.assertIsNone(result["records"][0]["store_category"])

    def test_final_state_exposes_aggregate_counts_without_final_inputs_or_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            saved_data(directory)
            docs = Path(directory) / "docs"
            docs.mkdir()
            (docs / "semantic-v1-final-results.json").write_text(json.dumps({"report": {
                "status": "completed", "final_holdout_evaluated": True,
                "products": [{"secret": "private-final-input"}], "answer_keys": "private-final-answer",
                "runs": {"candidate_final": {"overall": {"correct_field_decisions": 70},
                          "representative_failures": [{"secret": "private-final-failure"}]}}}}))
            repo = DemoRepository(directory, prefer_atlas=False)
            final = repo.state()["final_evaluation"]
            self.assertEqual(final["runs"]["candidate_final"]["overall"]["correct_field_decisions"], 70)
            self.assertNotIn("private-final", json.dumps(final))


if __name__ == "__main__":
    unittest.main()
