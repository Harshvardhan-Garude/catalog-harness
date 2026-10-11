"""50k synthetic database benchmark; deterministic processing, zero model calls.

Three separate fixed-seed datasets share isolated benchmark collections. All
generation, cursor reads and bulk writes are bounded to 100 records. The existing
validated hand-authored transformation plan supplies normalization/quarantine.
MongoDB explain command: https://www.mongodb.com/docs/manual/reference/command/explain/
Cursor hint: https://www.mongodb.com/docs/manual/reference/method/cursor.hint/
"""

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
import random
import statistics
import time
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient, UpdateOne

from enrichment.budget import write_json
from .storage import ROOT, batches, fingerprint, stable_json
from .transforms import transform_record, validate_plan


BENCHMARK_ID = "benchmark-v1"
SEED = 4102026
SIZES = (1000, 10000, 50000)
BATCH_SIZE = 100
INDEX_NAME = "benchmark_catalog_queue"
INDEX_KEYS = [("dataset_id", ASCENDING), ("supplier_id", ASCENDING),
              ("status", ASCENDING), ("source_record_id", ASCENDING)]
DIRECTORY = ROOT / "artifacts/benchmark-v1"
SUPPLIERS = {
    "northstar": {"unit_documentation": "Weights carry an explicit unit; there is no default."},
    "cedar": {"default_weight_unit": "kg", "unit_documentation": "Mass is kg unless explicitly declared."},
    "harbor": {"unit_documentation": "Each row must establish its weight unit."}}
FIELDS = {"northstar": ("sku", "title", "weight", "weight_unit"),
          "cedar": ("product_code", "description", "mass", "measurement_unit"),
          "harbor": ("item_id", "label", "shipping_weight", "uom")}


def now():
    return datetime.now(timezone.utc).isoformat()


def dataset_id(size, seed=SEED):
    if type(size) is not int or not 1 <= size <= 50000 or type(seed) is not int or not 0 <= seed < 2 ** 32:
        raise ValueError("Invalid benchmark size or seed")
    return "benchmark-v1-seed{}-n{}".format(seed, size)


def generate_records(size, seed=SEED):
    """Stream synthetic supplier exports; no labels, experiments or LLM imports."""
    identity, rng = dataset_id(size, seed), random.Random(seed)
    for index in range(size):
        slot = index % 100
        supplier = "northstar" if slot < 65 else "cedar" if slot < 90 else "harbor"
        product = "P-{:012x}".format(rng.getrandbits(48))
        name = rng.choice(("Travel Mug", "Desk Lamp", "Garden Kit", "Coffee Tin", "Cable Pack"))
        amount, unit = rng.randint(1, 8000) / 1000.0, rng.choice(("g", "kg", "lb"))
        scenario = rng.choices(("valid", "whitespace", "suffix", "default", "missing_unit", "conflict", "ambiguous_number"),
                               weights=(30, 25, 15, 10, 10, 5, 5))[0]
        fields = FIELDS[supplier]
        payload = dict(zip(fields, (product, name, amount, unit)))
        if scenario == "whitespace":
            payload[fields[0]] = "  " + product + "  "
            payload[fields[1]] = " \t" + name.replace(" ", "   ") + "  "
            payload[fields[2]], payload[fields[3]] = "  {:.3f}  ".format(amount), " " + unit.upper() + " "
        elif scenario == "suffix":
            payload[fields[2]] = "{:.3f} {}".format(amount, {"g": "grams", "kg": "kg", "lb": "lbs"}[unit])
            del payload[fields[3]]
        elif scenario in ("default", "missing_unit"):
            del payload[fields[3]]
        elif scenario == "conflict":
            payload[fields[2]] = "{:.3f} {}".format(amount, "kg" if unit != "kg" else "lb")
        elif scenario == "ambiguous_number":
            payload[fields[2]] = "1,234"
        yield {"dataset_id": identity, "supplier_id": supplier,
               "source_record_id": "bench-{:08d}".format(index + 1), "payload": payload}


def load_plan():
    return validate_plan(json.loads((ROOT / "fixtures/plan.json").read_text()))


def stream_digest(rows):
    digest, count = hashlib.sha256(), 0
    for row in rows:
        digest.update((stable_json(row) + "\n").encode("utf-8"))
        count += 1
    return {"count": count, "sha256": digest.hexdigest()}


