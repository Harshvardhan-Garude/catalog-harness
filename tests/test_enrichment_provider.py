"""Offline provider/evidence contracts: no network or credential reads."""

import copy
import json
import unittest
from unittest.mock import MagicMock

from catalog_harness.openai_http import OpenAIError
from enrichment.evidence import evidence_issues, validate_evidence
from enrichment.provider import (BASELINE_POLICY, BASE_INSTRUCTIONS, build_input, enrich_batch,
                                 instructions, propose_policy, validate_policy)


TAXONOMY = {"version": "semantic-v1", "categories": {
    "drinking_cup": "Vessel explicitly intended for drinking.", "cup_holder": "Accessory holding a cup.",
    "desk_organizer": "Organizer for a desk.", "unknown": "Insufficient or conflicting evidence."},
    "uses": {"drink_beverages": "Drink beverages.", "hold_drinking_cup": "Hold a drinking cup.",
             "organize_desk_items": "Organize desk items.", "unknown": "Insufficient or conflicting evidence."},
    "rules": ["Do not invent intended uses."]}
PRODUCT = {"source_record_id": "opaque-001", "fields": {
    "name": "Office mug", "description": "Drink beverages from this mug.",
    "supplier_notes": "Also designed to organize desk items."},
    "normalized_product": {"product_id": "P-001", "name": "Office mug", "weight_g": 250}}
OUTPUT = {"source_record_id": "opaque-001", "category": "drinking_cup", "primary_use": "drink_beverages",
    "secondary_uses": ["organize_desk_items"], "evidence": [
        {"field": "category", "label": "drinking_cup", "source_field": "description", "excerpt": "Drink beverages from this mug."},
        {"field": "primary_use", "label": "drink_beverages", "source_field": "description", "excerpt": "Drink beverages"},
        {"field": "secondary_uses", "label": "organize_desk_items", "source_field": "supplier_notes", "excerpt": "organize desk items"}],
    "review": []}
USAGE = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}


def budget_for(outputs=None):
    budget = MagicMock()
    budget.request.return_value = ({"products": [copy.deepcopy(OUTPUT)]} if outputs is None else outputs,
                                   {"model": "gpt-4.1-mini", "usage": dict(USAGE)})
    return budget


