"""Read saved benchmark summaries and at most 25 live catalog rows.

No benchmark execution or model calls are exposed by the demo. Its startup
snapshot stays limited to the existing 36 enrichment listings.
"""

import copy
import json
import re


REPORT_ID = "benchmark-v1"
REPORT_COLLECTION = "catalog_harness_benchmark_reports"
OUTPUT_COLLECTION = "catalog_harness_benchmark_outputs"
PAGE_SIZE = 25
SUPPLIERS = ("northstar", "cedar", "harbor")
STATUSES = ("normalized", "quarantined")
TOP_FIELDS = ("benchmark_id", "label", "status", "seed", "sizes", "dataset_mode", "batch_size", "warmup_requests",
              "measured_requests", "model_calls_added", "started_at", "completed_at", "limitations")
DATASET_FIELDS = ("dataset_id", "requested_records", "status", "raw_count", "output_count", "seed_initial",
                  "seed_repeat", "processing", "integrity", "query", "aggregation")


def _database(repository):
    store = getattr(repository, "store", None)
    return store.normalizer.raw.database if store is not None else None


def _sanitize(report):
    if (not isinstance(report, dict) or report.get("benchmark_id") != REPORT_ID
            or not isinstance(report.get("datasets"), list) or len(report["datasets"]) > 3):
        raise ValueError("Saved benchmark report is invalid")
    result = {key: copy.deepcopy(report[key]) for key in TOP_FIELDS if key in report}
    result["datasets"] = []
    for dataset in report["datasets"]:
        if (not isinstance(dataset, dict) or dataset.get("requested_records") not in (1000, 10000, 50000)
                or not re.fullmatch(r"benchmark-v1-seed[0-9]+-n(?:1000|10000|50000)", dataset.get("dataset_id", ""))):
            raise ValueError("Saved benchmark dataset is invalid")
        result["datasets"].append({key: copy.deepcopy(dataset[key]) for key in DATASET_FIELDS if key in dataset})
    if len(json.dumps(result, allow_nan=False).encode()) > 262144:
        raise ValueError("Saved benchmark report exceeds its read limit")
    return result


def saved_report(repository):
    """One small report read on explicit request; no benchmark-record cache."""
    database = _database(repository)
    if database is not None:
        try:
            report = database[REPORT_COLLECTION].find_one({"_id": REPORT_ID}, {"_id": 0}, max_time_ms=3000)
            if report is not None:
                return {"source": {"kind": "atlas", "label": "Atlas saved benchmark report"},
                        "report": _sanitize(report), "browser_available": True}
        except Exception:
            pass
    path = repository.root / "docs" / "benchmark-results.json"
    try:
        if path.stat().st_size > 262144:
            raise ValueError("Saved benchmark report exceeds its read limit")
        report = _sanitize(json.loads(path.read_text(encoding="utf-8")))
        return {"source": {"kind": "local_fallback", "label": "Local saved benchmark report — Atlas report unavailable"},
                "report": report, "browser_available": False}
    except (OSError, ValueError, TypeError):
        return {"source": {"kind": "unavailable", "label": "No saved benchmark measurements available"},
                "report": None, "browser_available": False}


def catalog_page(repository, *, dataset_id, supplier="", status="", page="1"):
    """Filter/paginate in Atlas; input validation precedes any database read."""
    if (not isinstance(dataset_id, str) or not re.fullmatch(r"benchmark-v1-seed[0-9]+-n(?:1000|10000|50000)", dataset_id)
            or supplier not in ("",) + SUPPLIERS or status not in ("",) + STATUSES
            or not isinstance(page, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", page) or int(page) > 2000):
        raise ValueError("Invalid benchmark dataset, supplier, status, or page")
    database = _database(repository)
    if database is None:
        raise ValueError("Live benchmark browsing requires Atlas; saved measurements remain available")
    saved = saved_report(repository)
    permitted = {row["dataset_id"] for row in (saved["report"] or {}).get("datasets", [])}
    if saved["source"]["kind"] != "atlas" or dataset_id not in permitted:
        raise ValueError("Select a dataset from the saved Atlas benchmark report")
    query = {"dataset_id": dataset_id}
    if supplier:
        query["supplier_id"] = supplier
    if status:
        query["status"] = status
    collection = database[OUTPUT_COLLECTION]
    total = collection.count_documents(query, maxTimeMS=3000)
    offset = (int(page) - 1) * PAGE_SIZE
    cursor = (collection.find(query, {"_id": 0, "source_record_id": 1, "supplier_id": 1,
                                     "status": 1, "product": 1, "reason": 1})
              .sort("source_record_id", 1).skip(offset).limit(PAGE_SIZE).batch_size(PAGE_SIZE).max_time_ms(3000))
    return {"source": {"kind": "atlas", "label": "Live Atlas benchmark catalog"}, "dataset_id": dataset_id,
            "supplier": supplier, "status": status, "page": int(page), "page_size": PAGE_SIZE,
            "total_records": total, "has_next": offset + PAGE_SIZE < total,
            "records": list(cursor), "notice": "Synthetic deterministic outputs; no new inference. At most 25 records per page."}
