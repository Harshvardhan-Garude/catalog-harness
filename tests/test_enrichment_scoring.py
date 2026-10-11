import copy
import json
import stat
import tempfile
import unittest
from pathlib import Path

from enrichment_eval.fixtures import (FIXTURE_VERSION, SPLIT_COUNTS, authored_split,
                                      generate, load_labels, load_public)
from enrichment_eval.scoring import (CATEGORIES, USES, candidate_decision,
                                     candidate_is_acceptable, evaluate)


def key(source="record-1", categories=("drinking_cup",), primary=("drink_beverages",),
        required=(), optional=()):
    return {"source_record_id": source, "acceptable_categories": list(categories),
            "acceptable_primary_uses": list(primary), "required_secondary_uses": list(required),
            "optional_secondary_uses": list(optional)}


def prediction(label, category=None, primary=None, secondary=None):
    return {"source_record_id": label["source_record_id"],
            "category": category if category is not None else label["acceptable_categories"][0],
            "primary_use": primary if primary is not None else label["acceptable_primary_uses"][0],
            "secondary_uses": list(label["required_secondary_uses"]) if secondary is None else secondary,
            "evidence": [], "review": []}


class SyntheticEnrichmentFixtureTests(unittest.TestCase):
    def test_split_sizes_are_frozen_distinct_and_labels_are_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "enrichment"
            self.assertEqual(generate(root), {"development": 24, "validation": 12, "final": 24})
            before = {path: path.read_bytes() for path in root.rglob("*.jsonl")}
            self.assertEqual(generate(root), SPLIT_COUNTS)
            self.assertEqual(before, {path: path.read_bytes() for path in root.rglob("*.jsonl")})
            all_ids, all_names, all_descriptions = set(), set(), set()
            for split, count in SPLIT_COUNTS.items():
                rows, labels = load_public(root, split), load_labels(root, split)
                self.assertEqual(len(rows), count)
                self.assertEqual(len(labels), count)
                self.assertEqual({row["source_record_id"] for row in rows},
                                 {label["source_record_id"] for label in labels})
                for row in rows:
                    self.assertEqual(set(row), {"source_record_id", "fields", "normalized_product"})
                    self.assertEqual(set(row["fields"]), {"name", "description", "supplier_notes"})
                    self.assertNotIn(row["source_record_id"], all_ids)
                    self.assertNotIn(row["fields"]["name"], all_names)
                    all_ids.add(row["source_record_id"])
                    all_names.add(row["fields"]["name"])
                    description = row["fields"]["description"]
                    if description:
                        self.assertNotIn(description, all_descriptions)
                        all_descriptions.add(description)
                self.assertEqual(stat.S_IMODE((root / "evaluator_only" / (split + ".jsonl")).stat().st_mode), 0o600)
            self.assertEqual(len(all_ids), 60)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((root / "evaluator_only").stat().st_mode), 0o700)

    def test_differing_file_is_never_overwritten_or_partially_regenerated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "evaluator_only").mkdir()
            differing = root / "evaluator_only" / "validation.jsonl"
            differing.write_text("preserve this existing content\n")
            with self.assertRaises(ValueError):
                generate(root)
            self.assertEqual(differing.read_text(), "preserve this existing content\n")
            self.assertFalse((root / "agent_visible").exists())

    def test_taxonomy_matches_frozen_scoring_vocabulary(self):
        path = Path(__file__).resolve().parents[1] / "enrichment" / "taxonomy.json"
        taxonomy = json.loads(path.read_text())
        self.assertEqual(taxonomy["version"], FIXTURE_VERSION)
        self.assertEqual(set(taxonomy["categories"]), CATEGORIES)
        self.assertEqual(set(taxonomy["uses"]), USES)

    def test_fresh_copies_prevent_prediction_mutation_changing_answer_keys(self):
        rows, labels = authored_split("development")
        rows[0]["fields"]["name"] = "mutated"
        labels[0]["acceptable_categories"].append("unknown")
        fresh_rows, fresh_labels = authored_split("development")
        self.assertNotEqual(rows[0], fresh_rows[0])
        self.assertNotEqual(labels[0], fresh_labels[0])
        with self.assertRaises(ValueError):
            authored_split("holdout-typo")


