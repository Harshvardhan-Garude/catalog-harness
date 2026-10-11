"""Only project-prefixed collections and bounded reads/writes; no destructive APIs."""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne


ROOT = Path(__file__).resolve().parent.parent
PREFIX = "catalog_harness_"
MAX_BATCH = 100
DATASET_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
SUPPLIER_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def batches(rows, size=MAX_BATCH):
    if type(size) is not int or not 1 <= size <= MAX_BATCH:
        raise ValueError("Batch size must be between 1 and 100")
    source = iter(rows)
    while True:
        batch = list(islice(source, size))
        if not batch:
            break
        yield batch


def read_jsonl(path):
    with Path(path).open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def connect():
    logging.getLogger("pymongo").disabled = True
    logging.getLogger("dotenv.main").disabled = True
    load_dotenv(ROOT / ".env")
    uri = os.getenv("MONGODB_URI", "").strip()
    database = os.getenv("MONGODB_DATABASE", "").strip()
    if not uri or not database or uri.startswith("YOUR_") or database.startswith("YOUR_"):
        raise ValueError("Set MONGODB_URI and MONGODB_DATABASE in local .env")
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        raise ValueError("Invalid MongoDB URI scheme")
    client = MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000,
                         socketTimeoutMS=20000, timeoutMS=30000, appname="catalog-harness")
    try:
        client.admin.command("ping")
        return client, Store(client[database])
    except Exception:
        client.close()
        raise


