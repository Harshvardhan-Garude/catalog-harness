"""One frozen, bounded synthetic experiment; final holdout has no live path."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pymongo.errors import PyMongoError

from catalog_harness.openai_http import OpenAIError, configuration
from catalog_harness.storage import ROOT, batches, connect, stable_json
from enrichment_eval.fixtures import load_labels, load_public
from enrichment_eval.scoring import decide_candidate, evaluate

from .budget import ExperimentBudget, write_json
from .freeze import manifest_digest, prepare, verify
from .provider import BASELINE_POLICY, enrich_batch, propose_policy
from .storage import EnrichmentStore

FIXTURES = ROOT / "data/enrichment-v1"
ARTIFACTS = ROOT / "artifacts/enrichment-v1"


def perfect(report):
    counts = report["overall"]
    return (counts["correct_field_decisions"] == counts["total_field_decisions"]
            and not any(counts[key] for key in (
                "unsupported_assertions", "evidence_issue_count", "missing_outputs",
                "duplicate_outputs", "unexpected_outputs", "unnecessary_abstentions")))


def proposal_failures(development_report):
    """Strip all evaluator context except bounded, answer-free failure codes."""
    if development_report.get("split") != "development":
        raise ValueError("Proposal diagnosis is development-only")
    grouped = {}
    for failure in development_report["development_failures"]:
        source = failure["source_record_id"]
        grouped.setdefault(source, []).append({"field": failure["field"], "code": failure["category"]})
    return [{"source_record_id": source, "issues": issues[:12]}
            for source, issues in list(grouped.items())[:6]]


def raw_snapshots(normalizer):
    """Protect existing v1 development data without inspecting final holdout."""
    datasets = normalizer.datasets.find({"split": "development", "seed_state": "complete"}, {"_id": 1}).limit(100)
    identities = [row["_id"] for row in datasets if not row["_id"].startswith("semantic-v1-")]
    return {identity: normalizer.raw_digest(identity) for identity in sorted(identities)}


def assert_raw_unchanged(store, sources, prior):
    for source in sources.values():
        if store.normalizer.raw_digest(source["dataset_id"]) != source["raw_digest"]:
            raise ValueError("Semantic source integrity check failed")
    if raw_snapshots(store.normalizer) != prior:
        raise ValueError("Existing raw data integrity check failed")


def run_stage(*, experiment_id, variant, split, policy, products, labels, taxonomy,
              budget, store, source, freeze_hash, directory):
    if split not in ("development", "validation") or variant not in ("baseline", "candidate"):
        raise ValueError("Invalid live experiment stage")
    if {row["source_record_id"] for row in products} != {row["source_record_id"] for row in labels}:
        raise ValueError("Evaluation identities differ")
    stage = variant + "_" + split
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = store.start_run(experiment_id, variant, split, policy, source, freeze_hash)
    outputs, checks = [], {}
    first_call = len(budget.ledger["calls"])
    try:
        for ordinal, batch in enumerate(batches(products, 4), 1):
            rows, details, batch_checks = enrich_batch(batch, taxonomy, policy, budget, stage)
            write_json(directory / ("batch-{:02d}.json".format(ordinal)),
                       {"outputs": rows, "details": details, "evidence_checks": batch_checks})
            store.save_batch(run_id, rows, details, batch_checks, offset=len(outputs))
            outputs.extend(rows)
            for identity, issues in batch_checks.items():
                checks.setdefault(identity, []).extend(issues)
            print("{} batch {}/{} persisted".format(stage, ordinal, (len(products) + 3) // 4), flush=True)
        report = evaluate(labels, outputs, split=split, evidence_checks=checks)
        persisted = store.read_results(run_id)
        # Compare every occurrence, retaining missing/duplicate predictions in scoring.
        if stable_json([row["output"] for row in persisted]) != stable_json(outputs):
            raise ValueError("Atlas output persistence differs")
        persisted_checks = {}
        for row in persisted:
            merged = persisted_checks.setdefault(row["source_record_id"], [])
            for issue in row["evidence_checks"]:
                if issue not in merged:
                    merged.append(issue)
        if persisted_checks != checks:
            raise ValueError("Atlas evidence persistence differs")
        reread = evaluate(labels, [row["output"] for row in persisted], split=split,
                          evidence_checks=persisted_checks)
        if reread != report:
            raise ValueError("Atlas evaluation differs")
        if store.normalizer.raw_digest(source["dataset_id"]) != source["raw_digest"]:
            raise ValueError("Raw integrity differs")
        calls = budget.ledger["calls"][first_call:]
        report.update(run_id=run_id, kind="synthetic_semantic_enrichment", policy_version=policy["version"],
                      atlas_persistence_verified=True, raw_unchanged=True,
                      model_calls=len(calls), inference_products=len(products),
                      token_usage={key: sum((call["usage"] or {}).get(key, 0) for call in calls)
                                   for key in ("input_tokens", "output_tokens", "total_tokens")})
        report["average_tokens_per_product"] = {
            key: value / len(products) for key, value in report["token_usage"].items()}
        store.finish_run(run_id, report, calls)
        saved = store.runs.find_one({"_id": run_id}, {"evaluation": 1, "status": 1})
        if not saved or saved["status"] != "evaluated" or saved["evaluation"] != report:
            raise ValueError("Atlas run evaluation persistence differs")
        write_json(directory / "evaluation.json", report)
        return report, outputs
    except Exception:
        # Failure reporting must not replace the original sanitized failure.
        try:
            store.fail_run(run_id)
        except Exception:
            pass
        raise


def execute(fixtures_root=FIXTURES, artifact_dir=ARTIFACTS):
    """Exactly one experiment per durable directory; no reset/resume option."""
    fixtures_root, artifact_dir = Path(fixtures_root), Path(artifact_dir)
    manifest = verify(fixtures_root, artifact_dir)
    if (artifact_dir / "calls.json").exists():
        raise ValueError("Existing call ledger; no automatic restart or retry")
    api_key, missing = configuration()
    if missing:
        print("Missing: " + ", ".join(missing))
        return 1
    taxonomy = json.loads((ROOT / "enrichment/taxonomy.json").read_text())
    # Only these splits are loaded for live orchestration; final keys stay unused.
    products = {split: load_public(fixtures_root, split) for split in ("development", "validation")}
    labels = {split: load_labels(fixtures_root, split) for split in products}
    experiment_id = uuid4().hex
    result = {"experiment_id": experiment_id, "kind": "synthetic_semantic_experiment",
              "fixture_version": "semantic-v1", "freeze_sha256": manifest_digest(manifest),
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "preflight",
              "final_holdout_evaluated": False, "policies": {"baseline": dict(BASELINE_POLICY)},
              "runs": {}, "decision": {"accepted": False, "reason": "experiment_incomplete"}}
    client = None
    budget = None
    store = None
    experiment_saved = False
    sources, prior = {}, {}
    error = None
    try:
        client, normalizer = connect()
        store = EnrichmentStore(normalizer)
        store.ensure_indexes()
        prior = raw_snapshots(normalizer)
        for split in products:
            sources[split] = store.prepare_sources(products[split], split, artifact_dir / "normalizer-sources")
        result.update(sources=sources, prior_raw=prior)
        # Freeze provenance is persisted and read before the first live request.
        store.experiments.insert_one({"_id": experiment_id, "freeze": manifest, "report": result})
        experiment_saved = True
        frozen = store.experiments.find_one({"_id": experiment_id}, {"freeze": 1})
        if not frozen or frozen["freeze"] != manifest:
            raise ValueError("Atlas freeze persistence differs")
        assert_raw_unchanged(store, sources, prior)
        verify(fixtures_root, artifact_dir)
        budget = ExperimentBudget(artifact_dir, api_key, max_calls=20)
        result["status"] = "running"
        write_json(artifact_dir / "report.json", result)

        def stage(variant, split, policy):
            report, predictions = run_stage(experiment_id=experiment_id, variant=variant, split=split,
                policy=policy, products=products[split], labels=labels[split], taxonomy=taxonomy,
                budget=budget, store=store, source=sources[split], freeze_hash=result["freeze_sha256"],
                directory=artifact_dir / (variant + "_" + split))
            result["runs"][variant + "_" + split] = report
            write_json(artifact_dir / "report.json", result)
            return report, predictions

        baseline_dev, baseline_outputs = stage("baseline", "development", BASELINE_POLICY)
        # Decide whether to propose using development only, before reading any
        # validation predictions. All keys are retained by this evaluator process.
        candidate = None
        if perfect(baseline_dev):
            result["decision"] = {"accepted": False, "reason": "perfect_development_baseline_skip_revision"}
        else:
            failures = proposal_failures(baseline_dev)
            if failures:
                candidate, details = propose_policy(products["development"], baseline_outputs, failures,
                                                     BASELINE_POLICY, taxonomy, budget)
                result["policies"]["candidate"] = candidate
                write_json(artifact_dir / "policy-proposal.json", {"policy": candidate, "details": details})
            else:
                result["decision"] = {"accepted": False, "reason": "no_diagnosable_development_failure"}
        baseline_val, _ = stage("baseline", "validation", BASELINE_POLICY)
        if candidate is not None:
            stage("candidate", "development", candidate)
            candidate_val, _ = stage("candidate", "validation", candidate)
            result["decision"] = decide_candidate(baseline_val, candidate_val)
        assert_raw_unchanged(store, sources, prior)
        verify(fixtures_root, artifact_dir)
        result.update(status="completed", raw_unchanged=True, atlas_persistence_verified=True)
    except OpenAIError as failure:
        error = {"category": failure.category, "code": failure.code}
    except PyMongoError:
        error = {"category": "atlas", "code": "operation_failed"}
    except Exception:
        error = {"category": "local", "code": "verification_failed"}
    finally:
        if error:
            result.update(status="blocked", error=error)
            # No retry, revision, or further live model call after any failure.
            if budget:
                budget.ledger["stopped"] = True
                write_json(budget.path, budget.ledger)
        if budget:
            result["usage"] = budget.summary()
            budget.close()
        else:
            result["usage"] = {"calls": 0, "tokens": None, "unknown_usage_calls": 0}
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(artifact_dir / "report.json", result)
        if store and experiment_saved:
            try:
                store.experiments.update_one({"_id": experiment_id}, {"$set": {"report": result}})
                saved = store.experiments.find_one({"_id": experiment_id}, {"report": 1})
                if not saved or saved["report"] != result:
                    raise ValueError("Experiment persistence differs")
            except Exception:
                if error:
                    result["prior_error"] = error
                result.update(status="blocked", error={"category": "atlas", "code": "report_persistence_failed"},
                              atlas_persistence_verified=False)
                write_json(artifact_dir / "report.json", result)
        if client:
            client.close()
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    return 0 if result["status"] == "completed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "report"))
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            manifest = prepare(FIXTURES, ARTIFACTS)
            print("Frozen semantic-v1: 24 development, 12 validation, 24 unused final; sha256=" + manifest_digest(manifest))
            return 0
        if args.command == "report":
            print((ARTIFACTS / "report.json").read_text())
            return 0
        return execute()
    except OpenAIError as error:
        print("FAIL {}/{}".format(error.category, error.code))
    except (ValueError, OSError):
        print("FAIL local/precondition_failed; verify freeze files and existing call ledger; no request retried")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