class BenchmarkStore:
    def __init__(self, db, batch_size=BATCH_SIZE):
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("Batch size must be between 1 and 100")
        self.db, self.batch_size = db, batch_size
        self.raw = db["catalog_harness_benchmark_raw"]
        self.outputs = db["catalog_harness_benchmark_outputs"]
        self.reports = db["catalog_harness_benchmark_reports"]

    def ensure_indexes(self):
        identity = [("dataset_id", ASCENDING), ("source_record_id", ASCENDING)]
        self.raw.create_index(identity, unique=True, name="benchmark_raw_identity")
        self.outputs.create_index(identity, unique=True, name="benchmark_output_identity")
        self.outputs.create_index(INDEX_KEYS, name=INDEX_NAME)

    def raw_records(self, identity):
        return self.raw.find({"dataset_id": identity}, {"_id": 0}).sort(
            "source_record_id", ASCENDING).batch_size(self.batch_size).max_time_ms(20000)

    def raw_digest(self, identity):
        return stream_digest(self.raw_records(identity))

    def seed(self, size, seed=SEED):
        identity, started = dataset_id(size, seed), time.perf_counter()
        existing = self.raw.count_documents({"dataset_id": identity}, maxTimeMS=20000)
        inserted = matched = 0
        for batch in batches(generate_records(size, seed), self.batch_size):
            operations = [UpdateOne({"dataset_id": identity, "source_record_id": row["source_record_id"]},
                                    {"$setOnInsert": row}, upsert=True) for row in batch]
            result = self.raw.bulk_write(operations, ordered=False)
            inserted += result.upserted_count
            matched += result.matched_count
        seconds = time.perf_counter() - started
        count = self.raw.count_documents({"dataset_id": identity}, maxTimeMS=20000)
        return {"seconds": seconds, "inserted": inserted, "matched": matched,
                "existing_before": existing, "raw_count": count,
                "records_per_second": size / seconds if seconds else None,
                "measurement": "initial_insertion" if existing == 0 else "upsert_with_existing_records"}

    def process(self, identity, plan):
        validate_plan(plan)
        started, counts, plan_hash = time.perf_counter(), {"normalized": 0, "quarantined": 0}, fingerprint(plan)
        for batch in batches(self.raw_records(identity), self.batch_size):
            operations = []
            for row in batch:
                output = transform_record(row, plan, SUPPLIERS[row["supplier_id"]])
                counts[output["status"]] += 1
                output.update(source_sha256=fingerprint(row), plan_sha256=plan_hash,
                              processing_kind="deterministic_hand_authored_plan")
                operations.append(UpdateOne({"dataset_id": identity, "source_record_id": row["source_record_id"]},
                                            {"$setOnInsert": output}, upsert=True))
            self.outputs.bulk_write(operations, ordered=False)
        seconds = time.perf_counter() - started
        return {"seconds": seconds, "records_per_second": sum(counts.values()) / seconds if seconds else None,
                **counts}

    def reconcile(self, identity, plan):
        outputs = self.outputs.find({"dataset_id": identity}, {"_id": 0}).sort(
            "source_record_id", ASCENDING).batch_size(self.batch_size).max_time_ms(20000)
        count = 0
        for raw, output in zip_longest(self.raw_records(identity), outputs):
            if raw is None or output is None:
                return False
            expected = transform_record(raw, plan, SUPPLIERS[raw["supplier_id"]])
            expected.update(source_sha256=fingerprint(raw), plan_sha256=fingerprint(plan),
                            processing_kind="deterministic_hand_authored_plan")
            if output != expected:
                return False
            count += 1
        return count > 0

    def aggregation(self, identity):
        rows = self.outputs.aggregate([
            {"$match": {"dataset_id": identity}},
            {"$group": {"_id": {"supplier_id": "$supplier_id", "status": "$status"}, "records": {"$sum": 1}}},
            {"$sort": {"_id.supplier_id": 1, "_id.status": 1}}, {"$limit": 20}],
            maxTimeMS=20000, batchSize=self.batch_size)
        return [{"supplier_id": row["_id"]["supplier_id"], "status": row["_id"]["status"],
                 "records": row["records"]} for row in rows]


def latency_summary(samples_ms):
    if not samples_ms or any(not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in samples_ms):
        raise ValueError("Invalid latency samples")
    ordered = sorted(samples_ms)
    return {"median_ms": statistics.median(ordered),
            "p95_ms": ordered[math.ceil(.95 * len(ordered)) - 1], "samples": len(ordered)}


def _query(collection, query_filter, sort, hint):
    return list(collection.find(query_filter, {"_id": 0}).sort(sort).hint(hint).limit(25)
                .batch_size(25).max_time_ms(20000))


