"""Offline contracts for the synthetic database benchmark; no service calls."""

import copy
import hashlib
import itertools
import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from catalog_harness import benchmark
from catalog_harness.storage import fingerprint, stable_json


class MemoryCursor(list):
    def __init__(self, rows, collection):
        super().__init__(copy.deepcopy(list(rows)))
        self.collection = collection

    def sort(self, fields, direction=None):
        fields = [(fields, direction)] if isinstance(fields, str) else fields
        for name, order in reversed(fields):
            super().sort(key=lambda row: row.get(name, ""), reverse=order < 0)
        return self

    def batch_size(self, size):
        self.collection.cursor_batches.append(size)
        return self

    def max_time_ms(self, value):
        return self

    def limit(self, size):
        del self[size:]
        return self


class MemoryCollection:
    """Insert-only fake records actual bulk boundaries and repeat-upsert counts."""

    def __init__(self, name):
        self.name = name
        self.rows = {}
        self.bulk_sizes = []
        self.cursor_batches = []
        self.indexes = []

    def create_index(self, fields, **kwargs):
        self.indexes.append((fields, kwargs))
        return kwargs.get("name", "index")

    def find(self, query, projection=None):
        excluded = {key for key, value in (projection or {}).items() if not value}
        return MemoryCursor(({key: value for key, value in row.items() if key not in excluded}
                             for row in self.rows.values()
                             if all(row.get(key) == value for key, value in query.items())), self)

    def find_one(self, query, projection=None, **kwargs):
        return next(iter(self.find(query, projection)), None)

    def count_documents(self, query, **kwargs):
        return len(self.find(query))

    def bulk_write(self, operations, ordered=False):
        operations = list(operations)
        self.bulk_sizes.append(len(operations))
        inserted = matched = 0
        for operation in operations:
            query, update = operation._filter, operation._doc
            identity = stable_json(query)
            if identity not in self.rows:
                inserted += 1
                self.rows[identity] = copy.deepcopy({**query, **update.get("$setOnInsert", {})})
            else:
                matched += 1
            self.rows[identity].update(copy.deepcopy(update.get("$set", {})))
        return SimpleNamespace(upserted_count=inserted, matched_count=matched, modified_count=0)


class MemoryDatabase(dict):
    def __missing__(self, name):
        value = self[name] = MemoryCollection(name)
        return value


class QueryCursor:
    def __init__(self, collection, query, projection):
        self.collection = collection
        self.call = {"filter": copy.deepcopy(query), "projection": projection}
        collection.calls.append(self.call)

    def sort(self, value):
        self.call["sort"] = value
        return self

    def hint(self, value):
        self.call["hint"] = value
        return self

    def limit(self, value):
        self.call["limit"] = value
        return self

    def skip(self, value):
        self.call["skip"] = value
        return self

    def max_time_ms(self, value):
        self.call["max_time_ms"] = value
        return self

    def batch_size(self, value):
        self.call["batch_size"] = value
        return self

    def __iter__(self):
        self.call["consumed"] = True
        return iter([{"source_record_id": "record-1"}])

    def explain(self):
        self.call["explain"] = True
        return {"executionStats": {"nReturned": 1, "totalDocsExamined": 1000,
                                   "totalKeysExamined": 0}}


class QueryCollection:
    def __init__(self):
        self.calls = []
        self.name = "catalog_harness_benchmark_outputs"
        self.database = self
        self.commands = []

    def find(self, query, projection=None):
        return QueryCursor(self, query, projection)

    def command(self, value):
        self.commands.append(copy.deepcopy(value))
        return {"executionStats": {"nReturned": 1, "totalDocsExamined": 1000,
                                   "totalKeysExamined": 0}}


