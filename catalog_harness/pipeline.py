"""Execute validated plans on streamed copies; evaluation is a separate step."""

from datetime import datetime, timezone
from uuid import uuid4

from .policy import HarnessPolicy
from .storage import fingerprint
from .transforms import transform_record, validate_plan


def run_fixture(store, dataset_id, plan, event_log, final_report=False):
    return run_plan(store, dataset_id, plan, event_log, kind="hand_authored_fixture",
                    policy=HarnessPolicy(), final_report=final_report)


def run_plan(store, dataset_id, plan, event_log, *, kind, policy, final_report=False, model_requests=None):
    validate_plan(plan)
    if kind not in ("hand_authored_fixture", "ai_development_baseline"):
        raise ValueError("Unsupported run kind")
    dataset = store.dataset(dataset_id)
    if kind == "ai_development_baseline" and dataset["split"] != "development":
        raise ValueError("AI baseline is development-only")
    if dataset["split"] == "final" and not final_report:
        raise ValueError("Final holdout requires the separate final-report command")
    if final_report and dataset["split"] != "final":
        raise ValueError("Final report requires a final dataset")
    policy_id = store.save_policy(policy)
    run_id = uuid4().hex
    before = store.raw_digest(dataset_id)
    plan_hash = fingerprint(plan)
    store.runs.insert_one({"_id": run_id, "dataset_id": dataset_id, "split": dataset["split"],
                           "kind": kind, "plan_sha256": plan_hash, "plan": plan,
                           "model_requests": model_requests or [],
                           "policy_id": policy_id, "status": "started",
                           "started_at": datetime.now(timezone.utc)})
    event_log.emit("started", run_id, dataset_id, plan_sha256=plan_hash, policy_id=policy_id)
    try:
        metadata = {m["supplier_id"]: m for m in store.supplier_metadata(dataset_id)}
        outputs = (transform_record(row, plan, metadata.get(row["supplier_id"], {}))
                   for row in store.raw_records(dataset_id))
        store.save_results(run_id, outputs)
        after = store.raw_digest(dataset_id)
        if before != after:
            raise ValueError("Raw data integrity check failed")
        store.runs.update_one({"_id": run_id}, {"$set": {"status": "transformed", "raw_unchanged": True,
                              "raw_digest": after, "transformed_at": datetime.now(timezone.utc)}})
        event_log.emit("transformed", run_id, dataset_id, records=after["count"], raw_unchanged=True)
        return run_id
    except Exception:
        store.runs.update_one({"_id": run_id}, {"$set": {"status": "failed"}})
        event_log.emit("failed", run_id, dataset_id)
        raise
