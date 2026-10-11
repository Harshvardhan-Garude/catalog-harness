"""Separate semantic results, linked to immutable raw and normalized sources."""

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pymongo import UpdateOne

from catalog_harness.pipeline import run_fixture
from catalog_harness.storage import ROOT, EventLog, batches, fingerprint, stable_json


class EnrichmentStore:
    def __init__(self, normalizer_store):
        self.normalizer = normalizer_store
        db = normalizer_store.raw.database
        self.results = db["catalog_harness_enrichment_results"]
        self.runs = db["catalog_harness_enrichment_runs"]
        self.policies = db["catalog_harness_enrichment_policies"]
        self.experiments = db["catalog_harness_enrichment_experiments"]

    def ensure_indexes(self):
        # Every model row must survive persistence, including duplicate source
        # identities that the evaluator must count rather than silently merge.
        self.results.create_index([("run_id", 1), ("output_index", 1)], unique=True,
                                  name="enrichment_output_identity")
        self.results.create_index([("run_id", 1), ("source_record_id", 1)],
                                  name="enrichment_run_source")

    def prepare_sources(self, products, split, directory):
        if split not in ("development", "validation"):
            raise ValueError("Live source preparation excludes final holdout")
        dataset_id = "semantic-v1-" + split
        directory = Path(directory) / split
        directory.mkdir(parents=True, exist_ok=True)
        sources = []
        for product in products:
            fields = product["fields"]
            sources.append({"dataset_id": dataset_id, "supplier_id": "northstar",
                "source_record_id": product["source_record_id"], "payload": {
                    "sku": product["normalized_product"]["product_id"], "title": fields["name"],
                    "weight": product["normalized_product"]["weight_g"], "weight_unit": "g",
                    "description": fields["description"], "supplier_notes": fields["supplier_notes"]}})
        documents = {"manifest.json": json.dumps({"dataset_id": dataset_id, "split": "development",
                      "semantic_split": split, "size": len(products)}, indent=2) + "\n",
                     "suppliers.json": json.dumps([{"supplier_id": "northstar",
                         "unit_documentation": "Each source record explicitly declares grams."}], indent=2) + "\n",
                     "records.jsonl": "".join(stable_json(row) + "\n" for row in sources)}
        for name, content in documents.items():
            path = directory / name
            if path.exists() and path.read_text() != content:
                raise ValueError("Existing semantic raw source differs")
            if not path.exists():
                path.write_text(content)
        self.normalizer.ensure_indexes()
        seeded = self.normalizer.seed(directory)
        repeated = self.normalizer.seed(directory)
        if seeded != repeated:
            raise ValueError("Semantic seed was not repeatable")
        plan = json.loads((ROOT / "fixtures/plan.json").read_text())
        normalizer_run_id = run_fixture(self.normalizer, dataset_id, plan,
                                        EventLog(directory / "normalization-events"))
        results = {row["source_record_id"]: row for row in self.normalizer.run_results(normalizer_run_id)}
        for product in products:
            result = results.get(product["source_record_id"])
            if not result or result["status"] != "normalized" or result["product"] != product["normalized_product"]:
                raise ValueError("Deterministic normalization does not match semantic source")
        if len(results) != len(products):
            raise ValueError("Normalization source identity mismatch")
        return {"dataset_id": dataset_id, "normalization_run_id": normalizer_run_id,
                "raw_digest": self.normalizer.raw_digest(dataset_id), "seed_repeatable": True}

    def start_run(self, experiment_id, variant, split, policy, source, freeze_hash):
        policy_id = fingerprint(policy)
        self.policies.update_one({"_id": policy_id}, {"$setOnInsert": policy}, upsert=True)
        run_id = uuid4().hex
        self.runs.insert_one({"_id": run_id, "experiment_id": experiment_id, "variant": variant,
                              "split": split, "policy_id": policy_id, "fixture_version": "semantic-v1",
                              "freeze_sha256": freeze_hash, "source": source,
                              "kind": "synthetic_semantic_enrichment", "status": "started",
                              "started_at": datetime.now(timezone.utc)})
        return run_id

    def save_batch(self, run_id, outputs, details, checks, offset=0):
        if type(offset) is not int or offset < 0:
            raise ValueError("Invalid enrichment output offset")
        raw_outputs = details.get("model_outputs", [])
        original_checks = details.get("original_evidence_checks", {})
        checks_by_output = details.get("original_evidence_checks_by_output", [])
        cursor = 0
        for batch in batches(outputs, 4):
            operations = []
            for row in batch:
                index = offset + cursor
                document = {"run_id": run_id, "output_index": index,
                    "source_record_id": row["source_record_id"], "output": copy.deepcopy(row),
                    "evidence_checks": copy.deepcopy(checks.get(row["source_record_id"], [])),
                    "model": details["model"], "usage_batch": dict(details["usage"])}
                if cursor < len(raw_outputs):
                    document["original_model_output"] = copy.deepcopy(raw_outputs[cursor])
                document["original_evidence_checks"] = copy.deepcopy(
                    checks_by_output[cursor] if cursor < len(checks_by_output)
                    else original_checks.get(row["source_record_id"], []))
                operations.append(UpdateOne({"run_id": run_id, "output_index": index},
                                  {"$setOnInsert": document}, upsert=True))
                cursor += 1
            self.results.bulk_write(operations, ordered=False)

    def read_results(self, run_id):
        return list(self.results.find({"run_id": run_id}, {"_id": 0}).sort("output_index", 1).limit(100))

    def finish_run(self, run_id, report, model_calls):
        self.runs.update_one({"_id": run_id}, {"$set": {"status": "evaluated", "evaluation": report,
                              "model_calls": model_calls, "finished_at": datetime.now(timezone.utc)}})

    def fail_run(self, run_id, category="local", code="verification_failed"):
        allowed_categories = {"authentication", "permission", "quota_or_rate_limit", "network", "response",
                              "budget", "policy", "configuration", "http_error", "invalid_request", "not_found",
                              "local", "atlas"}
        allowed_codes = {"verification_failed", "persistence_failed", "raw_integrity_failed", "request_failed",
                         "invalid_api_key", "insufficient_quota", "rate_limit_exceeded", "model_not_found",
                         "invalid_request_error", "unsupported_parameter", "invalid_json_schema",
                         "context_length_exceeded", "model_access_denied", "timeout", "tls", "connection",
                         "incomplete", "refusal", "invalid_json", "invalid_shape", "invalid_structured_json",
                         "unexpected_model", "model_changed", "missing_usage", "too_large", "call_limit",
                         "experiment_stopped", "invalid_enrichment_shape", "unexpected_source_identity",
                         "invalid_policy", "no_change"}
        safe_category = category if isinstance(category, str) and category in allowed_categories else "local"
        safe_code = code if isinstance(code, str) and (code in allowed_codes or re.fullmatch(r"http_[1-5][0-9]{2}", code)) else "verification_failed"
        self.runs.update_one({"_id": run_id}, {"$set": {"status": "failed", "failure": {
            "category": safe_category, "code": safe_code}, "finished_at": datetime.now(timezone.utc)}})
