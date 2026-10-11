"""Frozen semantic scoring, independent of the v1 normalizer evaluator.

Exact excerpt validation checks provenance, not meaning. Semantic keys and
mechanical evidence issues therefore remain separately reported. A single
assertion that fails both checks counts once as unsupported.
"""

import hashlib
import json
from collections import defaultdict


SCORING_VERSION = "semantic-v1-scoring-1"
OBJECTIVE_VERSION = "semantic-v1-validation-objective-1"
FIELDS = ("category", "primary_use", "secondary_uses")
MAX_FAILURE_EXAMPLES = 12
CATEGORIES = frozenset(("drinking_cup", "measuring_cup", "desk_organizer", "cup_holder",
                        "decorative_vessel", "lighting", "gardening_kit", "cable_accessory",
                        "storage_container", "unknown"))
USES = frozenset(("drink_beverages", "measure_materials", "organize_desk_items", "hold_drinking_cup",
                 "display_decoration", "provide_light", "gardening", "organize_cables", "store_items", "unknown"))
COMPARISON_OBJECTIVE = {
    "version": OBJECTIVE_VERSION,
    "selection_split": "validation",
    "correct_field_decisions": "strictly_greater",
    "unsupported_assertions": "no_increase",
    "missing_outputs": 0,
    "duplicate_outputs": 0,
    "unexpected_outputs": 0,
    "identical_fixture_identities_and_denominators": True,
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validated_labels(labels):
    expected = {}
    label_keys = {"source_record_id", "acceptable_categories", "acceptable_primary_uses",
                  "required_secondary_uses", "optional_secondary_uses"}
    for label in labels:
        if not isinstance(label, dict) or set(label) != label_keys:
            raise ValueError("Invalid evaluator label schema")
        source = label["source_record_id"]
        if not isinstance(source, str) or not source or source in expected:
            raise ValueError("Invalid or duplicate evaluator identity")
        for key, allowed in (("acceptable_categories", CATEGORIES), ("acceptable_primary_uses", USES),
                             ("required_secondary_uses", USES - {"unknown"}),
                             ("optional_secondary_uses", USES - {"unknown"})):
            values = label[key]
            if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                    or len(values) != len(set(values)) or not set(values) <= allowed):
                raise ValueError("Invalid evaluator labels")
            if key.startswith("acceptable_") and (not values or ("unknown" in values and len(values) != 1)):
                raise ValueError("Unknown must be the sole acceptable abstention label")
        if set(label["required_secondary_uses"]) & set(label["optional_secondary_uses"]):
            raise ValueError("Required and optional secondary labels must be disjoint")
        expected[source] = label
    return expected


def _safe_prediction(output, field):
    """Only emit bounded taxonomy labels, never arbitrary model prose/evidence."""
    value = output.get(field)
    vocabulary = CATEGORIES if field == "category" else USES
    if field == "secondary_uses":
        if not isinstance(value, list):
            return "invalid_shape"
        return [item if isinstance(item, str) and item in vocabulary else "invalid_label"
                for item in value[:10]]
    return value if isinstance(value, str) and value in vocabulary else "invalid_label"


def _assertions(output):
    assertions = set()
    for field in ("category", "primary_use"):
        value = output.get(field)
        if isinstance(value, str) and value != "unknown":
            assertions.add((field, value))
    secondary = output.get("secondary_uses")
    if isinstance(secondary, list):
        assertions.update(("secondary_uses", value) for value in secondary
                          if isinstance(value, str) and value != "unknown")
    return assertions


