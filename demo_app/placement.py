"""Explicit, deterministic merchandising rules over saved semantic facts.

This demo feature is not a new model result or a measured semantic improvement.
Store paths never replace the underlying product category, uses, or raw record.
"""

import json
from pathlib import Path

from catalog_harness.storage import fingerprint
from enrichment.evidence import validate_evidence, valid_output_shape

PROFILE_FILES = {"homeware": "homeware.json", "gift": "gift.json"}


def load_profile(root, store_id, taxonomy):
    if not isinstance(store_id, str) or store_id not in PROFILE_FILES:
        raise ValueError("Unknown store profile")
    path = Path(root) / "demo_app/profiles" / PROFILE_FILES[store_id]
    try:
        if path.is_symlink() or path.stat().st_size > 32768:
            raise ValueError("Invalid store profile file")
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Store profile is missing or invalid") from None
    expected = {"store_id", "name", "version", "description", "allowed_paths", "rules", "unmapped_behavior"}
    if not isinstance(profile, dict) or set(profile) != expected or profile["store_id"] != store_id:
        raise ValueError("Invalid store profile schema")
    if any(not isinstance(profile[key], str) or not profile[key].strip() or len(profile[key]) > 300
           for key in ("name", "version", "description")) or profile["unmapped_behavior"] != "needs_review":
        raise ValueError("Invalid store profile schema")
    paths, rules = profile["allowed_paths"], profile["rules"]
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 30 or
            any(not isinstance(path, str) or not path.strip() or len(path) > 200 for path in paths) or
            len(paths) != len(set(paths)) or not isinstance(rules, list) or len(rules) > 60):
        raise ValueError("Invalid store profile rules")
    identities, pairs = set(), set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"rule_id", "category", "primary_use", "path"}:
            raise ValueError("Invalid store profile rule")
        if any(not isinstance(value, str) for value in rule.values()):
            raise ValueError("Invalid store profile rule")
        pair = (rule["category"], rule["primary_use"])
        if (not rule["rule_id"] or len(rule["rule_id"]) > 80 or rule["rule_id"] in identities or pair in pairs or
                rule["category"] not in taxonomy["categories"] or rule["category"] == "unknown" or
                rule["primary_use"] not in taxonomy["uses"] or rule["primary_use"] == "unknown" or
                rule["path"] not in paths):
            raise ValueError("Invalid or ambiguous store profile rule")
        identities.add(rule["rule_id"])
        pairs.add(pair)
    return profile


def profile_hash(profile):
    return fingerprint(profile)


def placement(product, semantic, profile, taxonomy):
    output = semantic["output"] if isinstance(semantic.get("output"), dict) else {}
    reasons = []
    if not valid_output_shape(output):
        reasons.append("Saved semantic output is structurally invalid.")
    else:
        if output["category"] == "unknown":
            reasons.append("Product category needs review.")
        if output["primary_use"] == "unknown":
            reasons.append("Primary use needs review.")
        if output["review"]:
            reasons.append("Saved enrichment requests human review.")
        if semantic.get("evidence_checks") or validate_evidence(product, output, taxonomy):
            reasons.append("Saved facts have literal evidence validation issues.")
    result = {"source_record_id": product["source_record_id"], "split": product["split"],
              "name": product["fields"]["name"], "semantic_category": output.get("category"),
              "primary_use": output.get("primary_use"), "semantic_run_id": semantic["run_id"],
              "policy_version": semantic["policy_version"], "store_category": None, "rule_id": None,
              "status": "needs_review", "reasons": reasons}
    if reasons:
        return result
    matches = [rule for rule in profile["rules"] if
               rule["category"] == output["category"] and rule["primary_use"] == output["primary_use"]]
    if len(matches) != 1:
        result.update(status="unmapped", reasons=["No explicit store rule matches the supported category and primary use."])
    else:
        result.update(status="ready", store_category=matches[0]["path"], rule_id=matches[0]["rule_id"])
    return result