def measure_query(collection, query_filter, sort, hint, warmup=2, samples=20):
    if type(warmup) is not int or warmup != 2 or type(samples) is not int or samples < 20 or samples > 40:
        raise ValueError("Use two warm-ups and 20 to 40 measured requests")
    page_hash, returned = None, 0
    timings = []
    for ordinal in range(warmup + samples):
        started = time.perf_counter()
        rows = _query(collection, query_filter, sort, hint)
        elapsed = (time.perf_counter() - started) * 1000
        current = fingerprint(rows)
        if page_hash is not None and page_hash != current:
            raise ValueError("Query results changed during measurement")
        page_hash, returned = current, len(rows)
        if ordinal >= warmup:
            timings.append(elapsed)
    # Explain executionStats is deliberately outside timed client requests.
    explained = collection.database.command({"explain": {"find": collection.name,
        "filter": query_filter, "sort": dict(sort), "limit": 25, "hint": hint,
        "maxTimeMS": 20000}, "verbosity": "executionStats"})
    stats = explained["executionStats"]
    metrics = {"n_returned": stats["nReturned"], "docs_examined": stats["totalDocsExamined"],
               "keys_examined": stats["totalKeysExamined"]}
    if any(type(value) is not int or value < 0 for value in metrics.values()) or metrics["n_returned"] != returned:
        raise ValueError("Explain result count differs from measured query")
    return {**latency_summary(timings), "samples_ms": timings, "execution_stats": metrics,
            "page_sha256": page_hash, "returned": returned}


def _read_report(path):
    with Path(path).open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Benchmark report exceeds limit")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Invalid benchmark report")
    return result


