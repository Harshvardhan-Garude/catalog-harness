import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from demo_app.benchmark_view import OUTPUT_COLLECTION, REPORT_COLLECTION, catalog_page, saved_report


DATASET = "benchmark-v1-seed4102026-n50000"
REPORT = {"benchmark_id": "benchmark-v1", "label": "50k synthetic database benchmark", "status": "completed",
          "model_calls_added": 0, "datasets": [{"dataset_id": DATASET, "requested_records": 50000,
          "raw_count": 50000, "output_count": 50000, "processing": {"records_per_second": 1234.5}}]}


class DemoBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.reports, self.outputs = Mock(), Mock()
        self.database = {REPORT_COLLECTION: self.reports, OUTPUT_COLLECTION: self.outputs}
        self.repository = SimpleNamespace(root=self.root, store=SimpleNamespace(normalizer=SimpleNamespace(
            raw=SimpleNamespace(database=self.database))))
        self.reports.find_one.return_value = copy.deepcopy(REPORT)

    def tearDown(self):
        self.temporary.cleanup()

    def test_report_read_is_small_and_never_reads_catalog_or_runs_processing(self):
        self.reports.find_one.return_value["_id"] = "private-database-id"
        self.reports.find_one.return_value["unrelated"] = "excluded"
        response = saved_report(self.repository)
        self.assertEqual(response["source"]["kind"], "atlas")
        self.assertTrue(response["browser_available"])
        self.assertNotIn("_id", response["report"])
        self.assertNotIn("unrelated", response["report"])
        self.reports.find_one.assert_called_once_with({"_id": "benchmark-v1"}, {"_id": 0}, max_time_ms=3000)
        self.outputs.assert_not_called()
        self.outputs.find.assert_not_called()

    def test_fallback_is_explicit_and_disables_live_browser(self):
        self.reports.find_one.side_effect = RuntimeError("SECRET_ATLAS_DRIVER_TEXT")
        (self.root / "docs").mkdir()
        (self.root / "docs/benchmark-results.json").write_text(json.dumps(REPORT))
        response = saved_report(self.repository)
        self.assertEqual(response["source"]["kind"], "local_fallback")
        self.assertFalse(response["browser_available"])
        self.assertNotIn("SECRET", json.dumps(response))
        (self.root / "docs/benchmark-results.json").unlink()
        self.assertIsNone(saved_report(self.repository)["report"])

    def test_invalid_filters_are_rejected_before_database_access(self):
        for changes in ({"dataset_id": "semantic-v1-final"}, {"supplier": "$where"}, {"status": "other"},
                        {"page": "0"}, {"page": "2001"}, {"page": "1.0"}, {"page": "+2"}):
            arguments = {"dataset_id": DATASET, **changes}
            with self.assertRaises(ValueError):
                catalog_page(self.repository, **arguments)
        self.reports.find_one.assert_not_called()
        self.outputs.find.assert_not_called()

    def test_live_pagination_is_filtered_bounded_and_excludes_raw_payloads(self):
        records = [{"source_record_id": "item-{:04d}".format(i), "status": "quarantined"} for i in range(25)]
        cursor = Mock()
        for method in ("sort", "skip", "limit", "batch_size", "max_time_ms"):
            getattr(cursor, method).return_value = cursor
        cursor.__iter__ = Mock(return_value=iter(records))
        self.outputs.find.return_value = cursor
        self.outputs.count_documents.return_value = 80
        result = catalog_page(self.repository, dataset_id=DATASET, supplier="cedar", status="quarantined", page="2")
        self.assertEqual(len(result["records"]), 25)
        self.assertEqual(result["page_size"], 25)
        self.assertTrue(result["has_next"])
        self.outputs.count_documents.assert_called_once_with({"dataset_id": DATASET, "supplier_id": "cedar", "status": "quarantined"}, maxTimeMS=3000)
        cursor.skip.assert_called_once_with(25)
        cursor.limit.assert_called_once_with(25)
        cursor.batch_size.assert_called_once_with(25)
        self.assertNotIn("payload", self.outputs.find.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
