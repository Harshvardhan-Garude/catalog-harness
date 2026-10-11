import copy
import json
import tempfile
import unittest
from decimal import localcontext
from pathlib import Path

from catalog_harness.__main__ import parse_plan
from catalog_harness.transforms import InvalidPlan, Quarantine, transform_record, validate_plan, weight_in_grams


PLAN = json.loads((Path(__file__).resolve().parents[1] / "fixtures/plan.json").read_text())


class TransformationTests(unittest.TestCase):
    def test_conversion_and_cleanup(self):
        for value, unit, grams in ((100, "g", 100), (" 1.25 ", " KG ", 1250),
                                    ("2 lbs", None, 907.18474), (".5 kilogram", None, 500)):
            with self.subTest(value=value):
                self.assertAlmostEqual(weight_in_grams(value, unit, {}), grams, places=6)
        raw = {"dataset_id": "test", "supplier_id": "northstar", "source_record_id": "r1",
               "payload": {"sku": "  S1 ", "title": " Test  Product\t ", "weight": "1.25", "weight_unit": "kg"}}
        before = copy.deepcopy(raw)
        actual = transform_record(raw, PLAN, {})
        self.assertEqual(actual["product"], {"product_id": "S1", "name": "Test Product", "weight_g": 1250})
        self.assertEqual(raw, before)

    def test_default_requires_documentation_and_explicit_overrides(self):
        metadata = {"default_weight_unit": "kg", "unit_documentation": "Export specification"}
        self.assertEqual(weight_in_grams(2, None, metadata), 2000)
        self.assertEqual(weight_in_grams(2, "g", metadata), 2)
        self.assertEqual(weight_in_grams("2 lb", None, metadata), 907.18474)
        with self.assertRaises(Quarantine):
            weight_in_grams(2, None, {"default_weight_unit": "kg"})
        for documentation in (True, [], "   "):
            with self.assertRaises(Quarantine):
                weight_in_grams(2, None, {"default_weight_unit": "kg",
                                         "unit_documentation": documentation})

    def test_conversion_is_independent_of_decimal_context(self):
        with localcontext() as context:
            context.prec = 2
            context.Emax = 2
            self.assertEqual(weight_in_grams("1.25", "kg", {}), 1250)

    def test_malformed_payload_quarantines_without_aborting_batch(self):
        raw = {"dataset_id": "test", "supplier_id": "northstar", "source_record_id": "r3",
               "payload": []}
        self.assertEqual(transform_record(raw, PLAN, {})["reason"], "invalid_payload")

    def test_ambiguous_missing_conflicting_and_invalid_weights_quarantine(self):
        cases = [(2, None), ("2 kg", "g"), ("1,234", "g"), ("2 oz", None),
                 (True, "g"), (float("nan"), "g"), (float("inf"), "g"), (-1, "g"), (0, "g")]
        for value, unit in cases:
            with self.subTest(value=value):
                with self.assertRaises(Quarantine):
                    weight_in_grams(value, unit, {})
        raw = {"dataset_id": "test", "supplier_id": "northstar", "source_record_id": "r2",
               "payload": {"sku": "S2", "title": "Product", "weight": 5}}
        self.assertEqual(transform_record(raw, PLAN, {})["reason"], "missing_unit_evidence")
        self.assertNotIn("product", transform_record(raw, PLAN, {}))

    def test_invalid_plans_rejected(self):
        mutations = [lambda p: p.update(python="arbitrary_code"),
                     lambda p: p.update(version=True),
                     lambda p: p["suppliers"]["northstar"].update(on_ambiguity="guess"),
                     lambda p: p["suppliers"]["northstar"]["fields"].update(weight="$where"),
                     lambda p: p["suppliers"]["northstar"].update(string_cleanup=["exec"]),
                     lambda p: p["suppliers"]["northstar"].update(default_unit="kg")]
        for mutate in mutations:
            plan = copy.deepcopy(PLAN)
            mutate(plan)
            with self.assertRaises(InvalidPlan):
                validate_plan(plan)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text('{"version":1,"version":1,"suppliers":{}}')
            with self.assertRaises(ValueError):
                parse_plan(path)