def run_benchmark(db, directory=DIRECTORY, sizes=SIZES):
    sizes = tuple(sizes)
    if not sizes or len(set(sizes)) != len(sizes) or any(size not in SIZES for size in sizes):
        raise ValueError("Benchmark sizes must be distinct supported sizes")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "benchmark.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = load_plan()
        path = directory / "report.json"
        if path.exists():
            saved = _read_report(path)
            if (saved.get("benchmark_id") != BENCHMARK_ID or saved.get("seed") != SEED or
                    saved.get("sizes") != list(sizes) or saved.get("plan_sha256") != fingerprint(plan)):
                raise ValueError("Existing benchmark identity differs")
            if saved.get("status") == "completed":
                return saved  # Preserve initial measurements; no automatic rerun.
            report = saved
            report["status"] = "running"
        else:
            report = {"benchmark_id": BENCHMARK_ID, "label": "50k synthetic database benchmark", "version": 1,
                "status": "running", "seed": SEED, "sizes": list(sizes), "dataset_mode": "separate",
                "batch_size": BATCH_SIZE, "warmup_requests": 2, "measured_requests": 20,
                "model_calls_added": 0, "started_at": now(), "plan_sha256": fingerprint(plan),
                "plan_kind": "hand_authored_validated_deterministic", "datasets": [],
                "limitations": ["Synthetic supplier exports; no LLM enrichment or production-traffic claim.",
                    "Client-observed query latency includes network and cursor materialization.",
                    "Single laptop and Atlas sandbox; median/p95 from 20 requests after 2 warm-ups per plan.",
                    "Separate datasets share isolated benchmark collections; natural scans examine all populated namespaces.",
                    "Initial seed timing includes streamed generation and bulk upserts; processing includes reads and bulk writes.",
                    "Index is measured before forced scan; caches/network variability can dominate wall-clock latency.",
                    "p95 uses the nearest-rank definition; executionStats collected separately."]}
        store = BenchmarkStore(db)

        def persist():
            report["updated_at"] = now()
            write_json(path, report)
            store.reports.update_one({"_id": BENCHMARK_ID}, {"$set": report}, upsert=True)

        try:
            store.ensure_indexes()
            persist()
            for size in sizes:
                identity = dataset_id(size)
                existing = next((row for row in report["datasets"] if row["dataset_id"] == identity), None)
                if existing and existing.get("status") == "completed":
                    continue
                entry = existing or {"dataset_id": identity, "requested_records": size, "status": "running", "started_at": now()}
                if existing is None:
                    report["datasets"].append(entry)
                entry["status"] = "running"
                if "seed_initial" not in entry:
                    entry["stage"] = "seed_initial"
                    persist()
                    entry["seed_initial"] = store.seed(size)
                    persist()
                if "seed_repeat" not in entry:
                    entry["stage"] = "seed_repeat"
                    persist()
                    entry["seed_repeat"] = store.seed(size)
                    persist()
                entry["stage"] = "raw_integrity_before"
                before = store.raw_digest(identity)
                expected = stream_digest(generate_records(size))
                if before != expected:
                    raise ValueError("Raw benchmark records differ from generator")
                entry["raw_before"] = before
                entry["raw_count"] = before["count"]
                entry["stage"] = "processing"
                persist()
                if "processing" not in entry:
                    entry["processing"] = store.process(identity, plan)
                    persist()
                entry["stage"] = "integrity_after"
                after = store.raw_digest(identity)
                entry["raw_after"] = after
                entry["output_count"] = store.outputs.count_documents({"dataset_id": identity}, maxTimeMS=20000)
                entry["integrity"] = {"raw_unchanged": before == after,
                    "outputs_reconcile": entry["output_count"] == size and store.reconcile(identity, plan),
                    "repeat_seed_no_duplicates": entry["seed_repeat"]["inserted"] == 0 and
                        entry["seed_repeat"]["raw_count"] == size and entry["seed_repeat"]["matched"] == size}
                if not all(entry["integrity"].values()):
                    raise ValueError("Benchmark integrity verification failed")
                entry["stage"] = "queries"
                persist()
                query_filter = {"dataset_id": identity, "supplier_id": "northstar", "status": "quarantined",
                                "source_record_id": {"$gt": "bench-00000100"}}
                sort = [("source_record_id", ASCENDING)]
                indexed = measure_query(store.outputs, query_filter, sort, INDEX_NAME)
                scan = measure_query(store.outputs, query_filter, sort, {"$natural": 1})
                if indexed["page_sha256"] != scan["page_sha256"]:
                    raise ValueError("Index and scan returned different pages")
                entry["query"] = {"filter": query_filter, "sort": [list(item) for item in sort], "limit": 25,
                    "index_name": INDEX_NAME, "index_keys": [list(item) for item in INDEX_KEYS],
                    "indexed": indexed, "collection_scan": scan, "network_included": True,
                    "pagination": "keyset after source_record_id bench-00000100", "same_page_verified": True,
                    "output_collection_count_at_measurement": store.outputs.count_documents({}, maxTimeMS=20000)}
                entry["aggregation"] = store.aggregation(identity)
                if sum(row["records"] for row in entry["aggregation"]) != size:
                    raise ValueError("Aggregation does not reconcile")
                entry.update(status="completed", stage="completed", completed_at=now())
                persist()
                print(json.dumps({"dataset_id": identity, "status": "completed", "records": size,
                                  "model_calls_added": 0}), flush=True)
            report.update(status="completed", completed_at=now())
            report.pop("error", None)
            persist()
            return report
        except Exception as error:
            from enrichment.atlas_diagnostics import sanitized_error
            safe = {"category": "local", "code": "verification_failed"} if isinstance(error, ValueError) else sanitized_error(error)
            report.update(status="failed", error=safe, failed_at=now())
            write_json(path, report)
            try:
                store.reports.update_one({"_id": BENCHMARK_ID}, {"$set": report}, upsert=True)
            except Exception:
                pass
            raise


def connect_benchmark():
    logging.disable(logging.CRITICAL)
    load_dotenv(ROOT / ".env")
    missing = [name for name in ("MONGODB_URI", "MONGODB_DATABASE")
               if not os.getenv(name, "").strip() or os.getenv(name, "").strip().startswith("YOUR_")]
    if missing:
        raise ValueError("Missing: " + ", ".join(missing))
    client = MongoClient(os.environ["MONGODB_URI"].strip(), serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000, socketTimeoutMS=20000, timeoutMS=30000, retryReads=False, retryWrites=False,
        appname="catalog-harness-benchmark")
    try:
        client.admin.command("ping")
        return client, client[os.environ["MONGODB_DATABASE"].strip()]
    except Exception:
        client.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "report"))
    parser.add_argument("--directory", type=Path, default=DIRECTORY)
    args, client = parser.parse_args(argv), None
    try:
        if args.command == "report":
            result = _read_report(args.directory / "report.json")
        else:
            client, db = connect_benchmark()
            result = run_benchmark(db, args.directory)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") == "completed" else 1
    except Exception as error:
        from enrichment.atlas_diagnostics import sanitized_error
        safe = {"category": "local", "code": "configuration_or_verification_failed"} if isinstance(error, ValueError) else sanitized_error(error)
        print(json.dumps({"status": "failed", "error": safe, "model_calls_added": 0}))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
