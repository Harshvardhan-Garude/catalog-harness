"""Cached saved experiment reads and explicit Atlas-only placement acceptance.

No inference, evaluator answer keys, final products, or source updates are
available through this module. Browsing uses a verified startup snapshot.
"""

import copy
import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from catalog_harness.storage import ROOT, connect, fingerprint, stable_json
from enrichment.evidence import valid_output_shape
from enrichment.provider import instructions
from enrichment.storage import EnrichmentStore

from .placement import PROFILE_FILES, load_profile, placement, profile_hash

EXPERIMENT_ID = "a33a4aedd2ba4ace89cb8a6ec023d178"
SPLIT_COUNTS = {"development": 24, "validation": 12}
PLACEMENTS_COLLECTION = "catalog_harness_demo_placements"
MAX_READ_BYTES = 4 * 1024 * 1024


def _json(path):
    try:
        path = Path(path)
        if path.stat().st_size > MAX_READ_BYTES:
            raise ValueError("Saved demo file exceeds its read limit")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Saved demo file is missing or invalid") from None


def _jsonl(path):
    try:
        path = Path(path)
        if path.stat().st_size > MAX_READ_BYTES:
            raise ValueError("Saved demo file exceeds its read limit")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(rows) > 64:
            raise ValueError("Saved demo read exceeds its row limit")
        return rows
    except (OSError, ValueError):
        raise ValueError("Saved demo source is missing or invalid") from None


def _index(rows, expected=None):
    indexed = {}
    for row in rows:
        identity = row.get("source_record_id") if isinstance(row, dict) else None
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", identity) or identity in indexed:
            raise ValueError("Saved demo identities are invalid or duplicated")
        indexed[identity] = row
    if expected is not None and len(indexed) != expected:
        raise ValueError("Saved demo result count differs from experiment")
    return indexed


def _raw_digest(rows):
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: (item["supplier_id"], item["source_record_id"])):
        digest.update((stable_json(row) + "\n").encode())
    return {"count": len(rows), "sha256": digest.hexdigest()}


def _report(value):
    report = value.get("report", value) if isinstance(value, dict) else None
    if not isinstance(report, dict) or report.get("experiment_id") != EXPERIMENT_ID or report.get("status") != "completed":
        raise ValueError("Completed demo experiment is unavailable")
    if not report.get("atlas_persistence_verified") or not report.get("raw_unchanged"):
        raise ValueError("Demo experiment integrity is unverified")
    if set(report.get("policies", {})) != {"baseline", "candidate"}:
        raise ValueError("Demo experiment policies are unavailable")
    for split, count in SPLIT_COUNTS.items():
        source = report.get("sources", {}).get(split, {})
        if (source.get("dataset_id") != "semantic-v1-" + split or
                not isinstance(source.get("normalization_run_id"), str) or
                source.get("raw_digest", {}).get("count") != count):
            raise ValueError("Demo source count differs from frozen experiment")
        for variant in ("baseline", "candidate"):
            stage = variant + "_" + split
            run = report.get("runs", {}).get(stage, {})
            if (run.get("split") != split or run.get("overall", {}).get("total_expected") != count or
                    run.get("policy_version") != report["policies"][variant].get("version")):
                raise ValueError("Demo run contract differs from experiment")
    return copy.deepcopy(report)


def _semantic(row, run):
    output = row.get("output")
    if (not valid_output_shape(output) or output["source_record_id"] != row["source_record_id"] or
            not isinstance(row.get("evidence_checks"), list)):
        raise ValueError("Saved semantic output is invalid")
    return {"output": copy.deepcopy(output), "original_model_output": copy.deepcopy(row.get("original_model_output")),
            "evidence_checks": copy.deepcopy(row["evidence_checks"]), "run_id": run["run_id"],
            "policy_version": run["policy_version"], "model": row.get("model")}


def _product(raw, normalized, split):
    payload = raw["payload"]
    if not isinstance(payload, dict) or any(not isinstance(payload.get(name), str) for name in
                                          ("title", "description", "supplier_notes")):
        raise ValueError("Saved raw product is invalid")
    if (not isinstance(normalized, dict) or set(normalized) != {"product_id", "name", "weight_g"} or
            normalized.get("product_id") != payload.get("sku")):
        raise ValueError("Saved normalized identity differs from raw source")
    return {"source_record_id": raw["source_record_id"], "split": split,
            "fields": {"name": payload["title"], "description": payload["description"], "supplier_notes": payload["supplier_notes"]},
            "normalized_product": copy.deepcopy(normalized), "raw_payload": copy.deepcopy(payload)}