class SemanticScoringTests(unittest.TestCase):
    def test_independent_labels_support_multiple_categories_and_secondary_sets(self):
        label = key(categories=("drinking_cup", "measuring_cup"), primary=("measure_materials",),
                    required=("drink_beverages",), optional=("store_items",))
        for category in label["acceptable_categories"]:
            for secondary in (("drink_beverages",), ("drink_beverages", "store_items")):
                counts = evaluate([label], [prediction(label, category=category, secondary=list(secondary))])["overall"]
                self.assertEqual(counts["correct_field_decisions"], 3)
                self.assertEqual(counts["unsupported_assertions"], 0)
        missing = evaluate([label], [prediction(label, secondary=[])])["overall"]
        self.assertEqual(missing["correct_field_decisions"], 2)
        self.assertEqual(missing["unnecessary_abstentions"], 1)

    def test_wrong_semantics_and_invalid_citations_union_without_double_counting(self):
        label = key()
        output = prediction(label, category="cup_holder", secondary=["organize_cables"])
        issues = {"record-1": [
            {"field": "category", "label": "cup_holder", "code": "excerpt_not_found"},
            {"field": "category", "label": "cup_holder", "code": "missing_evidence"},
            {"field": "primary_use", "label": "drink_beverages", "code": "excerpt_not_found"},
            {"field": "review", "code": "invalid_review"},
        ]}
        counts = evaluate([label], [output], evidence_checks=issues)["overall"]
        self.assertEqual(counts["correct_field_decisions"], 1)
        self.assertEqual(counts["unsupported_assertions"], 3)
        self.assertEqual(counts["evidence_issue_count"], 4)

    def test_abstention_policy_removes_assertion_before_citation_accounting(self):
        label = key(categories=("unknown",), primary=("unknown",))
        issues = {"record-1": [{"field": "category", "label": "drinking_cup", "code": "missing_evidence"}]}
        counts = evaluate([label], [prediction(label)], evidence_checks=issues)["overall"]
        self.assertEqual(counts["unsupported_assertions"], 0)
        self.assertEqual(counts["necessary_abstentions"], 3)
        self.assertEqual(counts["evidence_issue_count"], 1)

    def test_missing_duplicates_and_unexpected_records_keep_fixed_denominator(self):
        labels = [key("one"), key("two"), key("three")]
        outputs = [prediction(labels[0]), prediction(labels[1]), prediction(labels[1]), prediction(key("extra"))]
        counts = evaluate(labels, outputs)["overall"]
        self.assertEqual(counts["total_field_decisions"], 9)
        self.assertEqual(counts["correct_field_decisions"], 3)
        self.assertEqual(counts["incorrect_field_decisions"], 6)
        self.assertEqual(counts["missing_outputs"], 1)
        self.assertEqual(counts["duplicate_outputs"], 1)
        self.assertEqual(counts["unexpected_outputs"], 1)

    def test_abstaining_on_every_listing_cannot_win(self):
        _, labels = authored_split("development")
        outputs = [prediction(label, category="unknown", primary="unknown", secondary=[]) for label in labels]
        counts = evaluate(labels, outputs)["overall"]
        self.assertEqual(counts["total_field_decisions"], 72)
        self.assertLess(counts["correct_field_decisions"], counts["total_field_decisions"])
        self.assertGreater(counts["unnecessary_abstentions"], 30)
        self.assertEqual(counts["unsupported_assertions"], 0)

    def test_failures_are_bounded_and_never_include_expected_answers(self):
        labels = [key("source-{:02d}".format(i)) for i in range(20)]
        outputs = [prediction(label, category="cup_holder", primary="hold_drinking_cup") for label in labels]
        report = evaluate(labels, outputs)
        self.assertEqual(len(report["representative_failures"]), 12)
        for failure in report["development_failures"]:
            self.assertEqual(set(failure), {"source_record_id", "field", "category", "prediction"})
        predicted = [failure["prediction"] for failure in report["representative_failures"]]
        self.assertNotIn("drinking_cup", predicted)
        self.assertNotIn("drink_beverages", predicted)

    def test_malformed_outputs_and_labels_do_not_escape_accounting(self):
        label = key()
        malformed = prediction(label)
        malformed.update(category={}, primary_use=[], secondary_uses=[{}])
        result = evaluate([label], [malformed, "invalid"])
        self.assertEqual(result["overall"]["correct_field_decisions"], 0)
        self.assertEqual(result["overall"]["incorrect_field_decisions"], 3)
        self.assertEqual(result["overall"]["unexpected_outputs"], 1)
        with self.assertRaises(ValueError):
            evaluate([label, label], [])
        invalid_key = copy.deepcopy(label)
        invalid_key["acceptable_categories"] = ["unknown", "drinking_cup"]
        with self.assertRaises(ValueError):
            evaluate([invalid_key], [])

    def test_validation_acceptance_requires_strict_gain_and_no_new_unsupported(self):
        label = key()
        baseline = evaluate([label], [prediction(label, category="unknown", primary="unknown")], "validation")
        candidate = evaluate([label], [prediction(label)], "validation")
        self.assertTrue(candidate_is_acceptable(baseline, candidate))
        self.assertFalse(candidate_is_acceptable(candidate, candidate))
        bad_evidence = {"record-1": [{"field": "category", "label": "drinking_cup", "code": "missing_evidence"}]}
        unsupported_candidate = evaluate([label], [prediction(label)], "validation", bad_evidence)
        self.assertEqual(candidate_decision(baseline, unsupported_candidate),
                         {"accepted": False, "reason": "unsupported_assertions_increased"})
        with_extra = evaluate([label], [prediction(label), prediction(key("extra"))], "validation")
        self.assertEqual(candidate_decision(baseline, with_extra)["reason"], "incomplete_or_duplicate_outputs")
        different = evaluate([key("different")], [prediction(key("different"))], "validation")
        self.assertFalse(candidate_is_acceptable(baseline, different))

    def test_development_and_final_cannot_select_policy(self):
        for split in ("development", "final"):
            report = evaluate([key()], [], split)
            with self.assertRaises(ValueError):
                candidate_is_acceptable(report, report)
            if split == "final":
                self.assertEqual(report["development_failures"], [])
                self.assertEqual(report["representative_failures"], [])
        empty = evaluate([], [], "validation")
        self.assertFalse(candidate_is_acceptable(empty, empty))


if __name__ == "__main__":
    unittest.main()