def evaluate(labels, outputs, split="development", evidence_checks=None):
    """Account for three field decisions per expected record, including absence.

    evidence_checks maps source IDs to detailed issue lists {field, code, label?}.
    Citation issues with a label count only while that assertion remains in the
    post-policy output. Every mechanical issue is counted separately as well.
    """
    if split not in ("development", "validation", "final"):
        raise ValueError("Invalid enrichment evaluation split")
    expected = _validated_labels(labels)
    found = defaultdict(list)
    for output in outputs:
        if not isinstance(output, dict):
            found[None].append({})
            continue
        identity = output.get("source_record_id")
        if not isinstance(identity, str):
            identity = None
        found[identity].append(output)
    checks = evidence_checks or {}
    counts = {key: 0 for key in (
        "total_expected", "total_field_decisions", "correct_field_decisions", "incorrect_field_decisions",
        "unsupported_assertions", "necessary_abstentions", "unnecessary_abstentions", "missing_outputs",
        "duplicate_outputs", "unexpected_outputs", "evidence_issue_count")}
    counts["total_expected"] = len(expected)
    counts["total_field_decisions"] = 3 * len(expected)
    failures = []
    unsupported = set()

    def fail(source, field, category, output):
        if split != "final" and len(failures) < MAX_FAILURE_EXAMPLES:
            failures.append({"source_record_id": source, "field": field, "category": category,
                             "prediction": _safe_prediction(output, field) if field in FIELDS else None})

    for source, label in expected.items():
        matches = found.pop(source, [])
        if not matches:
            counts["missing_outputs"] += 1
            counts["incorrect_field_decisions"] += 3
            fail(source, "record", "missing_output", {})
            continue
        if len(matches) != 1:
            counts["duplicate_outputs"] += len(matches) - 1
            counts["incorrect_field_decisions"] += 3
            fail(source, "record", "duplicate_output", {})
        else:
            output = matches[0]
            for field, acceptable_key in (("category", "acceptable_categories"),
                                          ("primary_use", "acceptable_primary_uses")):
                prediction = output.get(field)
                allowed = label[acceptable_key]
                correct = isinstance(prediction, str) and prediction in allowed
                counts["correct_field_decisions" if correct else "incorrect_field_decisions"] += 1
                if prediction == "unknown":
                    counts["necessary_abstentions" if "unknown" in allowed else "unnecessary_abstentions"] += 1
                if not correct:
                    fail(source, field, "incorrect_label", output)
            secondary = output.get("secondary_uses")
            shape_ok = isinstance(secondary, list) and all(isinstance(v, str) for v in secondary)
            predicted_set = set(secondary) if shape_ok else set()
            required = set(label["required_secondary_uses"])
            allowed_secondary = required | set(label["optional_secondary_uses"])
            primary = output.get("primary_use")
            repeats_primary = isinstance(primary, str) and primary in predicted_set
            correct = (shape_ok and len(secondary) == len(predicted_set)
                       and required <= predicted_set <= allowed_secondary and not repeats_primary)
            counts["correct_field_decisions" if correct else "incorrect_field_decisions"] += 1
            if shape_ok and not secondary:
                if required:
                    counts["unnecessary_abstentions"] += 1
                elif not allowed_secondary:
                    counts["necessary_abstentions"] += 1
            if not correct:
                category = "missing_required_secondary" if required - predicted_set else "invalid_secondary"
                fail(source, "secondary_uses", category, output)
        # Union per-assertion semantic and evidence failures to avoid double counts.
        assertions = set()
        for output in matches:
            assertions |= _assertions(output)
        allowed_by_field = {
            "category": set(label["acceptable_categories"]),
            "primary_use": set(label["acceptable_primary_uses"]),
            "secondary_uses": set(label["required_secondary_uses"]) | set(label["optional_secondary_uses"]),
        }
        unsupported.update((source, field, value) for field, value in assertions
                           if value not in allowed_by_field[field])
        for issue in checks.get(source, []):
            if not isinstance(issue, dict):
                continue
            counts["evidence_issue_count"] += 1
            field, value = issue.get("field"), issue.get("label")
            if isinstance(value, str) and (field, value) in assertions:
                unsupported.add((source, field, value))
                fail(source, field, "invalid_evidence", matches[0])
    counts["unexpected_outputs"] = sum(len(rows) for rows in found.values())
    counts["unsupported_assertions"] = len(unsupported)
    if counts["correct_field_decisions"] + counts["incorrect_field_decisions"] != counts["total_field_decisions"]:
        raise AssertionError("Field-decision accounting invariant failed")
    return {
        "split": split,
        "fixture_version": "semantic-v1",
        "scoring_version": SCORING_VERSION,
        "objective_version": OBJECTIVE_VERSION,
        "evaluation_identity_sha256": _digest(sorted(expected)),
        "evaluation_labels_sha256": _digest([expected[source] for source in sorted(expected)]),
        "overall": counts,
        "field_accuracy": (counts["correct_field_decisions"] / counts["total_field_decisions"]
                           if counts["total_field_decisions"] else None),
        "representative_failures": failures,
        "development_failures": failures if split == "development" else [],
        "objective": dict(COMPARISON_OBJECTIVE),
    }


def candidate_decision(baseline, candidate):
    """Only untouched validation data selects a policy; final data never does."""
    if baseline.get("split") != "validation" or candidate.get("split") != "validation":
        raise ValueError("Policy selection requires validation results only")
    for key in ("fixture_version", "scoring_version", "objective_version", "evaluation_identity_sha256",
                "evaluation_labels_sha256"):
        if baseline.get(key) != candidate.get(key):
            return {"accepted": False, "reason": "evaluation_contract_mismatch"}
    old, new = baseline["overall"], candidate["overall"]
    for key in ("total_expected", "total_field_decisions"):
        if old[key] != new[key] or not new[key]:
            return {"accepted": False, "reason": "evaluation_denominator_mismatch"}
    if any(new[key] for key in ("missing_outputs", "duplicate_outputs", "unexpected_outputs")):
        return {"accepted": False, "reason": "incomplete_or_duplicate_outputs"}
    if new["unsupported_assertions"] > old["unsupported_assertions"]:
        return {"accepted": False, "reason": "unsupported_assertions_increased"}
    if new["correct_field_decisions"] <= old["correct_field_decisions"]:
        return {"accepted": False, "reason": "no_strict_field_accuracy_improvement"}
    return {"accepted": True, "reason": "validation_improved_without_more_unsupported_assertions"}


def candidate_is_acceptable(baseline, candidate):
    return candidate_decision(baseline, candidate)["accepted"]


def decide_candidate(baseline, candidate):
    """Named decision-report alias for experiment orchestration."""
    return candidate_decision(baseline, candidate)
