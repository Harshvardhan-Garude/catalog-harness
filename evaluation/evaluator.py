"""Fixed evaluator and acceptance criteria; this is outside the agent tool surface."""

import math
import hashlib
import json
from collections import Counter, defaultdict


ABS_TOLERANCE_G = 1e-6
REL_TOLERANCE = 1e-9
MIN_RECOVERABLE_COVERAGE = 0.95


def identity(row):
    return (row.get("dataset_id"), row.get("supplier_id"), row.get("source_record_id"))


def product_matches(actual, expected):
    if not isinstance(actual, dict) or set(actual) != {"product_id", "name", "weight_g"}:
        return False
    weight = actual.get("weight_g")
    return (actual.get("product_id") == expected["product_id"] and actual.get("name") == expected["name"]
            and type(weight) in (int, float) and math.isfinite(weight) and weight > 0
            and math.isclose(weight, expected["weight_g"], rel_tol=REL_TOLERANCE, abs_tol=ABS_TOLERANCE_G))


def empty_counts():
    return {name: 0 for name in ("total_expected", "recoverable", "ambiguous", "correct_normalization",
                                "incorrect_outputs", "correct_quarantine", "unnecessarily_unresolved",
                                "missing_outputs", "duplicate_outputs", "unexpected_outputs")}


def rates(counts):
    def ratio(numerator, denominator):
        return {"count": counts[numerator], "denominator": counts[denominator],
                "rate": counts[numerator] / counts[denominator] if counts[denominator] else None}
    return {"normalization": ratio("correct_normalization", "recoverable"),
            "ambiguity_quarantine": ratio("correct_quarantine", "ambiguous"),
            "unresolved": ratio("unnecessarily_unresolved", "recoverable"),
            "incorrect": ratio("incorrect_outputs", "total_expected")}


def evaluate(labels, outputs, split="development"):
    if split not in ("development", "final"):
        raise ValueError("Invalid evaluation split")
    expected = {}
    for label in labels:
        key = identity(label)
        if key in expected or None in key or label.get("expected_status") not in ("normalized", "quarantined"):
            raise ValueError("Invalid or duplicate evaluator labels")
        expected[key] = label
    found = defaultdict(list)
    for output in outputs:
        found[identity(output)].append(output)
    by_supplier = defaultdict(empty_counts)
    failures = []
    for key, label in expected.items():
        counts = by_supplier[label["supplier_id"]]
        counts["total_expected"] += 1
        recoverable = label["expected_status"] == "normalized"
        counts["recoverable" if recoverable else "ambiguous"] += 1
        matches = found.pop(key, [])
        category = None
        if not matches:
            category = "missing_outputs"
        elif len(matches) != 1:
            counts["duplicate_outputs"] += len(matches) - 1
            category = "incorrect_outputs"
        elif matches[0].get("status") == "quarantined" and "product" not in matches[0]:
            category = "unnecessarily_unresolved" if recoverable else "correct_quarantine"
        elif (recoverable and matches[0].get("status") == "normalized"
              and product_matches(matches[0].get("product"), label["product"])):
            category = "correct_normalization"
        else:
            category = "incorrect_outputs"
        counts[category] += 1
        if split == "development" and category not in ("correct_normalization", "correct_quarantine"):
            # Evidence contains identifiers/category, never ground-truth answers.
            failures.append({"source_record_id": key[2], "supplier_id": key[1], "category": category})
    for key, extras in found.items():
        by_supplier[key[1] or "unknown"]["unexpected_outputs"] += len(extras)
    overall = empty_counts()
    for counts in by_supplier.values():
        for name, value in counts.items():
            overall[name] += value
    def accepted(counts):
        return (counts["total_expected"] > 0 and not any(counts[x] for x in
                ("incorrect_outputs", "missing_outputs", "duplicate_outputs", "unexpected_outputs"))
                and counts["correct_quarantine"] == counts["ambiguous"]
                and (counts["recoverable"] == 0 or counts["correct_normalization"] / counts["recoverable"] >= MIN_RECOVERABLE_COVERAGE))
    identity_digest = hashlib.sha256(json.dumps(sorted(expected), separators=(",", ":")).encode()).hexdigest()
    return {"split": split, "evaluation_identity_sha256": identity_digest,
            "overall": {**overall, "rates": rates(overall)},
            "by_supplier": {sid: {**counts, "rates": rates(counts)} for sid, counts in sorted(by_supplier.items())},
            "acceptance_passed": accepted(overall) and all(accepted(c) for c in by_supplier.values()),
            "development_failures": failures if split == "development" else [],
            "tolerances": {"absolute_g": ABS_TOLERANCE_G, "relative": REL_TOLERANCE}}


def candidate_is_acceptable(baseline, candidate):
    """Development-only guard for later policy selection; no loop is implemented."""
    if baseline["split"] != "development" or candidate["split"] != "development":
        raise ValueError("Final holdout must never select a policy")
    if baseline["evaluation_identity_sha256"] != candidate["evaluation_identity_sha256"]:
        return False
    # Unexpected outputs can introduce diagnostic-only suppliers. Removing those
    # baseline errors must not count as changing the expected evaluation cohort.
    baseline_suppliers = {sid for sid, counts in baseline["by_supplier"].items()
                          if counts["total_expected"]}
    candidate_suppliers = {sid for sid, counts in candidate["by_supplier"].items()
                           if counts["total_expected"]}
    if baseline_suppliers != candidate_suppliers:
        return False
    for sid in baseline_suppliers:
        old = baseline["by_supplier"][sid]
        new = candidate["by_supplier"][sid]
        if any(old[k] != new[k] for k in ("total_expected", "recoverable", "ambiguous")):
            return False
        if new["correct_normalization"] < old["correct_normalization"]:
            return False
    return (candidate["acceptance_passed"] and candidate["overall"]["correct_normalization"]
            > baseline["overall"]["correct_normalization"])
