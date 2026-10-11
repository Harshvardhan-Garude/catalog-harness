import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from catalog_harness.policy import HarnessPolicy
from catalog_harness.storage import EventLog, Store, batches
from catalog_harness.transforms import transform_record
from evaluation.evaluator import evaluate
from evaluation.generate import SUPPLIERS, generate, generate_batch


PLAN = json.loads((Path(__file__).resolve().parents[1] / "fixtures/plan.json").read_text())


class MemoryCursor(list):
    def sort(self, fields):
        for field, direction in reversed(fields):
            super().sort(key=lambda row: row[field], reverse=direction < 0)
        return self

    def batch_size(self, size):
        return self


class MemoryCollection:
    """Small insert-only fake exercising seed integrity without a live service."""

    def __init__(self):
        self.rows = []

    def find(self, query, projection=None):
        excluded = {key for key, value in (projection or {}).items() if not value}
        return MemoryCursor(copy.deepcopy({key: value for key, value in row.items() if key not in excluded})
                            for row in self.rows if all(row.get(key) == value for key, value in query.items()))

    def find_one(self, query, projection=None):
        return next(iter(self.find(query, projection)), None)

    def update_one(self, query, update, upsert=False):
        row = next((row for row in self.rows if all(row.get(key) == value for key, value in query.items())), None)
        if row is None:
            if not upsert:
                return
            row = {**copy.deepcopy(query), **copy.deepcopy(update.get("$setOnInsert", {}))}
            self.rows.append(row)
        row.update(copy.deepcopy(update.get("$set", {})))

    def bulk_write(self, operations, ordered=False):
        for operation in operations:
            self.update_one(operation._filter, operation._doc, operation._upsert)


class DatasetAndStorageTests(unittest.TestCase):
    def test_reproducibility_separation_and_fixture(self):
        batch = generate_batch("development", 1101, 300)
        self.assertEqual(batch, generate_batch("development", 1101, 300))
        final = generate_batch("final", 2202, 300)
        ids = {r["source_record_id"] for r in batch[1]}
        self.assertFalse(ids & {r["source_record_id"] for r in final[1]})
        metadata = {m["supplier_id"]: m for m in SUPPLIERS}
        snapshot = copy.deepcopy(batch[1])
        outputs = [transform_record(r, PLAN, metadata[r["supplier_id"]]) for r in batch[1]]
        report = evaluate(batch[2], outputs)
        self.assertTrue(report["acceptance_passed"])
        self.assertEqual(snapshot, batch[1])
        self.assertEqual({sid: sum(r["supplier_id"] == sid for r in batch[1]) for sid in metadata},
                         {"northstar": 195, "cedar": 75, "harbor": 30})
        for row in batch[1]:
            self.assertEqual(set(row), {"dataset_id", "supplier_id", "source_record_id", "payload"})

    def test_generated_labels_are_separate_and_regeneration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = generate(root, 60)
            snapshot = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(manifest, generate(root, 60))
            self.assertEqual(snapshot, {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            self.assertFalse(any('label' in p.name for p in (root / 'agent_visible').rglob('*')))
            with self.assertRaises(ValueError):
                generate(root, 60, 1101, 1101)

    def test_noninteger_sizes_are_rejected(self):
        for size in (True, 30.5, "30"):
            with self.assertRaises(ValueError):
                generate_batch("development", 1101, size)

    def test_seeding_is_repeatable_and_corruption_blocks_execution(self):
        collections = {}
        db = MagicMock()
        db.__getitem__.side_effect = lambda name: collections.setdefault(name, MemoryCollection())
        store = Store(db)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = generate(root, 30)
            dataset_id = manifest[0]["dataset_id"]
            public = root / "agent_visible" / dataset_id
            first = store.seed(public)
            raw_snapshot = copy.deepcopy(store.raw.rows)
            self.assertEqual(store.seed(public), first)
            self.assertEqual(store.raw.rows, raw_snapshot)
            self.assertEqual(len(store.raw.rows), 30)
            self.assertEqual(len(store.suppliers.rows), 3)
            self.assertEqual(store.dataset(dataset_id)["seed_state"], "complete")
            store.raw.rows[0]["payload"]["unexpected"] = True
            with self.assertRaises(ValueError):
                store.seed(public)
            with self.assertRaises(ValueError):
                store.dataset(dataset_id)
            self.assertTrue(store.raw.rows[0]["payload"]["unexpected"])

    def test_invalid_supplier_metadata_is_rejected_before_database_writes(self):
        for mutation in ("duplicate", "dataset_override", "private_answers"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dataset_id = generate(root, 30)[0]["dataset_id"]
                public = root / "agent_visible" / dataset_id
                supplier_path = public / "suppliers.json"
                suppliers = json.loads(supplier_path.read_text())
                if mutation == "duplicate":
                    suppliers.append(suppliers[0])
                elif mutation == "dataset_override":
                    suppliers[0]["dataset_id"] = "another_dataset"
                else:
                    suppliers[0]["expected_answers"] = "private evaluator content"
                supplier_path.write_text(json.dumps(suppliers))
                store = Store(MagicMock())
                with self.assertRaises(ValueError):
                    store.seed(public)
                store.datasets.update_one.assert_not_called()
                store.raw.bulk_write.assert_not_called()

    def test_batch_limits_and_precise_result_identity(self):
        self.assertEqual([len(b) for b in batches(range(205))], [100, 100, 5])
        for size in (0, 101, True):
            with self.assertRaises(ValueError):
                list(batches(range(2), size))
        db = MagicMock()
        store = Store(db)
        result = {"dataset_id": "d", "supplier_id": "s", "source_record_id": "r", "status": "quarantined"}
        store.save_results("run", [result])
        operation = store.results.bulk_write.call_args[0][0][0]
        self.assertEqual(operation._filter, {"run_id": "run", "dataset_id": "d", "supplier_id": "s", "source_record_id": "r"})
        self.assertIn("$setOnInsert", operation._doc)
        self.assertTrue(operation._upsert)

    def test_exploration_is_bounded_and_final_is_blocked(self):
        store = Store(MagicMock())
        store.datasets.find_one.return_value = {"split": "development", "seed_state": "complete"}
        store.sample("d", "s", 5)
        store.raw.find.return_value.sort.return_value.limit.assert_called_with(5)
        with self.assertRaises(ValueError):
            store.sample("d", "s", 51)
        store.datasets.find_one.return_value = {"split": "final", "seed_state": "complete"}
        with self.assertRaises(ValueError):
            store.sample("d", "s")

    def test_policy_cannot_remove_safety_checks_and_events_reject_secret_fields(self):
        with self.assertRaises(ValueError):
            HarnessPolicy(verification_steps=("readback",))
        with self.assertRaises(ValueError):
            HarnessPolicy(context_fields=("final_labels",))
        with tempfile.TemporaryDirectory() as directory:
            events = EventLog(directory)
            events.emit("started", "run", "dataset", plan_sha256="digest")
            with self.assertRaises(ValueError):
                events.emit("started", "run", "dataset", uri="secret")