class BenchmarkTests(unittest.TestCase):
    def test_first_record_does_not_materialize_the_fifty_thousand_record_dataset(self):
        tracemalloc.start()
        try:
            rows = benchmark.generate_records(50000)
            self.assertEqual(next(rows)["dataset_id"], benchmark.dataset_id(50000))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)

    def test_generator_streams_reproducibly_at_all_three_sizes_with_distinct_datasets(self):
        namespaces = set()
        for size in (1000, 10000, 50000):
            with self.subTest(size=size):
                rows = benchmark.generate_records(size)
                self.assertIs(iter(rows), rows)
                digest = hashlib.sha256()
                identities = set()
                for row in rows:
                    self.assertEqual(set(row), {"dataset_id", "supplier_id", "source_record_id", "payload"})
                    self.assertEqual(row["dataset_id"], benchmark.dataset_id(size))
                    identities.add((row["supplier_id"], row["source_record_id"]))
                    digest.update(stable_json(row).encode())
                self.assertEqual(len(identities), size)
                second = hashlib.sha256()
                for row in benchmark.generate_records(size):
                    second.update(stable_json(row).encode())
                self.assertEqual(digest.digest(), second.digest())
                namespaces.add(benchmark.dataset_id(size))
        self.assertEqual(len(namespaces), 3)
        self.assertNotEqual(list(itertools.islice(benchmark.generate_records(1000), 10)),
                            list(itertools.islice(benchmark.generate_records(1000, seed=123), 10)))

    def test_seed_is_idempotent_and_processing_preserves_raw_with_bounded_bulk_writes(self):
        database = MemoryDatabase()
        store = benchmark.BenchmarkStore(database, batch_size=100)
        store.ensure_indexes()
        first = store.seed(1000)
        self.assertEqual(first["inserted"], 1000)
        before = store.raw_digest(benchmark.dataset_id(1000))
        self.assertEqual(before["count"], 1000)
        second = store.seed(1000)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["matched"], 1000)
        self.assertEqual(store.raw_digest(benchmark.dataset_id(1000)), before)
        result = store.process(benchmark.dataset_id(1000), benchmark.load_plan())
        self.assertEqual(result["normalized"] + result["quarantined"], 1000)
        self.assertGreater(result["normalized"], 0)
        self.assertGreater(result["quarantined"], 0)
        self.assertGreater(result["records_per_second"], 0)
        self.assertEqual(store.raw_digest(benchmark.dataset_id(1000)), before)
        self.assertEqual(store.outputs.count_documents({"dataset_id": benchmark.dataset_id(1000)}), 1000)
        self.assertTrue(store.reconcile(benchmark.dataset_id(1000), benchmark.load_plan()))
        for collection in database.values():
            self.assertTrue(all(0 < size <= 100 for size in collection.bulk_sizes))
            self.assertTrue(all(0 < size <= 100 for size in collection.cursor_batches))

    def test_invalid_sizes_batches_and_insufficient_query_samples_fail_before_work(self):
        for size in (0, -1, 50001, True, 1.5, "1000"):
            with self.subTest(size=size), self.assertRaises(ValueError):
                next(benchmark.generate_records(size))
        for batch_size in (0, 101, True):
            with self.subTest(batch_size=batch_size), self.assertRaises(ValueError):
                benchmark.BenchmarkStore(MemoryDatabase(), batch_size=batch_size)
        collection = QueryCollection()
        with self.assertRaises(ValueError):
            benchmark.measure_query(collection, {}, [("source_record_id", 1)], benchmark.INDEX_NAME, samples=19)
        self.assertEqual(collection.calls, [])

    def test_percentiles_use_twenty_client_samples(self):
        summary = benchmark.latency_summary(list(range(1, 21)))
        self.assertEqual(summary["samples"], 20)
        self.assertEqual(summary["median_ms"], 10.5)
        self.assertEqual(summary["p95_ms"], 19)

    def test_query_plans_use_same_filter_sort_and_page_and_separate_explain(self):
        query = {"dataset_id": benchmark.dataset_id(1000), "supplier_id": "northstar",
                 "status": "quarantined"}
        sort = [("source_record_id", 1)]
        collections = []
        for hint in ("benchmark_catalog_queue", {"$natural": 1}):
            collection = QueryCollection()
            summary = benchmark.measure_query(collection, query, sort, hint, warmup=2, samples=20)
            self.assertEqual(summary["samples"], 20)
            self.assertEqual(summary["execution_stats"],
                             {"n_returned": 1, "docs_examined": 1000, "keys_examined": 0})
            measured = [call for call in collection.calls if call.get("consumed")]
            self.assertEqual(len(measured), 22)
            self.assertEqual(len(collection.commands), 1)
            explained = collection.commands[0]
            self.assertEqual(explained["verbosity"], "executionStats")
            self.assertEqual(explained["explain"]["filter"], query)
            self.assertEqual(explained["explain"]["hint"], hint)
            self.assertEqual(explained["explain"]["sort"], dict(sort))
            self.assertEqual(explained["explain"]["limit"], 25)
            for call in collection.calls:
                self.assertEqual(call["filter"], query)
                self.assertEqual(call["sort"], sort)
                self.assertEqual(call["limit"], 25)
                self.assertEqual(call["hint"], hint)
            collections.append(collection)
        self.assertEqual(len(collections[0].calls), len(collections[1].calls))

    def test_failed_repeat_seed_resumes_without_replacing_initial_measurement(self):
        initial = {"seconds": 1.0, "inserted": 1000, "matched": 0, "existing_before": 0,
                   "raw_count": 1000, "records_per_second": 1000, "measurement": "initial_insertion"}
        repeat = {"seconds": .5, "inserted": 0, "matched": 1000, "existing_before": 1000,
                  "raw_count": 1000, "records_per_second": 2000, "measurement": "upsert_with_existing_records"}
        store = MagicMock()
        store.seed.side_effect = [initial, RuntimeError("private driver detail")]
        database = MemoryDatabase()
        with tempfile.TemporaryDirectory() as directory, patch.object(benchmark, "BenchmarkStore", return_value=store):
            with self.assertRaises(RuntimeError):
                benchmark.run_benchmark(database, directory=directory, sizes=(1000,))
            saved = json.loads((Path(directory) / "report.json").read_text())
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["datasets"][0]["seed_initial"], initial)
            self.assertNotIn("private driver detail", json.dumps(saved))
            store.process.assert_not_called()
            store.reset_mock()
            store.seed.side_effect = None
            store.seed.return_value = repeat
            store.raw_digest.return_value = benchmark.stream_digest(benchmark.generate_records(1000))
            store.process.return_value = {"seconds": 1, "records_per_second": 1000,
                                          "normalized": 800, "quarantined": 200}
            store.outputs.count_documents.return_value = 1000
            store.reconcile.return_value = True
            store.aggregation.return_value = [{"supplier_id": "northstar", "status": "normalized", "records": 800},
                                               {"supplier_id": "northstar", "status": "quarantined", "records": 200}]
            with patch.object(benchmark, "measure_query", return_value={"page_sha256": "same-page"}):
                report = benchmark.run_benchmark(database, directory=directory, sizes=(1000,))
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["datasets"][0]["seed_initial"], initial)
            store.seed.assert_called_once_with(1000)
            self.assertTrue(all(report["datasets"][0]["integrity"].values()))
            store.reset_mock()
            self.assertEqual(benchmark.run_benchmark(database, directory=directory, sizes=(1000,)), report)
            self.assertEqual(store.mock_calls, [])

    def completed_report(self):
        """Distinguishable saved timings ensure restoration cannot remeasure them."""
        return {"benchmark_id": benchmark.BENCHMARK_ID, "version": 1, "seed": benchmark.SEED,
                "sizes": list(benchmark.SIZES), "status": "completed",
                "plan_sha256": fingerprint(benchmark.load_plan()), "model_calls_added": 0,
                "datasets": [{"dataset_id": benchmark.dataset_id(size), "requested_records": size,
                              "status": "completed", "seed_initial": {"seconds": size / 17.0,
                                  "inserted": size, "matched": 0, "measurement": "initial_insertion"},
                              "seed_repeat": {"seconds": size / 29.0, "inserted": 0, "matched": size}}
                             for size in benchmark.SIZES]}

    def test_fresh_artifact_directory_restores_completed_atlas_report_without_writes_or_rerun(self):
        saved = self.completed_report()
        database = MagicMock()
        collection = database.__getitem__.return_value
        collection.find_one.return_value = copy.deepcopy(saved)
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as fresh, \
                patch.object(benchmark, "BenchmarkStore") as factory:
            (Path(first) / "report.json").write_text(json.dumps(saved))
            self.assertEqual(benchmark.run_benchmark(database, directory=first), saved)
            database.__getitem__.assert_not_called()
            self.assertFalse((Path(fresh) / "report.json").exists())
            restored = benchmark.run_benchmark(database, directory=fresh)
            self.assertEqual(restored, saved)
            self.assertEqual(json.loads((Path(fresh) / "report.json").read_text()), saved)
            database.__getitem__.assert_called_once_with("catalog_harness_benchmark_reports")
            collection.find_one.assert_called_once()
            self.assertEqual([call[0] for call in collection.mock_calls], ["find_one"])
            factory.assert_not_called()

    def test_incompatible_atlas_report_rejects_cold_start_without_local_or_database_writes(self):
        valid = self.completed_report()
        for field, value in (("benchmark_id", "other-benchmark"), ("seed", benchmark.SEED + 1),
                             ("sizes", [1000]), ("plan_sha256", "different-plan")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as fresh, \
                    patch.object(benchmark, "BenchmarkStore") as factory:
                saved = copy.deepcopy(valid)
                saved[field] = value
                database = MagicMock()
                collection = database.__getitem__.return_value
                collection.find_one.return_value = saved
                with self.assertRaises(ValueError):
                    benchmark.run_benchmark(database, directory=fresh)
                self.assertFalse((Path(fresh) / "report.json").exists())
                self.assertEqual([call[0] for call in collection.mock_calls], ["find_one"])
                factory.assert_not_called()

    def test_reconciliation_detects_corruption_without_overwriting_raw_or_saved_output(self):
        store = benchmark.BenchmarkStore(MemoryDatabase())
        identity, plan = benchmark.dataset_id(1000), benchmark.load_plan()
        store.seed(1000)
        before = store.raw_digest(identity)
        store.process(identity, plan)
        output = next(iter(store.outputs.rows.values()))
        output["processing_kind"] = "unexpected_modification"
        self.assertFalse(store.reconcile(identity, plan))
        store.process(identity, plan)
        self.assertEqual(output["processing_kind"], "unexpected_modification")
        self.assertEqual(store.raw_digest(identity), before)


if __name__ == "__main__":
    unittest.main()
