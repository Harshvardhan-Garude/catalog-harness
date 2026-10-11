"""Evaluate the two already frozen policies on final data once, with safe resume.

This phase has no policy proposal or selection path. Completed HTTP responses
are replayed through the unchanged provider; missing persistence is repaired
without asking the model again. Final answers stay in the evaluator process.
"""

import argparse
import copy
import fcntl
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pymongo.errors import PyMongoError

from catalog_harness.openai_http import OpenAIError, configuration
from catalog_harness.storage import ROOT, batches, fingerprint, stable_json
from catalog_harness.storage import connect
from catalog_harness.transforms import transform_record, validate_plan
from enrichment_eval.fixtures import load_labels, load_public
from enrichment_eval.scoring import evaluate

from .atlas_diagnostics import sanitized_error
from .budget import write_json
from .freeze import manifest_digest, verify
from .provider import BASELINE_POLICY, enrich_batch, validate_policy
from .storage import EnrichmentStore


FIXTURES = ROOT / "data/enrichment-v1"
ORIGINAL_ARTIFACTS = ROOT / "artifacts/enrichment-v1"
ARTIFACTS = ROOT / "artifacts/enrichment-v1-final"
DOCUMENTED_POLICY = ROOT / "docs/semantic-v1-policy.json"
DATASET_ID = "semantic-v1-final"
VARIANTS = ("baseline", "candidate")
VERSION = "semantic-v1-final-evaluation-1"
TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _read(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _make_budget(directory, api_key):
    # Keeping this import lazy lets the report command remain read-only.
    from .spending import FinalBudget
    return FinalBudget(directory, api_key)


def _contract(fixtures_root, original_artifacts, documented_policy):
    manifest = verify(fixtures_root, original_artifacts)
    prior = _read(Path(original_artifacts) / "report.json")
    policies = prior.get("policies", {})
    if (prior.get("status") != "completed" or not prior.get("decision", {}).get("accepted")
            or prior.get("freeze_sha256") != manifest_digest(manifest)
            or policies.get("baseline") != BASELINE_POLICY
            or manifest.get("baseline_policy") != BASELINE_POLICY
            or manifest.get("split_counts", {}).get("final") != 24):
        raise ValueError("The completed frozen comparison is required")
    candidate = validate_policy(policies.get("candidate"))
    if candidate != _read(documented_policy):
        raise ValueError("Selected and documented policy differ")
    selected = {variant: copy.deepcopy(policies[variant]) for variant in VARIANTS}
    plan = validate_plan(_read(ROOT / "fixtures/plan.json"))
    prior_raw = copy.deepcopy(prior.get("prior_raw", {}))
    for source in prior["sources"].values():
        prior_raw[source["dataset_id"]] = copy.deepcopy(source["raw_digest"])
    contract = {"version": VERSION, "prior_experiment_id": prior["experiment_id"],
                "original_report_sha256": fingerprint(prior),
                "freeze_sha256": manifest_digest(manifest),
                "policy_fingerprints": {variant: fingerprint(policy) for variant, policy in selected.items()},
                "normalizer_plan_sha256": fingerprint(plan), "prior_raw": prior_raw,
                "final_records": 24, "batch_size": 4, "max_new_model_calls": 12,
                "max_output_tokens": 2048, "retuning_permitted": False}
    return contract, manifest, selected, plan


def _state(directory, contract):
    path = Path(directory) / "state.json"
    if path.exists():
        state = _read(path)
        if state.get("contract") != contract:
            raise ValueError("Final phase contract changed; refuse resume")
        if set(state.get("run_ids", {})) != set(VARIANTS):
            raise ValueError("Final phase run identities are incomplete")
        return state
    state = {"phase_id": uuid4().hex, "contract": contract,
             "started_at": datetime.now(timezone.utc).isoformat(),
             "run_ids": {variant: uuid4().hex for variant in VARIANTS},
             "normalization_run_id": uuid4().hex, "source": None,
             "completed_batches": {variant: [] for variant in VARIANTS},
             "runs": {}, "status": "preflight", "active_stage": "connect_and_ping"}
    write_json(path, state)
    return state


def _save_state(directory, state):
    write_json(Path(directory) / "state.json", state)


def _assert_prior(normalizer, contract):
    for dataset_id, snapshot in contract["prior_raw"].items():
        if normalizer.raw_digest(dataset_id) != snapshot:
            raise ValueError("Existing development or validation raw data changed")


def _write_immutable(path, content):
    path = Path(path)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError("Existing final source artifact differs")
        return
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        handle.write(content)


def prepare_final_source(normalizer, products, plan, state, directory):
    """Seed final raw records and persist deterministic copies under stable IDs."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = []
    for product in products:
        fields = product["fields"]
        records.append({"dataset_id": DATASET_ID, "supplier_id": "northstar",
            "source_record_id": product["source_record_id"], "payload": {
                "sku": product["normalized_product"]["product_id"], "title": fields["name"],
                "weight": product["normalized_product"]["weight_g"], "weight_unit": "g",
                "description": fields["description"], "supplier_notes": fields["supplier_notes"]}})
    documents = {
        "manifest.json": json.dumps({"dataset_id": DATASET_ID, "split": "final", "semantic_split": "final",
                                      "size": len(products)}, indent=2) + "\n",
        "suppliers.json": json.dumps([{"supplier_id": "northstar",
            "unit_documentation": "Each source record explicitly declares grams."}], indent=2) + "\n",
        "records.jsonl": "".join(stable_json(row) + "\n" for row in records),
    }
    for name, content in documents.items():
        _write_immutable(directory / name, content)
    normalizer.ensure_indexes()
    seeded, repeated = normalizer.seed(directory), normalizer.seed(directory)
    if seeded != repeated or normalizer.dataset(DATASET_ID)["split"] != "final":
        raise ValueError("Final raw seed is not repeatable or has wrong split")
    before = normalizer.raw_digest(DATASET_ID)
    if state["source"] and state["source"]["raw_digest"] != before:
        raise ValueError("Final raw snapshot differs from reserved phase")
    stored_raw = list(normalizer.raw_records(DATASET_ID))
    if sorted(map(stable_json, stored_raw)) != sorted(map(stable_json, records)):
        raise ValueError("Final source persistence differs")
    normalized = [transform_record(row, plan, {}) for row in stored_raw]
    expected = {row["source_record_id"]: row["normalized_product"] for row in products}
    if (len(normalized) != len(products) or len({row["source_record_id"] for row in normalized}) != len(products)
            or any(row["status"] != "normalized" or row["product"] != expected.get(row["source_record_id"])
                   for row in normalized)):
        raise ValueError("Final deterministic normalization differs")
    run_id = state["normalization_run_id"]
    immutable = {"dataset_id": DATASET_ID, "split": "final", "phase_id": state["phase_id"],
                 "kind": "hand_authored_final_source_normalization",
                 "plan_sha256": fingerprint(plan), "plan": plan}
    normalizer.runs.update_one({"_id": run_id}, {"$setOnInsert": immutable}, upsert=True)
    saved = normalizer.runs.find_one({"_id": run_id})
    if not saved or any(saved.get(key) != value for key, value in immutable.items()):
        raise ValueError("Final normalization provenance differs")
    normalizer.save_results(run_id, normalized)
    reread = list(normalizer.run_results(run_id))
    if sorted(map(stable_json, reread)) != sorted(map(stable_json, normalized)):
        raise ValueError("Final normalization persistence differs")
    if normalizer.raw_digest(DATASET_ID) != before:
        raise ValueError("Final normalization changed raw records")
    normalizer.runs.update_one({"_id": run_id}, {"$set": {
        "status": "transformed", "raw_unchanged": True, "raw_digest": before}})
    source = {"dataset_id": DATASET_ID, "normalization_run_id": run_id, "raw_digest": before,
              "seed_repeatable": True, "normalization_persistence_verified": True}
    write_json(directory / "verification.json", source)
    return source


def _ensure_run(store, state, variant, policy):
    policy_id = fingerprint(policy)
    store.policies.update_one({"_id": policy_id}, {"$setOnInsert": policy}, upsert=True)
    saved_policy = store.policies.find_one({"_id": policy_id})
    if not saved_policy or {key: value for key, value in saved_policy.items() if key != "_id"} != policy:
        raise ValueError("Final policy persistence differs")
    run_id = state["run_ids"][variant]
    immutable = {"experiment_id": state["phase_id"], "prior_experiment_id": state["contract"]["prior_experiment_id"],
                 "variant": variant, "split": "final", "policy_id": policy_id, "fixture_version": "semantic-v1",
                 "freeze_sha256": state["contract"]["freeze_sha256"], "source": state["source"],
                 "kind": "synthetic_semantic_final_evaluation"}
    store.runs.update_one({"_id": run_id}, {"$setOnInsert": {**immutable, "status": "started"}}, upsert=True)
    saved = store.runs.find_one({"_id": run_id})
    if not saved or any(saved.get(key) != value for key, value in immutable.items()):
        raise ValueError("Final run provenance differs")
    return run_id


def _persisted_rows(run_id, outputs, details, checks, offset):
    """Mirror the storage contract, including original model rows and checks."""
    expected = []
    for index, row in enumerate(outputs):
        expected.append({"run_id": run_id, "output_index": offset + index,
            "source_record_id": row["source_record_id"], "output": row,
            "evidence_checks": checks.get(row["source_record_id"], []),
            "model": details["model"], "usage_batch": details["usage"],
            "original_model_output": details["model_outputs"][index],
            "original_evidence_checks": details["original_evidence_checks_by_output"][index]})
    return expected


def _same_completed_batch(saved, recomputed):
    # The frozen review behavior iterates a set of invalid fields. Across
    # processes that can change only the order of appended review notes.
    # Preserve the first persisted representation while checking every value;
    # original model rows are never reordered or normalized here.
    def canonical(document):
        result = copy.deepcopy(document)
        for row in result["outputs"]:
            row["review"] = sorted(row["review"], key=stable_json)
        return stable_json(result)
    return canonical(saved) == canonical(recomputed)


def run_variant(variant, products, taxonomy, policy, budget, store, state, directory, fixtures_root):
    if variant not in VARIANTS or len(products) != 24:
        raise ValueError("Final evaluation requires the fixed final batch")
    run_id = _ensure_run(store, state, variant, policy)
    folder = Path(directory) / (variant + "_final")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    outputs, checks, expected_persistence = [], {}, []
    stage_ids = []
    for ordinal, batch in enumerate(batches(products, 4), 1):
        stage = "{}_final_{:02d}".format(variant, ordinal)
        stage_ids.append(stage)
        state["active_stage"] = stage
        _save_state(directory, state)
        # The guard returns a durable completed response on resume. Reusing the
        # unchanged provider revalidates every plan/citation without a new call.
        rows, details, batch_checks = enrich_batch(batch, taxonomy, policy, budget, stage)
        document = {"stage": stage, "outputs": rows, "details": details, "evidence_checks": batch_checks}
        batch_path = folder / ("batch-{:02d}.json".format(ordinal))
        if batch_path.exists():
            saved = _read(batch_path)
            if not _same_completed_batch(saved, document):
                raise ValueError("Completed final batch differs from cached response")
            rows, details, batch_checks = saved["outputs"], saved["details"], saved["evidence_checks"]
        else:
            write_json(batch_path, document)
        store.save_batch(run_id, rows, details, batch_checks, offset=len(outputs))
        expected_persistence.extend(_persisted_rows(run_id, rows, details, batch_checks, len(outputs)))
        outputs.extend(rows)
        for identity, issues in batch_checks.items():
            checks.setdefault(identity, []).extend(issues)
        if stage not in state["completed_batches"][variant]:
            state["completed_batches"][variant].append(stage)
        _save_state(directory, state)
        print("{} persisted".format(stage), flush=True)
    persisted = store.read_results(run_id)
    if stable_json(persisted) != stable_json(expected_persistence):
        raise ValueError("Final Atlas output, original response, or evidence persistence differs")
    persisted_checks = {}
    for row in persisted:
        merged = persisted_checks.setdefault(row["source_record_id"], [])
        for issue in row["evidence_checks"]:
            if issue not in merged:
                merged.append(issue)
    if checks != persisted_checks:
        raise ValueError("Final Atlas evidence persistence differs")
    # Final labels are loaded only here and never supplied to provider/budget.
    labels = load_labels(fixtures_root, "final")
    if {row["source_record_id"] for row in labels} != {row["source_record_id"] for row in products}:
        raise ValueError("Final evaluation identities differ")
    report = evaluate(labels, outputs, split="final", evidence_checks=checks)
    reread = evaluate(labels, [row["output"] for row in persisted], split="final", evidence_checks=persisted_checks)
    if report != reread or report["representative_failures"] or report["development_failures"]:
        raise ValueError("Final aggregate evaluation differs")
    if store.normalizer.raw_digest(DATASET_ID) != state["source"]["raw_digest"]:
        raise ValueError("Final raw records changed")
    calls = [call for call in budget.ledger["calls"] if call["stage"] in stage_ids]
    if len(calls) != 6 or any(call["status"] != "completed" for call in calls):
        raise ValueError("Final completed-call accounting differs")
    usage = {key: sum(call["usage"][key] for call in calls) for key in TOKEN_KEYS}
    report.update(run_id=run_id, kind="synthetic_semantic_final_evaluation", policy_version=policy["version"],
                  policy_fingerprint=fingerprint(policy), model_calls=6, inference_products=24,
                  token_usage=usage, average_tokens_per_product={key: value / 24 for key, value in usage.items()},
                  atlas_persistence_verified=True, raw_unchanged=True)
    store.finish_run(run_id, report, calls)
    saved = store.runs.find_one({"_id": run_id})
    if not saved or saved.get("status") != "evaluated" or saved.get("evaluation") != report:
        raise ValueError("Final Atlas evaluation persistence differs")
    write_json(folder / "evaluation.json", report)
    state["runs"][variant] = report
    _save_state(directory, state)
    return report


def compare_final(baseline, candidate):
    """Describe aggregate final changes; never select or revise a policy."""
    if baseline.get("split") != "final" or candidate.get("split") != "final":
        raise ValueError("Final comparison requires final aggregate reports")
    for key in ("fixture_version", "scoring_version", "evaluation_identity_sha256", "evaluation_labels_sha256"):
        if baseline.get(key) != candidate.get(key):
            raise ValueError("Final comparison contracts differ")
    old, new = baseline["overall"], candidate["overall"]
    if (old["total_expected"] != 24 or new["total_expected"] != 24
            or old["total_field_decisions"] != 72 or new["total_field_decisions"] != 72):
        raise ValueError("Final comparison denominators differ")
    delta = new["correct_field_decisions"] - old["correct_field_decisions"]
    return {"kind": "final_holdout_comparison_only", "policy_selection_performed": False,
            "retuning_permitted": False, "outcome": "gain" if delta > 0 else "regression" if delta < 0 else "unchanged",
            "correct_field_decisions_delta": delta,
            "field_accuracy_delta": candidate["field_accuracy"] - baseline["field_accuracy"],
            "unsupported_assertions_delta": new["unsupported_assertions"] - old["unsupported_assertions"],
            "unnecessary_abstentions_delta": new["unnecessary_abstentions"] - old["unnecessary_abstentions"],
            "limits": "One synthetic final comparison; no repeated trials or statistical generalization claim."}


def _report(state, budget=None):
    result = {"version": VERSION, "phase_id": state["phase_id"], "kind": "synthetic_semantic_final_evaluation",
              "prior_experiment_id": state["contract"]["prior_experiment_id"],
              "freeze_sha256": state["contract"]["freeze_sha256"],
              "policy_fingerprints": state["contract"]["policy_fingerprints"],
              "started_at": state["started_at"], "status": state["status"],
              "active_stage": state["active_stage"], "source": state["source"], "runs": state["runs"],
              "final_holdout_evaluated": bool(state["runs"]), "both_policies_evaluated": len(state["runs"]) == 2,
              "retuning_permitted": False, "usage": budget.summary() if budget else state.get("usage")}
    for key in ("comparison", "error", "finished_at", "atlas_persistence_verified", "raw_unchanged"):
        if key in state:
            result[key] = state[key]
    return result


def execute(fixtures_root=FIXTURES, original_artifacts=ORIGINAL_ARTIFACTS, artifact_dir=ARTIFACTS,
            documented_policy=DOCUMENTED_POLICY):
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact_dir.chmod(0o700)
    lock = (artifact_dir / "evaluation.lock").open("a")
    client = budget = store = state = None
    completed_read_only = False
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        contract, manifest, policies, plan = _contract(fixtures_root, original_artifacts, documented_policy)
        state = _state(artifact_dir, contract)
        if state["status"] == "completed":
            report = _read(artifact_dir / "report.json")
            if report != _report(state):
                raise ValueError("Completed final report differs")
            completed_read_only = True
            print(json.dumps(report, indent=2, sort_keys=True), flush=True)
            return 0
        api_key, missing = configuration()
        if missing:
            print("Missing: " + ", ".join(missing))
            return 1
        products = load_public(fixtures_root, "final")
        if len(products) != 24 or len({row["source_record_id"] for row in products}) != 24:
            raise ValueError("Frozen final record identities differ")
        taxonomy = _read(ROOT / "enrichment/taxonomy.json")
        state.update(status="preflight", active_stage="connect_and_ping")
        state.pop("error", None)
        _save_state(artifact_dir, state)
        client, normalizer = connect()
        store = EnrichmentStore(normalizer)
        store.ensure_indexes()
        _assert_prior(normalizer, contract)
        state["active_stage"] = "prepare_final_source"
        _save_state(artifact_dir, state)
        state["source"] = prepare_final_source(normalizer, products, plan, state, artifact_dir / "normalizer-sources")
        _save_state(artifact_dir, state)
        phase = {"kind": "synthetic_semantic_final_evaluation", "contract": contract, "freeze": manifest,
                 "policies": policies, "source": state["source"]}
        store.experiments.update_one({"_id": state["phase_id"]}, {"$setOnInsert": phase}, upsert=True)
        saved = store.experiments.find_one({"_id": state["phase_id"]})
        if not saved or any(saved.get(key) != value for key, value in phase.items()):
            raise ValueError("Final phase provenance persistence differs")
        verify(fixtures_root, original_artifacts)
        # Atlas/configuration failures above reserve zero model calls. The
        # spending guard owns all HTTP accounting and completed-response replay.
        budget = _make_budget(artifact_dir, api_key)
        state["status"] = "running"
        _save_state(artifact_dir, state)
        for variant in VARIANTS:
            run_variant(variant, products, taxonomy, policies[variant], budget, store, state, artifact_dir, fixtures_root)
            _assert_prior(normalizer, contract)
        state["comparison"] = compare_final(state["runs"]["baseline"], state["runs"]["candidate"])
        if normalizer.raw_digest(DATASET_ID) != state["source"]["raw_digest"]:
            raise ValueError("Final raw integrity changed")
        verify(fixtures_root, original_artifacts)
        state.update(status="completed", active_stage="completed", raw_unchanged=True, atlas_persistence_verified=True)
    except OpenAIError as error:
        if state is not None:
            state.update(status="blocked", error={"category": error.category, "code": error.code})
        else:
            print("FAIL {}/{}".format(error.category, error.code))
    except PyMongoError as error:
        if state is not None:
            state.update(status="blocked", error=sanitized_error(error))
        else:
            print("FAIL atlas/operation_failed")
    except Exception:
        if state is not None:
            state.update(status="blocked", error={"category": "local", "code": "verification_failed"})
        else:
            print("FAIL local/precondition_failed")
    finally:
        if state is not None and state["status"] != "completed":
            state.pop("atlas_persistence_verified", None)
            state.pop("raw_unchanged", None)
        if not completed_read_only and state is not None and state["status"] in ("completed", "blocked"):
            if budget:
                state["usage"] = budget.summary()
            state["finished_at"] = datetime.now(timezone.utc).isoformat()
            report = _report(state)
            if store and state["source"]:
                try:
                    store.experiments.update_one({"_id": state["phase_id"]}, {"$set": {"report": report}})
                    saved = store.experiments.find_one({"_id": state["phase_id"]})
                    if not saved or saved.get("report") != report:
                        raise ValueError("Final report persistence differs")
                except Exception:
                    state.update(status="blocked", atlas_persistence_verified=False,
                                 error={"category": "atlas", "code": "report_persistence_failed"})
                    report = _report(state)
            _save_state(artifact_dir, state)
            write_json(artifact_dir / "report.json", report)
        if budget:
            budget.close()
        if client:
            client.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        logging.disable(previous_logging)
    if state is not None:
        print(json.dumps(_report(state), indent=2, sort_keys=True), flush=True)
    return 0 if state is not None and state["status"] == "completed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "report"), nargs="?", default="run")
    args = parser.parse_args(argv)
    if args.command == "run":
        try:
            return execute()
        except OpenAIError as error:
            print("FAIL {}/{}".format(error.category, error.code))
        except Exception:
            print("FAIL local/precondition_failed")
        return 1
    try:
        print(json.dumps(_read(ARTIFACTS / "report.json"), indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError):
        print("FAIL local/report_unavailable")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
