"""Check literal citations and taxonomy membership, not semantic truth.

A real source excerpt can still support an incorrect interpretation. Semantic
correctness is measured independently against evaluator-only fixture answers.
All returned issues use fixed codes; source text never appears in errors.
"""

FIELDS = ("category", "primary_use", "secondary_uses")
SOURCE_FIELDS = ("name", "description", "supplier_notes")
ROW_FIELDS = {"source_record_id", "category", "primary_use", "secondary_uses", "evidence", "review"}


def allowed_labels(taxonomy):
    return set(taxonomy["categories"]), set(taxonomy["uses"])


def valid_output_shape(output):
    """Reject structural errors separately from citation/label validity."""
    if not isinstance(output, dict) or set(output) != ROW_FIELDS:
        return False
    if not isinstance(output["source_record_id"], str):
        return False
    if any(not isinstance(output[field], str) for field in ("category", "primary_use")):
        return False
    if not isinstance(output["secondary_uses"], list) or any(
            not isinstance(value, str) for value in output["secondary_uses"]):
        return False
    if not isinstance(output["evidence"], list) or not isinstance(output["review"], list):
        return False
    for citation in output["evidence"]:
        if not isinstance(citation, dict) or set(citation) != {"field", "label", "source_field", "excerpt"}:
            return False
        if any(not isinstance(value, str) for value in citation.values()):
            return False
    for review in output["review"]:
        if not isinstance(review, dict) or set(review) != {"field", "reason"}:
            return False
        if any(not isinstance(value, str) for value in review.values()):
            return False
    return True


def evidence_issues(product, output, taxonomy):
    """Detailed safe issues, with assertion labels for evaluator deduplication."""
    if not valid_output_shape(output):
        return [{"field": "output", "code": "invalid_shape"}]
    issues = []

    def issue(field, code, label=None):
        value = {"field": field if field in FIELDS else "output", "code": code}
        if label is not None:
            value["label"] = label
        if value not in issues:
            issues.append(value)

    categories, uses = allowed_labels(taxonomy)
    assertions = {"category": [output["category"]], "primary_use": [output["primary_use"]],
                  "secondary_uses": output["secondary_uses"]}
    if output["source_record_id"] != product.get("source_record_id"):
        issue("output", "source_identity")
    for field, labels in assertions.items():
        allowed = categories if field == "category" else uses
        for label in labels:
            if label not in allowed or (field == "secondary_uses" and label == "unknown"):
                # Only allowlisted taxonomy labels are exposed in error metadata.
                issue(field, "invalid_label", label if label in allowed else None)
        if len(labels) != len(set(labels)):
            for label in sorted(set(label for label in labels if labels.count(label) > 1)):
                issue(field, "duplicate_label", label if label in allowed else None)
    if output["primary_use"] in output["secondary_uses"]:
        issue("secondary_uses", "duplicates_primary", output["primary_use"] if output["primary_use"] in uses else None)

    reviewed = set()
    for review in output["review"]:
        if review["field"] not in FIELDS:
            issue("output", "invalid_review_field")
        elif not review["reason"].strip():
            issue(review["field"], "empty_review_reason")
        else:
            reviewed.add(review["field"])
    for field in ("category", "primary_use"):
        if output[field] == "unknown" and field not in reviewed:
            issue(field, "unknown_requires_review")

    supported = set()
    source = product.get("fields", {})
    for citation in output["evidence"]:
        field, label = citation["field"], citation["label"]
        if field not in FIELDS:
            issue("output", "invalid_evidence_field")
            continue
        allowed = categories if field == "category" else uses
        safe_label = label if label in allowed else None
        if label == "unknown" or label not in assertions[field]:
            issue(field, "evidence_not_asserted", safe_label)
            continue
        if label not in allowed:
            issue(field, "invalid_label")
            continue
        source_field = citation["source_field"]
        excerpt = citation["excerpt"]
        if source_field not in SOURCE_FIELDS:
            issue(field, "invalid_source_field", label)
        elif not excerpt.strip():
            issue(field, "empty_excerpt", label)
        elif not isinstance(source.get(source_field), str) or excerpt not in source[source_field]:
            issue(field, "excerpt_not_found", label)
        else:
            supported.add((field, label))
    for field, labels in assertions.items():
        for label in labels:
            if label != "unknown" and label in (categories if field == "category" else uses):
                if (field, label) not in supported:
                    issue(field, "missing_evidence", label)
    return issues


def validate_evidence(product, output, taxonomy):
    """Return sanitized field/code issues; never raises for invalid model content.

    Success means only that allowed labels have literal, nonempty source
    citations. It does not establish the semantic correctness of those labels.
    """
    result = []
    for value in evidence_issues(product, output, taxonomy):
        issue = {"field": value["field"], "code": value["code"]}
        if issue not in result:
            result.append(issue)
    return result

