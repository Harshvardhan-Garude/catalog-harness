"""Final evaluation with fake model transport and in-memory persistence only."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from pymongo.errors import PyMongoError

from catalog_harness.openai_http import OpenAIError
from catalog_harness.storage import fingerprint
from enrichment.budget import write_json
from enrichment.final_evaluation import (_persisted_rows, _same_completed_batch,
                                         compare_final, execute)
from enrichment.freeze import manifest_digest
from enrichment.provider import BASELINE_POLICY
from enrichment_eval.fixtures import authored_split
from enrichment_eval.scoring import evaluate


MODEL = "gpt-4.1-mini-2025-04-14"
USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
CANDIDATE = {"version": "semantic-policy-v1-candidate",
             "instruction_addendum": "Check conflicting descriptions carefully.",
             "review_behavior": "abstain_invalid_evidence"}


class Collection:
    def __init__(self):
        self.rows = {}

    def update_one(self, query, update, upsert=False):
        identity = query["_id"]
        if identity not in self.rows:
            if not upsert:
                return
            self.rows[identity] = {"_id": identity, **copy.deepcopy(update.get("$setOnInsert", {}))}
        self.rows[identity].update(copy.deepcopy(update.get("$set", {})))

    def find_one(self, query, projection=None):
        return copy.deepcopy(self.rows.get(query["_id"]))


class Normalizer:
    def __init__(self, prior):
        self.prior = copy.deepcopy(prior)
        self.runs = Collection()
        self.raw = []
        self.normalized = {}
        self.seed_calls = 0
        self.manifests = []

    def ensure_indexes(self):
        pass

    def seed(self, directory):
        self.seed_calls += 1
        manifest = json.loads((Path(directory) / "manifest.json").read_text())
        self.manifests.append(manifest)
        incoming = [json.loads(line) for line in (Path(directory) / "records.jsonl").read_text().splitlines()]
        if self.raw and self.raw != incoming:
            raise ValueError("Changed input")
        self.raw = incoming
        return {"dataset_id": manifest["dataset_id"], **self.raw_digest(manifest["dataset_id"])}

    def dataset(self, identity):
        return {"dataset_id": identity, "split": "final", "size": 24}

    def raw_digest(self, identity):
        if identity in self.prior:
            return copy.deepcopy(self.prior[identity])
        return {"count": len(self.raw), "sha256": fingerprint(self.raw)}

    def raw_records(self, identity):
        return copy.deepcopy(self.raw)

    def save_results(self, run_id, rows):
        stored = self.normalized.setdefault(run_id, {})
        for row in rows:
            stored.setdefault(row["source_record_id"], copy.deepcopy(row))

    def run_results(self, run_id):
        return copy.deepcopy(list(self.normalized[run_id].values()))


class SemanticStore:
    def __init__(self, normalizer):
        self.normalizer = normalizer
        self.runs, self.policies, self.experiments = Collection(), Collection(), Collection()
        self.rows = {}
        self.fail_once = False
        self.tamper = False

    def ensure_indexes(self):
        pass

    def save_batch(self, run_id, outputs, details, checks, offset=0):
        if self.fail_once:
            self.fail_once = False
            raise PyMongoError("private-error-content")
        stored = self.rows.setdefault(run_id, {})
        for row in _persisted_rows(run_id, outputs, details, checks, offset):
            stored.setdefault(row["output_index"], copy.deepcopy(row))

    def read_results(self, run_id):
        rows = [copy.deepcopy(row) for _, row in sorted(self.rows.get(run_id, {}).items())]
        if self.tamper and rows:
            rows[0]["original_model_output"]["primary_use"] = "store_items"
        return rows

    def finish_run(self, run_id, report, calls):
        self.runs.update_one({"_id": run_id}, {"$set": {
            "status": "evaluated", "evaluation": report, "model_calls": calls}})


class CachedBudget:
    """Exercise the orchestrator's response replay, not the spending guard."""
    def __init__(self, directory, calls, uncertain=False):
        self.directory = Path(directory)
        self.path = self.directory / "calls.json"
        self.transmissions = calls
        self.uncertain = uncertain
        self.ledger = json.loads(self.path.read_text()) if self.path.exists() else {"calls": [], "stopped": False}
        write_json(self.path, self.ledger)

    def request(self, stage, **kwargs):
        cache = self.directory / (stage + "-cache.json")
        for record in self.ledger["calls"]:
            if record["stage"] == stage:
                if record["status"] != "completed":
                    raise OpenAIError("budget", "uncertain_or_failed_request")
                saved = json.loads(cache.read_text())
                if saved["request"] != kwargs:
                    raise OpenAIError("budget", "request_changed")
                return saved["output"], saved["details"]
        if self.ledger["stopped"]:
            raise OpenAIError("budget", "phase_stopped")
        self.transmissions.append({"stage": stage, **copy.deepcopy(kwargs)})
        record = {"ordinal": len(self.ledger["calls"]) + 1, "stage": stage, "status": "reserved", "usage": None}
        self.ledger["calls"].append(record)
        write_json(self.path, self.ledger)
        if self.uncertain:
            self.ledger["stopped"] = True
            write_json(self.path, self.ledger)
            raise OpenAIError("budget", "uncertain_or_failed_request")
        products = json.loads(kwargs["input_text"])["products"]
        output = {"products": [{"source_record_id": product["source_record_id"], "category": "unknown",
            "primary_use": "unknown", "secondary_uses": [], "evidence": [],
            "review": [{"field": "category", "reason": "Synthetic test response"},
                       {"field": "primary_use", "reason": "Synthetic test response"}]} for product in products]}
        details = {"model": MODEL, "usage": USAGE}
        write_json(cache, {"request": kwargs, "output": output, "details": details})
        record.update(status="completed", model=MODEL, usage=USAGE)
        write_json(self.path, self.ledger)
        return copy.deepcopy(output), copy.deepcopy(details)

    def summary(self):
        calls = self.ledger["calls"]
        return {"calls": len(calls), "max_calls": 12, "model": MODEL,
                "tokens": {key: sum((row["usage"] or {}).get(key, 0) for row in calls) for key in USAGE},
                "unknown_usage_calls": sum(row["usage"] is None for row in calls),
                "stopped": self.ledger["stopped"]}

    def close(self):
        pass


