"""Small CLI. Errors never interpolate server messages, URIs, or secret values."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .pipeline import run_fixture, run_plan
from .storage import EventLog, ROOT, connect, read_jsonl
from .transforms import validate_plan


def parse_plan(path):
    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate plan JSON key")
            value[key] = item
        return value
    path = Path(path)
    if path.stat().st_size > 32768:
        raise ValueError("Plan exceeds 32 KiB limit")
    return validate_plan(json.loads(path.read_text(), object_pairs_hook=unique_keys))


def parser():
    result = argparse.ArgumentParser(description="Catalog Harness: separate fixture and development AI runs")
    result.add_argument("--data-dir", type=Path, default=ROOT / "data")
    result.add_argument("--artifacts-dir", type=Path, default=ROOT / "artifacts")
    commands = result.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="Generate isolated development and final batches")
    generate.add_argument("--size", type=int, default=300, help="Records in EACH batch (default 300)")
    generate.add_argument("--dev-seed", type=int, default=1101)
    generate.add_argument("--final-seed", type=int, default=2202)
    commands.add_parser("validate-plan").add_argument("--plan", type=Path, default=ROOT / "fixtures/plan.json")
    for name in ("seed", "summary", "sample", "run-fixture", "final-report", "run-ai-baseline"):
        command = commands.add_parser(name)
        command.add_argument("--dataset", required=True)
        if name in ("run-fixture", "final-report"):
            command.add_argument("--plan", type=Path, default=ROOT / "fixtures/plan.json")
        if name == "sample":
            command.add_argument("--supplier", required=True)
            command.add_argument("--limit", type=int, default=8)
    evaluate = commands.add_parser("evaluate", help="Re-evaluate a development run; never expose labels")
    evaluate.add_argument("--run", required=True)
    return result


def evaluate_run(store, args, run_id, event_log, final_report=False):
    # Evaluator imports and labels are absent from transformation and sampling tools.
    from evaluation.evaluator import evaluate
    run = store.runs.find_one({"_id": run_id})
    if not run or run["status"] not in ("transformed", "evaluated"):
        raise ValueError("Run is missing or incomplete")
    dataset_id = run["dataset_id"]
    split = store.dataset(dataset_id)["split"]
    if split == "final" and not final_report:
        raise ValueError("Final holdout is restricted to final-report")
    if store.raw_digest(dataset_id) != run.get("raw_digest"):
        raise ValueError("Stored raw records no longer match the run snapshot")
    labels = args.data_dir / "evaluator_only" / dataset_id / "labels.jsonl"
    report = evaluate(read_jsonl(labels), store.run_results(run_id), split=split)
    if report["overall"]["total_expected"] != store.dataset(dataset_id)["size"]:
        raise ValueError("Evaluator label count does not match dataset")
    report.update(run_id=run_id, dataset_id=dataset_id, kind=run["kind"],
                  model_requests=run.get("model_requests", []), raw_unchanged=run["raw_unchanged"])
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (args.artifacts_dir / (run_id + ".json")).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    store.runs.update_one({"_id": run_id}, {"$set": {"status": "evaluated", "evaluation": report,
                          "evaluated_at": datetime.now(timezone.utc)}})
    event_log.emit("evaluated", run_id, dataset_id, acceptance_passed=report["acceptance_passed"])
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["acceptance_passed"] else 2


def main(argv=None):
    args = parser().parse_args(argv)
    client = None
    try:
        if args.command == "generate":
            from evaluation.generate import generate
            print(json.dumps(generate(args.data_dir, args.size, args.dev_seed, args.final_seed), indent=2))
            return 0
        if args.command == "validate-plan":
            parse_plan(args.plan)
            print("PASS: Allowlisted transformation plan is valid (hand-authored fixture).")
            return 0
        if hasattr(args, "dataset") and (not args.dataset or len(args.dataset) > 100
                                          or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.dataset)):
            raise ValueError("Invalid dataset identifier")
        plan = parse_plan(args.plan) if hasattr(args, "plan") else None
        client, store = connect()
        if args.command == "seed":
            store.ensure_indexes()
            print(json.dumps(store.seed(args.data_dir / "agent_visible" / args.dataset), indent=2))
        elif args.command == "summary":
            print(json.dumps(store.summary(args.dataset), indent=2))
        elif args.command == "sample":
            print(json.dumps(store.sample(args.dataset, args.supplier, args.limit), indent=2))
        elif args.command in ("run-fixture", "final-report"):
            store.ensure_indexes()
            events = EventLog(args.artifacts_dir)
            final = args.command == "final-report"
            run_id = run_fixture(store, args.dataset, plan, events, final_report=final)
            return evaluate_run(store, args, run_id, events, final_report=final)
        elif args.command == "run-ai-baseline":
            from .openai_provider import OpenAIPlanProvider
            from .policy import HarnessPolicy
            if store.dataset(args.dataset)["split"] != "development":
                raise ValueError("AI baseline is development-only")
            policy = HarnessPolicy(version="ai-baseline-v1")
            metadata = store.supplier_metadata(args.dataset)
            samples = [row for supplier in metadata for row in store.sample(
                args.dataset, supplier["supplier_id"], policy.samples_per_supplier, policy.sampling_strategy)]
            provider = OpenAIPlanProvider(max_requests=1)
            args.artifacts_dir.mkdir(parents=True, exist_ok=True)
            try:
                generated_plan = provider.obtain_plan(samples, metadata, policy)
            finally:
                # Preserve attempted-request usage even if plan generation fails.
                attempt = {"timestamp": datetime.now(timezone.utc).isoformat(),
                           "kind": "ai_plan_generation", "dataset_id": args.dataset,
                           "requests": provider.request_events}
                with (args.artifacts_dir / "model-requests.jsonl").open("a") as stream:
                    stream.write(json.dumps(attempt, sort_keys=True) + "\n")
            store.ensure_indexes()
            events = EventLog(args.artifacts_dir)
            run_id = run_plan(store, args.dataset, generated_plan, events, kind="ai_development_baseline",
                              policy=policy, model_requests=provider.request_events)
            return evaluate_run(store, args, run_id, events)
        elif args.command == "evaluate":
            return evaluate_run(store, args, args.run, EventLog(args.artifacts_dir))
        return 0
    except Exception as exc:
        from .openai_http import OpenAIError
        if isinstance(exc, OpenAIError):
            print("FAIL: category={} code={}".format(exc.category, exc.code), file=sys.stderr)
            return 1
        # Database exception strings can include credentials or host details.
        print("ERROR ({}): operation failed; check local configuration, input files, "
              "plan validity, and Atlas network/permissions. Details suppressed to "
              "protect credentials.".format(type(exc).__name__), file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