class Store:
    def __init__(self, db):
        self.raw = db[PREFIX + "raw"]
        self.suppliers = db[PREFIX + "suppliers"]
        self.results = db[PREFIX + "results"]
        self.runs = db[PREFIX + "runs"]
        self.policies = db[PREFIX + "policies"]
        self.datasets = db[PREFIX + "datasets"]

    def ensure_indexes(self):
        identity = [("dataset_id", ASCENDING), ("supplier_id", ASCENDING), ("source_record_id", ASCENDING)]
        self.raw.create_index(identity, unique=True, name="raw_identity")
        self.suppliers.create_index([("dataset_id", ASCENDING), ("supplier_id", ASCENDING)],
                                    unique=True, name="supplier_identity")
        self.results.create_index([("run_id", ASCENDING)] + identity, unique=True, name="result_identity")

    def dataset(self, dataset_id):
        result = self.datasets.find_one({"_id": dataset_id}, {"_id": 0})
        if result is None or result.get("seed_state") != "complete":
            raise ValueError("Dataset must be seeded completely first")
        return result

    def raw_records(self, dataset_id):
        # Cursor streaming/batches are for deterministic execution, never context.
        return self.raw.find({"dataset_id": dataset_id}, {"_id": 0}).sort(
            [("supplier_id", ASCENDING), ("source_record_id", ASCENDING)]).batch_size(MAX_BATCH)

    def raw_digest(self, dataset_id):
        digest = hashlib.sha256()
        count = 0
        for row in self.raw_records(dataset_id):
            digest.update((stable_json(row) + "\n").encode())
            count += 1
        return {"count": count, "sha256": digest.hexdigest()}

    def seed(self, public_directory):
        public_directory = Path(public_directory)
        manifest = json.loads((public_directory / "manifest.json").read_text())
        suppliers = json.loads((public_directory / "suppliers.json").read_text())
        if (not isinstance(manifest, dict)
                or not isinstance(manifest.get("dataset_id"), str)
                or not DATASET_ID.fullmatch(manifest["dataset_id"])
                or manifest.get("split") not in ("development", "final")
                or type(manifest.get("size")) is not int
                or not 1 <= manifest["size"] <= 1000000):
            raise ValueError("Invalid dataset manifest")
        dataset_id = manifest["dataset_id"]
        if not isinstance(suppliers, list) or not 1 <= len(suppliers) <= 20:
            raise ValueError("Dataset requires 1 to 20 suppliers")
        known_suppliers = set()
        public_metadata_fields = {"supplier_id", "display_name", "default_weight_unit", "unit_documentation"}
        for supplier in suppliers:
            if (not isinstance(supplier, dict) or not set(supplier) <= public_metadata_fields
                    or not isinstance(supplier.get("supplier_id"), str)
                    or not SUPPLIER_ID.fullmatch(supplier["supplier_id"])
                    or supplier["supplier_id"] in known_suppliers
                    or any(not isinstance(value, str) for value in supplier.values())):
                raise ValueError("Invalid or duplicate supplier metadata")
            known_suppliers.add(supplier["supplier_id"])
        # Retain hashes/identities for source verification, never catalog payloads.
        hashes, identities = [], set()
        for row in read_jsonl(public_directory / "records.jsonl"):
            if not isinstance(row, dict) or set(row) != {"dataset_id", "supplier_id", "source_record_id", "payload"}:
                raise ValueError("Raw schema contains unsupported fields")
            if (row["dataset_id"] != dataset_id or not isinstance(row["supplier_id"], str)
                    or row["supplier_id"] not in known_suppliers
                    or not isinstance(row["source_record_id"], str)
                    or not 1 <= len(row["source_record_id"]) <= 128
                    or not isinstance(row["payload"], dict)):
                raise ValueError("Raw record does not match dataset or suppliers")
            key = (row["supplier_id"], row["source_record_id"])
            if key in identities:
                raise ValueError("Duplicate source identity in input")
            identities.add(key)
            hashes.append(fingerprint(row))
        if len(hashes) != manifest["size"]:
            raise ValueError("Record count does not match manifest")
        source_hash = fingerprint({"rows": sorted(hashes), "suppliers": suppliers, "manifest": manifest})
        old = self.datasets.find_one({"_id": dataset_id})
        if old and old.get("source_hash") != source_hash:
            raise ValueError("Dataset identity collision; generate with a new seed")
        self.datasets.update_one({"_id": dataset_id}, {"$setOnInsert": {
            **manifest, "source_hash": source_hash}, "$set": {"seed_state": "started"}}, upsert=True)
        for batch in batches(read_jsonl(public_directory / "records.jsonl")):
            operations = []
            for row in batch:
                key = {k: row[k] for k in ("dataset_id", "supplier_id", "source_record_id")}
                operations.append(UpdateOne(key, {"$setOnInsert": row}, upsert=True))
            self.raw.bulk_write(operations, ordered=False)
        self.suppliers.bulk_write([UpdateOne({"dataset_id": dataset_id, "supplier_id": supplier["supplier_id"]},
                                  {"$setOnInsert": {"dataset_id": dataset_id, **supplier}}, upsert=True)
                                   for supplier in suppliers], ordered=False)
        # Detect changes or extraneous records without overwriting raw data.
        stored_hashes = sorted(fingerprint(row) for row in self.raw_records(dataset_id))
        stored_suppliers = list(self.suppliers.find({"dataset_id": dataset_id}, {"_id": 0, "dataset_id": 0}))
        if (stored_hashes != sorted(hashes) or sorted(stored_suppliers, key=lambda x: x["supplier_id"])
                != sorted(suppliers, key=lambda x: x["supplier_id"])):
            raise ValueError("Stored data differs from source; raw records were not overwritten")
        self.datasets.update_one({"_id": dataset_id}, {"$set": {"seed_state": "complete"}})
        return {"dataset_id": dataset_id, **self.raw_digest(dataset_id)}

    def supplier_metadata(self, dataset_id):
        return list(self.suppliers.find({"dataset_id": dataset_id}, {"_id": 0}).limit(20))

    def sample(self, dataset_id, supplier_id, limit=8, strategy="per_supplier"):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Sample limit must be between 1 and 50")
        if self.dataset(dataset_id)["split"] != "development":
            raise ValueError("Final holdout is not available to exploration tools")
        if strategy not in ("per_supplier", "reverse_per_supplier"):
            raise ValueError("Invalid sample strategy")
        direction = DESCENDING if strategy == "reverse_per_supplier" else ASCENDING
        return list(self.raw.find({"dataset_id": dataset_id, "supplier_id": supplier_id}, {"_id": 0})
                    .sort("source_record_id", direction).limit(limit))

    def summary(self, dataset_id):
        self.dataset(dataset_id)
        return list(self.raw.aggregate([
            {"$match": {"dataset_id": dataset_id}},
            {"$group": {"_id": "$supplier_id", "records": {"$sum": 1}}},
            {"$sort": {"_id": 1}}, {"$limit": 20}], maxTimeMS=10000))

    def save_policy(self, policy):
        value = policy.as_dict()
        policy_id = fingerprint(value)
        # JSON round-trip converts tuples to stable MongoDB array values.
        self.policies.update_one({"_id": policy_id}, {"$setOnInsert": json.loads(stable_json(value))}, upsert=True)
        return policy_id

    def save_results(self, run_id, outputs):
        for batch in batches(outputs):
            operations = []
            for output in batch:
                key = {"run_id": run_id, **{k: output[k] for k in ("dataset_id", "supplier_id", "source_record_id")}}
                operations.append(UpdateOne(key, {"$setOnInsert": {**output, "run_id": run_id}}, upsert=True))
            self.results.bulk_write(operations, ordered=False)

    def run_results(self, run_id):
        return self.results.find({"run_id": run_id}, {"_id": 0, "run_id": 0}).batch_size(MAX_BATCH)


class EventLog:
    """Allowlisted local run events, deliberately excluding raw data/config/errors."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def emit(self, event, run_id, dataset_id, **details):
        allowed = {"records", "raw_unchanged", "acceptance_passed", "plan_sha256", "policy_id"}
        if not set(details) <= allowed or event not in ("started", "transformed", "evaluated", "failed"):
            raise ValueError("Unsupported run event")
        value = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
                 "run_id": run_id, "dataset_id": dataset_id, **details}
        with (self.directory / "events.jsonl").open("a") as stream:
            stream.write(stable_json(value) + "\n")