class EvidenceTests(unittest.TestCase):
    def test_complete_exact_evidence_passes_and_does_not_claim_semantic_truth(self):
        self.assertEqual(validate_evidence(PRODUCT, OUTPUT, TAXONOMY), [])
        # A literal citation can still be semantically false. This validator
        # deliberately leaves interpretation to the separate frozen evaluator.
        wrong = copy.deepcopy(OUTPUT)
        wrong["category"] = "cup_holder"
        wrong["evidence"][0]["label"] = "cup_holder"
        self.assertEqual(validate_evidence(PRODUCT, wrong, TAXONOMY), [])

    def test_wrong_case_wrong_source_and_empty_excerpts_are_invalid(self):
        for mutation, code in (("case", "excerpt_not_found"), ("source", "excerpt_not_found"),
                               ("empty", "empty_excerpt"), ("normalized", "invalid_source_field")):
            row = copy.deepcopy(OUTPUT)
            if mutation == "case":
                row["evidence"][1]["excerpt"] = "drink beverages"
            elif mutation == "source":
                row["evidence"][1]["source_field"] = "name"
            elif mutation == "empty":
                row["evidence"][1]["excerpt"] = " "
            else:
                row["evidence"][1]["source_field"] = "normalized_product"
            with self.subTest(mutation=mutation):
                issues = validate_evidence(PRODUCT, row, TAXONOMY)
                self.assertIn({"field": "primary_use", "code": code}, issues)
                self.assertIn({"field": "primary_use", "code": "missing_evidence"}, issues)

    def test_each_secondary_and_asserted_field_needs_own_evidence(self):
        row = copy.deepcopy(OUTPUT)
        row["evidence"] = row["evidence"][:1]
        issues = evidence_issues(PRODUCT, row, TAXONOMY)
        self.assertIn({"field": "primary_use", "code": "missing_evidence", "label": "drink_beverages"}, issues)
        self.assertIn({"field": "secondary_uses", "code": "missing_evidence", "label": "organize_desk_items"}, issues)

    def test_unknown_requires_review_and_never_requires_evidence(self):
        row = copy.deepcopy(OUTPUT)
        row.update(category="unknown", primary_use="unknown", secondary_uses=[], evidence=[])
        self.assertEqual(validate_evidence(PRODUCT, row, TAXONOMY), [
            {"field": "category", "code": "unknown_requires_review"},
            {"field": "primary_use", "code": "unknown_requires_review"}])
        row["review"] = [{"field": "category", "reason": "Conflicting listing."},
                         {"field": "primary_use", "reason": "Insufficient evidence."}]
        self.assertEqual(validate_evidence(PRODUCT, row, TAXONOMY), [])

    def test_invalid_secondary_values_and_unused_evidence_are_reported(self):
        row = copy.deepcopy(OUTPUT)
        row["secondary_uses"] = ["unknown", "drink_beverages", "organize_desk_items", "organize_desk_items"]
        issues = validate_evidence(PRODUCT, row, TAXONOMY)
        for code in ("invalid_label", "duplicate_label", "duplicates_primary"):
            self.assertIn({"field": "secondary_uses", "code": code}, issues)
        row = copy.deepcopy(OUTPUT)
        row["secondary_uses"] = []
        self.assertIn({"field": "secondary_uses", "code": "evidence_not_asserted"},
                      validate_evidence(PRODUCT, row, TAXONOMY))

    def test_invalid_shapes_and_labels_return_only_sanitized_issue_metadata(self):
        for row in (None, [], {}, {"source_record_id": "secret-marker"}):
            self.assertEqual(validate_evidence(PRODUCT, row, TAXONOMY), [{"field": "output", "code": "invalid_shape"}])
        row = copy.deepcopy(OUTPUT)
        row["category"] = "secret-marker"
        issues = validate_evidence(PRODUCT, row, TAXONOMY)
        self.assertIn({"field": "category", "code": "invalid_label"}, issues)
        self.assertNotIn("secret-marker", json.dumps(issues))
        self.assertTrue(all(set(issue) == {"field", "code"} for issue in issues))