class FinalEvaluationTests(unittest.TestCase):
    def harness(self, directory, uncertain=False):
        root = Path(directory)
        original = root / "original"
        original.mkdir()
        phase = root / "final"
        fixtures = root / "fixtures"
        products, labels = authored_split("final")
        manifest = {"baseline_policy": BASELINE_POLICY, "split_counts": {"final": 24}, "version": "test"}
        prior_raw = {"normalization-dev": {"count": 300, "sha256": "unchanged-original"}}
        prior = {"status": "completed", "decision": {"accepted": True}, "experiment_id": "prior-experiment",
                 "freeze_sha256": manifest_digest(manifest),
                 "policies": {"baseline": BASELINE_POLICY, "candidate": CANDIDATE},
                 "prior_raw": prior_raw, "sources": {"development": {
                     "dataset_id": "semantic-v1-development", "raw_digest": {"count": 24, "sha256": "development"}},
                     "validation": {"dataset_id": "semantic-v1-validation", "raw_digest": {"count": 12, "sha256": "validation"}}}}
        write_json(original / "report.json", prior)
        documented = root / "policy.json"
        write_json(documented, CANDIDATE)
        for source in prior["sources"].values():
            prior_raw[source["dataset_id"]] = source["raw_digest"]
        normalizer = Normalizer(prior_raw)
        store = SemanticStore(normalizer)
        transmissions = []
        client = MagicMock()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("enrichment.final_evaluation.verify", return_value=manifest))
        stack.enter_context(patch("enrichment.final_evaluation.configuration", return_value=("secret-never-serialize", [])))
        connection = stack.enter_context(patch("enrichment.final_evaluation.connect", return_value=(client, normalizer)))
        stack.enter_context(patch("enrichment.final_evaluation.EnrichmentStore", return_value=store))
        loader = stack.enter_context(patch("enrichment.final_evaluation.load_public", return_value=copy.deepcopy(products)))
        key_loader = stack.enter_context(patch("enrichment.final_evaluation.load_labels", return_value=copy.deepcopy(labels)))
        factory = stack.enter_context(patch("enrichment.final_evaluation._make_budget",
            side_effect=lambda path, key: CachedBudget(path, transmissions, uncertain)))
        arguments = dict(fixtures_root=fixtures, original_artifacts=original, artifact_dir=phase,
                         documented_policy=documented)
        return arguments, store, transmissions, connection, factory, loader, key_loader

    def run_silently(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            status = execute(**arguments)
        self.assertNotIn("secret-never-serialize", output.getvalue())
        self.assertNotIn("private-error-content", output.getvalue())
        return status

    def test_fixed_final_policies_twelve_calls_aggregate_report_and_no_answer_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            args, store, calls, _, _, public_loader, key_loader = self.harness(directory)
            self.assertEqual(self.run_silently(args), 0)
            report = json.loads((args["artifact_dir"] / "report.json").read_text())
            state = json.loads((args["artifact_dir"] / "state.json").read_text())
            self.assertEqual(len(calls), 12)
            self.assertEqual([row["stage"] for row in calls],
                             ["{}_final_{:02d}".format(variant, ordinal) for variant in ("baseline", "candidate") for ordinal in range(1, 7)])
            for row in calls:
                context = json.loads(row["input_text"])
                self.assertEqual(len(context["products"]), 4)
                self.assertTrue(all(product["source_record_id"].startswith("sem-v1-final-") for product in context["products"]))
                self.assertEqual(row["max_output_tokens"], 2048)
                self.assertLessEqual(len(row["input_text"].encode()), 16384)
                for key in ("acceptable_categories", "acceptable_primary_uses", "required_secondary_uses", "optional_secondary_uses"):
                    self.assertNotIn(key, row["input_text"])
            self.assertTrue(report["both_policies_evaluated"])
            self.assertTrue(report["atlas_persistence_verified"])
            self.assertEqual(report["comparison"]["outcome"], "unchanged")
            self.assertFalse(report["comparison"]["policy_selection_performed"])
            self.assertFalse(report["retuning_permitted"])
            self.assertEqual(report["usage"]["calls"], 12)
            self.assertEqual(report["usage"]["tokens"]["total_tokens"], 1440)
            for variant, run in report["runs"].items():
                self.assertEqual(run["split"], "final")
                self.assertEqual(run["overall"]["total_field_decisions"], 72)
                self.assertEqual(run["representative_failures"], [])
                self.assertEqual(run["development_failures"], [])
                self.assertEqual(run["run_id"], state["run_ids"][variant])
            self.assertEqual(public_loader.call_args.args[1], "final")
            self.assertTrue(all(call.args[1] == "final" for call in key_loader.call_args_list))
            self.assertEqual(len(store.normalizer.runs.rows), 1)
            self.assertEqual(len(store.runs.rows), 2)
            self.assertTrue(all(row["split"] == "final" for row in store.normalizer.manifests))
            self.assertNotIn("prior-experiment", store.experiments.rows)

    def test_atlas_interruption_resumes_same_ids_and_replays_response_without_duplicate_call(self):
        with tempfile.TemporaryDirectory() as directory:
            args, store, calls, _, _, _, _ = self.harness(directory)
            store.fail_once = True
            self.assertEqual(self.run_silently(args), 1)
            self.assertEqual(len(calls), 1)
            initial = json.loads((args["artifact_dir"] / "state.json").read_text())
            self.assertEqual(self.run_silently(args), 0)
            resumed = json.loads((args["artifact_dir"] / "state.json").read_text())
            self.assertEqual(len(calls), 12)
            self.assertEqual(initial["phase_id"], resumed["phase_id"])
            self.assertEqual(initial["normalization_run_id"], resumed["normalization_run_id"])
            self.assertEqual(initial["run_ids"], resumed["run_ids"])
            self.assertEqual(len(store.normalizer.runs.rows), 1)
            self.assertEqual(len(store.runs.rows), 2)
            self.assertTrue(all(len(rows) == 24 for rows in store.rows.values()))

    def test_completed_command_is_read_only_and_transmits_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            args, store, calls, connection, factory, _, _ = self.harness(directory)
            self.assertEqual(self.run_silently(args), 0)
            before = {name: (args["artifact_dir"] / name).read_bytes() for name in ("state.json", "report.json", "calls.json")}
            self.assertEqual(self.run_silently(args), 0)
            self.assertEqual(len(calls), 12)
            self.assertEqual(connection.call_count, 1)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(before, {name: (args["artifact_dir"] / name).read_bytes() for name in before})
            self.assertEqual(len(store.normalizer.runs.rows), 1)

    def test_uncertain_request_never_retransmits_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _, calls, _, _, _, _ = self.harness(directory, uncertain=True)
            self.assertEqual(self.run_silently(args), 1)
            self.assertEqual(self.run_silently(args), 1)
            self.assertEqual(len(calls), 1)
            report = json.loads((args["artifact_dir"] / "report.json").read_text())
            self.assertEqual(report["error"], {"category": "budget", "code": "uncertain_or_failed_request"})
            self.assertFalse(report["both_policies_evaluated"])
            self.assertNotIn("atlas_persistence_verified", report)

    def test_changed_documented_policy_stops_before_network_or_model(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _, calls, connection, factory, _, _ = self.harness(directory)
            write_json(args["documented_policy"], dict(CANDIDATE, instruction_addendum="Changed"))
            self.assertEqual(self.run_silently(args), 1)
            connection.assert_not_called()
            factory.assert_not_called()
            self.assertEqual(calls, [])

    def test_changed_prior_raw_stops_before_spending_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            args, store, calls, _, factory, _, _ = self.harness(directory)
            store.normalizer.prior["normalization-dev"]["sha256"] = "changed"
            self.assertEqual(self.run_silently(args), 1)
            factory.assert_not_called()
            self.assertEqual(calls, [])

    def test_original_model_output_tampering_fails_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            args, store, calls, _, _, _, _ = self.harness(directory)
            store.tamper = True
            self.assertEqual(self.run_silently(args), 1)
            self.assertEqual(len(calls), 6)
            report = json.loads((args["artifact_dir"] / "report.json").read_text())
            self.assertNotIn("atlas_persistence_verified", report)
            self.assertEqual(report["error"], {"category": "local", "code": "verification_failed"})

    def test_missing_openai_configuration_reports_names_without_model_or_atlas(self):
        with tempfile.TemporaryDirectory() as directory:
            args, _, calls, connection, factory, _, _ = self.harness(directory)
            with patch("enrichment.final_evaluation.configuration", return_value=(None, ["OPENAI_API_KEY"])):
                self.assertEqual(self.run_silently(args), 1)
            connection.assert_not_called()
            factory.assert_not_called()
            self.assertEqual(calls, [])

    def test_comparison_reports_final_regression_without_selection(self):
        _, labels = authored_split("final")
        outputs = [{"source_record_id": label["source_record_id"], "category": label["acceptable_categories"][0],
                    "primary_use": label["acceptable_primary_uses"][0],
                    "secondary_uses": list(label["required_secondary_uses"])} for label in labels]
        baseline = evaluate(labels, outputs, "final")
        outputs[0]["category"] = "unknown"
        candidate = evaluate(labels, outputs, "final")
        comparison = compare_final(baseline, candidate)
        self.assertEqual(comparison["outcome"], "regression")
        self.assertEqual(comparison["correct_field_decisions_delta"], -1)
        self.assertFalse(comparison["policy_selection_performed"])
        self.assertFalse(comparison["retuning_permitted"])
        with self.assertRaises(ValueError):
            compare_final(dict(baseline, split="validation"), candidate)

    def test_resume_preserves_raw_model_rows_but_tolerates_review_note_order(self):
        row = {"category": "unknown", "primary_use": "unknown", "secondary_uses": [],
               "review": [{"field": "category", "reason": "First"},
                          {"field": "primary_use", "reason": "Second"}]}
        saved = {"outputs": [copy.deepcopy(row)], "details": {"model_outputs": [copy.deepcopy(row)]}}
        recomputed = copy.deepcopy(saved)
        recomputed["outputs"][0]["review"].reverse()
        self.assertTrue(_same_completed_batch(saved, recomputed))
        recomputed["details"]["model_outputs"][0]["review"].reverse()
        self.assertFalse(_same_completed_batch(saved, recomputed))
        recomputed = copy.deepcopy(saved)
        recomputed["outputs"][0]["category"] = "drinking_cup"
        self.assertFalse(_same_completed_batch(saved, recomputed))


if __name__ == "__main__":
    unittest.main()