def _policy_signature(report):
    return fingerprint({"experiment_id": report["experiment_id"], "freeze_sha256": report["freeze_sha256"],
                        "policies": report["policies"], "decision": report["decision"],
                        "runs": {stage: row["run_id"] for stage, row in report["runs"].items()}})


def _final_aggregate(root):
    paths = (Path(root) / "docs/semantic-v1-final-results.json", Path(root) / "artifacts/enrichment-v1-final/report.json")
    for path in paths:
        if not path.exists():
            continue
        try:
            outer = _json(path)
            report = outer.get("report", outer)
            result = {key: copy.deepcopy(report[key]) for key in
                      ("status", "experiment_id", "fixture_version", "final_holdout_evaluated", "raw_unchanged", "atlas_persistence_verified") if key in report}
            result["label"] = "Synthetic final holdout results; aggregate evaluation only"
            result["runs"] = {}
            for stage, run in report.get("runs", {}).items():
                result["runs"][stage] = {key: copy.deepcopy(run[key]) for key in
                    ("overall", "field_accuracy", "model_calls", "token_usage", "policy_version", "run_id", "split") if key in run}
            # No final product inputs, model outputs, failure examples, or keys.
            for key in ("decision", "usage"):
                if key in report:
                    result[key] = copy.deepcopy(report[key])
            return result
        except (ValueError, TypeError, AttributeError):
            continue
    return None