class ProviderTests(unittest.TestCase):
    def test_strict_bounded_request_uses_shared_budget_and_whitelists_context(self):
        product = copy.deepcopy(PRODUCT)
        product.update(expected_answer="private-answer", evaluator={"secret": "private-evaluator"})
        product["fields"]["expected"] = "private-field-answer"
        product["normalized_product"]["hidden"] = "private-normalized-answer"
        taxonomy = copy.deepcopy(TAXONOMY)
        taxonomy["expected_answers"] = "private-taxonomy-answer"
        budget = budget_for()
        outputs, details, checks = enrich_batch([product], taxonomy, BASELINE_POLICY, budget, "baseline_development")
        budget.request.assert_called_once()
        args, request = budget.request.call_args
        self.assertEqual(args, ("baseline_development",))
        self.assertEqual(request["max_output_tokens"], 2048)
        self.assertFalse(request["schema"]["additionalProperties"])
        self.assertFalse(request["schema"]["properties"]["products"]["items"]["additionalProperties"])
        self.assertLessEqual(len(request["input_text"].encode()), 16384)
        self.assertNotIn("private-", request["input_text"])
        self.assertNotIn("api_key", request)
        self.assertEqual(outputs, [OUTPUT])
        self.assertEqual(details["usage"], USAGE)
        self.assertEqual(checks, {"opaque-001": []})

    def test_batch_duplicate_and_input_limits_prevent_request(self):
        oversized = copy.deepcopy(PRODUCT)
        oversized["fields"]["description"] = "x" * 17000
        for products, code in (([], "batch_budget"), ([PRODUCT] * 5, "batch_budget"),
                               ([PRODUCT, PRODUCT], "duplicate_input"), ([oversized], "input_budget")):
            budget = budget_for()
            with self.subTest(code=code), self.assertRaises(OpenAIError) as raised:
                enrich_batch(products, TAXONOMY, BASELINE_POLICY, budget, "baseline")
            self.assertEqual(raised.exception.code, code)
            budget.request.assert_not_called()

    def test_network_failure_propagates_once_without_retry(self):
        budget = budget_for()
        budget.request.side_effect = OpenAIError("network", "timeout")
        with self.assertRaises(OpenAIError) as raised:
            enrich_batch([PRODUCT], TAXONOMY, BASELINE_POLICY, budget, "baseline")
        self.assertEqual(raised.exception.code, "timeout")
        budget.request.assert_called_once()

    def test_invalid_model_shape_is_sanitized_with_usage(self):
        for output in ({"secret": "private"}, {"products": [None]}, {"products": [dict(OUTPUT, extra="private")]}):
            with self.subTest(output=output), self.assertRaises(OpenAIError) as raised:
                enrich_batch([PRODUCT], TAXONOMY, BASELINE_POLICY, budget_for(output), "baseline")
            self.assertEqual(raised.exception.code, "invalid_enrichment_shape")
            self.assertEqual(raised.exception.usage, USAGE)
            self.assertNotIn("private", str(raised.exception))

    def test_missing_and_duplicate_outputs_remain_visible_to_evaluator(self):
        for rows in ([], [OUTPUT, OUTPUT]):
            outputs, _, _ = enrich_batch([PRODUCT], TAXONOMY, BASELINE_POLICY, budget_for({"products": rows}), "baseline")
            self.assertEqual(len(outputs), len(rows))

    def test_duplicate_output_evidence_issues_are_merged_without_losing_first_row(self):
        invalid = copy.deepcopy(OUTPUT)
        invalid["evidence"][1]["excerpt"] = "not in source"
        _, details, checks = enrich_batch([PRODUCT], TAXONOMY, BASELINE_POLICY,
            budget_for({"products": [invalid, OUTPUT]}), "baseline")
        self.assertIn({"field": "primary_use", "code": "excerpt_not_found", "label": "drink_beverages"},
                      checks["opaque-001"])
        self.assertTrue(details["original_evidence_checks_by_output"][0])
        self.assertEqual(details["original_evidence_checks_by_output"][1], [])

    def test_report_only_preserves_prediction_but_records_bad_evidence(self):
        row = copy.deepcopy(OUTPUT)
        row["evidence"][1]["excerpt"] = "not in listing"
        outputs, details, checks = enrich_batch([PRODUCT], TAXONOMY, BASELINE_POLICY,
            budget_for({"products": [row]}), "baseline")
        self.assertEqual(outputs, [row])
        self.assertEqual(details["model_outputs"], [row])
        self.assertIn({"field": "primary_use", "code": "excerpt_not_found", "label": "drink_beverages"}, checks["opaque-001"])

    def test_candidate_behavior_really_abstains_invalid_field_and_rechecks(self):
        row = copy.deepcopy(OUTPUT)
        row["evidence"][1]["excerpt"] = "not in listing"
        policy = dict(BASELINE_POLICY, version="semantic-policy-v1-candidate", review_behavior="abstain_invalid_evidence",
                      instruction_addendum="Pay attention to conflicts between notes and descriptions.")
        budget = budget_for({"products": [row]})
        outputs, details, checks = enrich_batch([PRODUCT], TAXONOMY, policy, budget, "candidate")
        result = outputs[0]
        self.assertEqual(result["primary_use"], "unknown")
        self.assertEqual(result["category"], "drinking_cup")
        self.assertEqual(result["secondary_uses"], ["organize_desk_items"])
        self.assertTrue(any(review["field"] == "primary_use" for review in result["review"]))
        self.assertFalse(any(evidence["field"] == "primary_use" for evidence in result["evidence"]))
        self.assertEqual(checks["opaque-001"], [])
        self.assertTrue(details["original_evidence_checks"]["opaque-001"])
        self.assertEqual(details["model_outputs"][0]["primary_use"], "drink_beverages")
        prompt = budget.request.call_args[1]["instructions"]
        self.assertIn(policy["instruction_addendum"], prompt)
        self.assertTrue(prompt.endswith(BASE_INSTRUCTIONS))

    def test_abstaining_invalid_secondary_does_not_change_primary(self):
        row = copy.deepcopy(OUTPUT)
        row["evidence"] = row["evidence"][:2]
        policy = dict(BASELINE_POLICY, review_behavior="abstain_invalid_evidence")
        outputs, _, checks = enrich_batch([PRODUCT], TAXONOMY, policy, budget_for({"products": [row]}), "candidate")
        self.assertEqual(outputs[0]["secondary_uses"], [])
        self.assertEqual(outputs[0]["primary_use"], "drink_beverages")
        self.assertEqual(checks["opaque-001"], [])

    def test_policy_shape_limits_and_version_alone_does_not_change_behavior(self):
        invalid = [dict(BASELINE_POLICY, review_behavior="guess"), dict(BASELINE_POLICY, instruction_addendum="x" * 1601),
                   dict(BASELINE_POLICY, hidden="private"), dict(BASELINE_POLICY, version="private/secret")]
        for policy in invalid:
            with self.subTest(policy=policy), self.assertRaises(OpenAIError) as raised:
                validate_policy(policy)
            self.assertEqual(raised.exception.code, "invalid_policy")
        self.assertEqual(instructions(BASELINE_POLICY), instructions(dict(BASELINE_POLICY, version="other-version")))


