"""Check MongoDB connectivity using one temporary, uniquely identified document."""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError


COLLECTION = "_hackathon_setup_checks"


def main():
    # Avoid driver/debug logging that could expose connection details.
    logging.getLogger("pymongo").disabled = True
    logging.getLogger("dotenv.main").disabled = True
    try:
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except Exception:
        print("Could not load .env. Check its format locally; do not share credentials.")
        return 1

    uri = os.getenv("MONGODB_URI", "").strip()
    database = os.getenv("MONGODB_DATABASE", "").strip()
    missing = [name for name, value in (("MONGODB_URI", uri), ("MONGODB_DATABASE", database))
               if not value or value.startswith("YOUR_")]
    if missing:
        print("Configuration incomplete: " + ", ".join(missing) + ".")
        print("Copy .env.example to .env and fill in both values locally, then rerun.")
        print("No connection or database changes attempted.")
        return 0

    client = None
    collection = None
    attempted_insert = False
    cleanup_ok = True
    passed = False
    test_id = "hackathon-setup-" + uuid4().hex
    try:
        if not uri.startswith(("mongodb://", "mongodb+srv://")):
            print("Invalid MONGODB_URI format. Use the Atlas connection string locally.")
            return 1
        client = MongoClient(uri, serverSelectionTimeoutMS=10000,
                             connectTimeoutMS=10000, socketTimeoutMS=10000,
                             timeoutMS=20000)
        client.admin.command("ping")
        print("PASS: MongoDB ping.")
        collection = client[database][COLLECTION]
        document = {"_id": test_id, "purpose": "hackathon_setup_check",
                    "created_at": datetime.now(timezone.utc)}
        # Mark before sending: a timeout can occur after the server writes.
        attempted_insert = True
        collection.insert_one(document)
        print("PASS: Test document inserted into " + COLLECTION + ".")
        found = collection.find_one({"_id": test_id})
        if found is None or found.get("purpose") != document["purpose"]:
            print("FAIL: Test document could not be verified.")
        else:
            print("PASS: Test document read back.")
            passed = True
    except PyMongoError:
        print("FAIL: MongoDB operation failed. Check Atlas readiness, network access, "
              "database permissions, and credentials locally.")
    except Exception:
        print("FAIL: Check local configuration and database name. Error details are "
              "suppressed to protect credentials.")
    finally:
        if attempted_insert and collection is not None:
            try:
                collection.delete_one({"_id": test_id})
                if collection.find_one({"_id": test_id}, {"_id": 1}) is not None:
                    cleanup_ok = False
                    print("FAIL: Test document remains after cleanup.")
                else:
                    print("PASS: Test document absent after cleanup.")
            except Exception:
                cleanup_ok = False
                print("FAIL: Cleanup could not be confirmed. Remove only the document "
                      "with _id " + test_id + " from " + COLLECTION + " locally.")
            if not cleanup_ok:
                print("Cleanup target _id: " + test_id)
        if client is not None:
            client.close()

    if passed and cleanup_ok:
        print("PASS: MongoDB setup check complete.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