class DemoRepository:
    def __init__(self, root=ROOT, prefer_atlas=True, connector=None):
        self.root = Path(root)
        self.client = None
        self.store = None
        self._previews = {}
        self._lock = threading.RLock()
        self.taxonomy = _json(self.root / "enrichment/taxonomy.json")
        self.profiles = [load_profile(self.root, store_id, self.taxonomy) for store_id in PROFILE_FILES]
        if prefer_atlas:
            try:
                # Driver messages can contain topology hosts; never emit them.
                previous_disable = logging.root.manager.disable
                try:
                    logging.disable(logging.CRITICAL)
                    self.client, normalizer = (connector or connect)()
                    self.store = EnrichmentStore(normalizer)
                    report, products = self._atlas()
                finally:
                    logging.disable(previous_disable)
                self.source = {"kind": "atlas", "label": "Atlas saved experiment", "notice":
                    "Saved OpenAI enrichment results read from Atlas. Browsing makes no model calls."}
            except Exception:
                self.close()
                report, products = self._local()
                self.source = {"kind": "local_fallback", "label": "Local saved results — Atlas unavailable", "notice":
                    "Showing saved local experiment files. Atlas placement acceptance is disabled; no live Atlas verification is claimed."}
        else:
            report, products = self._local()
            self.source = {"kind": "local_fallback", "label": "Local saved results", "notice":
                "Showing saved local experiment files. Atlas placement acceptance is disabled."}
        self.report, self.products = report, products
        self._signature = _policy_signature(report)

    def _atlas(self):
        document = self.store.experiments.find_one({"_id": EXPERIMENT_ID}, {"report": 1})
        if not document:
            raise ValueError("Saved Atlas experiment is unavailable")
        report = _report(document)
        products = []
        for split, count in SPLIT_COUNTS.items():
            source = report["sources"][split]
            raw_rows = list(self.store.normalizer.raw.find({"dataset_id": source["dataset_id"]}, {"_id": 0})
                            .sort([("supplier_id", 1), ("source_record_id", 1)]).limit(65))
            raw = _index(raw_rows, count)
            if _raw_digest(raw_rows) != source["raw_digest"]:
                raise ValueError("Atlas raw digest differs from saved experiment")
            normalized_rows = list(self.store.normalizer.results.find({"run_id": source["normalization_run_id"]}, {"_id": 0})
                                   .limit(65))
            normalized = _index(normalized_rows, count)
            if set(raw) != set(normalized) or any(row.get("status") != "normalized" or
                    row.get("dataset_id") != source["dataset_id"] for row in normalized.values()):
                raise ValueError("Atlas normalization source differs from experiment")
            semantic = {}
            for variant in ("baseline", "candidate"):
                run = report["runs"][variant + "_" + split]
                saved_run = self.store.runs.find_one({"_id": run["run_id"]})
                if (not saved_run or saved_run.get("experiment_id") != EXPERIMENT_ID or saved_run.get("split") != split or
                        saved_run.get("variant") != variant or saved_run.get("status") != "evaluated" or
                        saved_run.get("freeze_sha256") != report["freeze_sha256"] or
                        saved_run.get("policy_id") != fingerprint(report["policies"][variant]) or
                        saved_run.get("evaluation") != run or saved_run.get("source") != source):
                    raise ValueError("Atlas semantic run does not belong to saved experiment")
                rows = list(self.store.results.find({"run_id": run["run_id"]}, {"_id": 0}).sort("output_index", 1).limit(65))
                semantic[variant] = _index(rows, count)
                if set(semantic[variant]) != set(raw) or any(row.get("model") != report["usage"]["model"] for row in rows):
                    raise ValueError("Atlas semantic output identities or models differ")
            for identity in sorted(raw):
                product = _product(raw[identity], normalized[identity]["product"], split)
                for variant in semantic:
                    product[variant] = _semantic(semantic[variant][identity], report["runs"][variant + "_" + split])
                products.append(product)
        return report, products

    def _local(self):
        directory = self.root / "artifacts/enrichment-v1"
        try:
            report = _report(_json(directory / "report.json"))
            products = []
            for split, count in SPLIT_COUNTS.items():
                source = report["sources"][split]
                raw_rows = _jsonl(directory / "normalizer-sources" / split / "records.jsonl")
                raw = _index(raw_rows, count)
                if _raw_digest(raw_rows) != source["raw_digest"]:
                    raise ValueError("Local raw digest differs from saved experiment")
                semantic, inputs = {}, {}
                for variant in ("baseline", "candidate"):
                    run = report["runs"][variant + "_" + split]
                    saved_evaluation = _json(directory / (variant + "_" + split) / "evaluation.json")
                    if saved_evaluation != run:
                        raise ValueError("Local evaluation differs from saved experiment")
                    rows, input_rows = [], []
                    paths = sorted((directory / (variant + "_" + split)).glob("batch-*.json"))
                    if len(paths) != (count + 3) // 4:
                        raise ValueError("Local saved batch count differs from experiment")
                    for path in paths:
                        batch = _json(path)
                        details = batch["details"]
                        if details.get("policy_version") != run["policy_version"] or details.get("model") != report["usage"]["model"]:
                            raise ValueError("Local batch provenance differs from experiment")
                        input_rows.extend(details["input"]["products"])
                        for index, output in enumerate(batch["outputs"]):
                            rows.append({"source_record_id": output["source_record_id"], "output": output,
                                "original_model_output": details["model_outputs"][index], "model": details["model"],
                                "evidence_checks": batch["evidence_checks"][output["source_record_id"]]})
                    semantic[variant], inputs[variant] = _index(rows, count), _index(input_rows, count)
                    if set(semantic[variant]) != set(raw) or set(inputs[variant]) != set(raw):
                        raise ValueError("Local semantic output identities differ from experiment")
                if inputs["baseline"] != inputs["candidate"]:
                    raise ValueError("Local variants used different source inputs")
                for identity in sorted(raw):
                    product = _product(raw[identity], inputs["baseline"][identity]["normalized_product"], split)
                    if product["fields"] != inputs["baseline"][identity]["fields"]:
                        raise ValueError("Local raw fields differ from saved model input")
                    for variant in semantic:
                        product[variant] = _semantic(semantic[variant][identity], report["runs"][variant + "_" + split])
                    products.append(product)
            return report, products
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("Verified Atlas results and complete local saved experiment files are unavailable. Check Atlas access or restore artifacts/enrichment-v1.") from None

    def state(self):
        profiles = [load_profile(self.root, store_id, self.taxonomy) for store_id in PROFILE_FILES]
        return copy.deepcopy({"source": self.source, "experiment": self.report, "products": self.products,
                              "policies": self.report["policies"], "profiles": profiles,
                              "policy_instructions": {variant: instructions(policy) for variant, policy in self.report["policies"].items()},
                              "final_evaluation": _final_aggregate(self.root)})

    def _selected(self, source_record_ids, available):
        if source_record_ids is None:
            return list(available)
        if (not isinstance(source_record_ids, list) or not source_record_ids or len(source_record_ids) > 36 or
                any(not isinstance(identity, str) for identity in source_record_ids) or
                len(source_record_ids) != len(set(source_record_ids)) or not set(source_record_ids) <= set(available)):
            raise ValueError("Select existing unique product IDs")
        return list(source_record_ids)

    def preview(self, store_id, source_record_ids=None):
        with self._lock:
            return self._preview(store_id, source_record_ids)

    def _preview(self, store_id, source_record_ids=None):
        profile = load_profile(self.root, store_id, self.taxonomy)
        indexed = {product["source_record_id"]: product for product in self.products}
        identities = self._selected(source_record_ids, indexed)
        variant = "candidate" if self.report["decision"].get("accepted") else "baseline"
        rows = [placement(indexed[identity], indexed[identity][variant], profile, self.taxonomy) for identity in identities]
        result = {"preview_id": uuid4().hex, "store_id": store_id, "profile_version": profile["version"],
                  "profile_hash": profile_hash(profile), "policy_version": self.report["policies"][variant]["version"],
                  "experiment_id": EXPERIMENT_ID, "source": copy.deepcopy(self.source),
                  "counts": {"selected": len(rows), **{status: sum(row["status"] == status for row in rows)
                                                      for status in ("ready", "needs_review", "unmapped")}},
                  "records": rows, "notice": "Deterministic store placement preview; no new inference or measured semantic improvement."}
        if len(self._previews) >= 32:
            self._previews.pop(next(iter(self._previews)))
        self._previews[result["preview_id"]] = copy.deepcopy(result)
        return result

    def accept(self, preview_id, source_record_ids=None):
        with self._lock:
            return self._accept(preview_id, source_record_ids)

    def _accept(self, preview_id, source_record_ids=None):
        if self.source["kind"] != "atlas" or self.store is None:
            raise ValueError("Atlas acceptance is unavailable while displaying local saved results")
        if not isinstance(preview_id, str) or preview_id not in self._previews:
            raise ValueError("Preview is missing or expired; create a new preview")
        preview = self._previews[preview_id]
        profile = load_profile(self.root, preview["store_id"], self.taxonomy)
        if profile_hash(profile) != preview["profile_hash"]:
            raise ValueError("Store profile changed; create a new preview")
        current = self.store.experiments.find_one({"_id": EXPERIMENT_ID}, {"report": 1})
        if not current or _policy_signature(_report(current)) != self._signature:
            raise ValueError("Saved enrichment policy changed; restart the demo and create a new preview")
        indexed = {row["source_record_id"]: row for row in preview["records"]}
        identities = self._selected(source_record_ids, indexed)
        if not identities or any(indexed[identity]["status"] != "ready" for identity in identities):
            raise ValueError("Only ready placements can be accepted; review uncertain or unmapped products first")
        collection = self.store.normalizer.raw.database[PLACEMENTS_COLLECTION]
        collection.create_index([("experiment_id", 1), ("store_id", 1), ("source_record_id", 1)], unique=True,
                                name="demo_placement_identity")
        for identity in identities:
            row = indexed[identity]
            key = {"experiment_id": EXPERIMENT_ID, "store_id": preview["store_id"], "source_record_id": identity}
            source = self.report["sources"][row["split"]]
            document = {**key, "kind": "demo_store_placement", "preview_id": preview_id,
                        "store_category": row["store_category"], "rule_id": row["rule_id"],
                        "profile_version": preview["profile_version"], "profile_hash": preview["profile_hash"],
                        "policy_version": row["policy_version"], "semantic_run_id": row["semantic_run_id"],
                        "dataset_id": source["dataset_id"], "normalization_run_id": source["normalization_run_id"],
                        "source_raw_digest": source["raw_digest"], "freeze_sha256": self.report["freeze_sha256"],
                        "accepted_at": datetime.now(timezone.utc).isoformat()}
            collection.update_one(key, {"$set": document}, upsert=True)
            saved = collection.find_one(key, {"_id": 0})
            if saved != document:
                raise ValueError("Atlas placement persistence could not be verified")
        return {"status": "accepted", "preview_id": preview_id, "store_id": preview["store_id"],
                "accepted_count": len(identities), "source_record_ids": identities,
                "collection": PLACEMENTS_COLLECTION,
                "notice": "Accepted only into the Atlas demo placements collection; product facts and raw records remain unchanged."}

    def close(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None
        self.store = None
