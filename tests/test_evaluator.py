import copy
import unittest

from evaluation.evaluator import candidate_is_acceptable, evaluate


def label(source, supplier="a", ambiguous=False):
    value = {"dataset_id": "test", "source_record_id": source, "supplier_id": supplier,
             "expected_status": "quarantined" if ambiguous else "normalized"}
    if not ambiguous:
        value["product"] = {"product_id": source, "name": "Product", "weight_g": 1000.0}
    return value


def output(expected, quarantined=False):
    value = {k: expected[k] for k in ("dataset_id", "supplier_id", "source_record_id")}
    value["status"] = "quarantined" if quarantined else "normalized"
    if not quarantined:
        value["product"] = copy.deepcopy(expected.get("product", {"product_id": "bad", "name": "Bad", "weight_g": 1}))
    return value


class EvaluationTests(unittest.TestCase):
    def test_full_accounting_with_missing_duplicates_and_extras(self):
        labels = [label(str(i)) for i in range(1, 6)] + [label("6", "b", True), label("7", "b", True)]
        wrong = output(labels[1])
        wrong["product"]["weight_g"] = 9
        outputs = [output(labels[0]), wrong, output(labels[2], True),
                   output(labels[4]), output(labels[4]), output(labels[5], True),
                   output(labels[6]), output(label("extra"))]
        result = evaluate(labels, outputs)
        counts = result["overall"]
        for key, expected in {"total_expected": 7, "recoverable": 5, "ambiguous": 2,
                              "correct_normalization": 1, "incorrect_outputs": 3,
                              "correct_quarantine": 1, "unnecessarily_unresolved": 1,
                              "missing_outputs": 1, "duplicate_outputs": 1, "unexpected_outputs": 1}.items():
            self.assertEqual(counts[key], expected, key)
        self.assertEqual(counts["rates"]["normalization"], {"count": 1, "denominator": 5, "rate": .2})
        self.assertEqual(result["by_supplier"]["b"]["ambiguous"], 2)
        self.assertFalse(result["acceptance_passed"])
        categories = ("correct_normalization", "incorrect_outputs", "correct_quarantine",
                      "unnecessarily_unresolved", "missing_outputs")
        self.assertEqual(sum(counts[k] for k in categories), counts["total_expected"])

    def test_tolerances_and_invalid_weight_types(self):
        expected = label("1")
        actual = output(expected)
        actual["product"]["weight_g"] += 0.0000005
        self.assertTrue(evaluate([expected], [actual])["acceptance_passed"])
        for value in (1001, float("nan"), True, "1000"):
            actual["product"]["weight_g"] = value
            self.assertEqual(evaluate([expected], [actual])["overall"]["incorrect_outputs"], 1)

    def test_duplicate_labels_and_empty_evaluation_rejected(self):
        with self.assertRaises(ValueError):
            evaluate([label("1"), label("1")], [])
        self.assertFalse(evaluate([], [])["acceptance_passed"])

    def test_final_cannot_select_policy_or_expose_failure_evidence(self):
        final = evaluate([label("1")], [], split="final")
        self.assertEqual(final["development_failures"], [])
        with self.assertRaises(ValueError):
            candidate_is_acceptable(final, final)
        dev = evaluate([label("1")], [output(label("1"))])
        self.assertFalse(candidate_is_acceptable(dev, dev))
        other = evaluate([label("2")], [output(label("2"))])
        self.assertFalse(candidate_is_acceptable(dev, other))

    def test_development_failure_evidence_has_no_expected_answers(self):
        failure = evaluate([label("1")], [output(label("1"), True)])["development_failures"][0]
        self.assertEqual(set(failure), {"source_record_id", "supplier_id", "category"})

    def test_candidate_can_remove_unexpected_supplier_outputs(self):
        labels = [label("1"), label("2")]
        baseline = evaluate(labels, [output(labels[0]), output(labels[1], True),
                                     output(label("extra", "unexpected_supplier"))])
        candidate = evaluate(labels, [output(row) for row in labels])
        self.assertTrue(candidate_is_acceptable(baseline, candidate))