class PolicyProposalTests(unittest.TestCase):
    def products(self, count=8):
        products, outputs, summaries = [], [], []
        for index in range(count):
            product, output = copy.deepcopy(PRODUCT), copy.deepcopy(OUTPUT)
            identity = "opaque-{:03d}".format(index)
            product["source_record_id"] = output["source_record_id"] = identity
            product["expected_answer"] = "private-product-answer"
            output["expected_answer"] = "private-output-answer"
            products.append(product)
            outputs.append(output)
            summaries.append({"source_record_id": identity, "expected": "private-summary-answer",
                              "issues": [{"field": "category", "code": "incorrect_label", "label": "private-label"},
                                         {"field": "category", "code": "private-code"}]})
        return products, outputs, summaries

    def test_one_bounded_proposal_excludes_hidden_answers_and_limits_examples(self):
        products, outputs, summaries = self.products()
        # Output contract must be exact; hidden fields are stripped at the
        # entry boundary below rather than ever serialized to the proposer.
        for output in outputs:
            output.pop("expected_answer")
        policy = dict(BASELINE_POLICY, version="semantic-policy-v1-candidate", instruction_addendum="Review explicit source conflicts.")
        budget = budget_for(policy)
        result, details = propose_policy(products, outputs, summaries, BASELINE_POLICY, TAXONOMY, budget)
        self.assertEqual(result, policy)
        budget.request.assert_called_once()
        args, request = budget.request.call_args
        self.assertEqual(args, ("policy_proposal",))
        context = json.loads(request["input_text"])
        self.assertEqual(len(context["development_products"]), 6)
        self.assertEqual(len(context["development_predictions"]), 6)
        self.assertEqual(len(context["failure_summaries"]), 6)
        self.assertNotIn("private-", request["input_text"])
        self.assertLessEqual(len(request["input_text"].encode()), 16384)
        self.assertEqual(request["max_output_tokens"], 1024)
        self.assertIn(BASE_INSTRUCTIONS, request["instructions"])
        self.assertEqual(details["usage"], USAGE)

    def test_no_development_failures_skip_model_request(self):
        budget = budget_for()
        with self.assertRaises(OpenAIError) as raised:
            propose_policy([PRODUCT], [OUTPUT], [], BASELINE_POLICY, TAXONOMY, budget)
        self.assertEqual(raised.exception.code, "no_development_failures")
        budget.request.assert_not_called()

    def test_noop_or_invalid_policy_never_silently_becomes_candidate(self):
        products, outputs, summaries = self.products(1)
        for candidate, code in ((dict(BASELINE_POLICY, version="semantic-policy-v1-candidate"), "no_change"),
                                (dict(BASELINE_POLICY, version="other", instruction_addendum="Review conflicts."), "invalid_policy"),
                                (dict(BASELINE_POLICY, instruction_addendum="x" * 1601), "invalid_policy")):
            budget = budget_for(candidate)
            with self.subTest(code=code), self.assertRaises(OpenAIError) as raised:
                propose_policy(products, outputs, summaries, BASELINE_POLICY, TAXONOMY, budget)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.usage, USAGE)
            budget.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
