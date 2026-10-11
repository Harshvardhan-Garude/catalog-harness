"""Complete orchestration contracts with fake HTTP and in-memory Atlas only."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_harness.openai_http import OpenAIError
from enrichment.budget import ExperimentBudget
from enrichment.experiment import execute, run_stage
from enrichment.provider import BASELINE_POLICY
from tests.test_enrichment_provider import OUTPUT, PRODUCT, USAGE


DETAILS = {"model": "gpt-4.1-mini-2025-04-14", "usage": USAGE}
CANDIDATE = dict(BASELINE_POLICY, version="semantic-policy-v1-candidate",
                 instruction_addendum="Review source intent before assigning a category.")


def example_split(prefix, count):
    products, labels = [], []
    for index in range(count):
        product = copy.deepcopy(PRODUCT)
        product["source_record_id"] = prefix + "-{:03d}".format(index)
        product["normalized_product"]["product_id"] = "P-" + product["source_record_id"]
        products.append(product)
        labels.append({"source_record_id": product["source_record_id"],
                       "acceptable_categories": ["drinking_cup"],
                       "acceptable_primary_uses": ["drink_beverages"],
                       "required_secondary_uses": ["organize_desk_items"], "optional_secondary_uses": []})
    return products, labels


class MemoryCollection:
    def __init__(self):
        self.documents = {}

    def insert_one(self, document):
        self.documents[document["_id"]] = copy.deepcopy(document)

    def update_one(self, query, update):
        self.documents[query["_id"]].update(copy.deepcopy(update["$set"]))

    def find_one(self, query, projection=None):
        return copy.deepcopy(self.documents.get(query["_id"]))


class MemorySemanticStore:
    """A persistence double that keeps row occurrence and evaluation identity."""
    def __init__(self, normalizer):
        self.normalizer = normalizer
        self.runs = MemoryCollection()
        self.experiments = MemoryCollection()
        self.outputs = {}
        self.failures = []

    def ensure_indexes(self):
        pass

    def prepare_sources(self, products, split, directory):
        dataset = "semantic-v1-" + split
        return {"dataset_id": dataset, "normalization_run_id": "norm-" + split,
                "raw_digest": self.normalizer.raw_digest(dataset), "seed_repeatable": True}

    def start_run(self, experiment_id, variant, split, policy, source, freeze_hash):
        identity = variant + "_" + split
        self.runs.insert_one({"_id": identity, "status": "started"})
        self.outputs[identity] = []
        return identity

    def save_batch(self, run_id, outputs, details, checks, offset=0):
        for ordinal, output in enumerate(outputs, offset):
            self.outputs[run_id].append({"run_id": run_id, "output_index": ordinal,
                "source_record_id": output["source_record_id"], "output": copy.deepcopy(output),
                "evidence_checks": copy.deepcopy(checks[output["source_record_id"]])})

    def read_results(self, run_id):
        return copy.deepcopy(self.outputs[run_id])

    def finish_run(self, run_id, report, calls):
        self.runs.update_one({"_id": run_id}, {"$set": {"status": "evaluated", "evaluation": report}})

    def fail_run(self, run_id):
        self.failures.append(run_id)
        self.runs.update_one({"_id": run_id}, {"$set": {"status": "failed"}})


class ExperimentTests(unittest.TestCase):
    def harness(self, directory, imperfect=False, fail=False):
        products, labels = {}, {}
        for split, count in (("development", 24), ("validation", 12)):
            products[split], labels[split] = example_split(split, count)
        normalizer = MagicMock()
        normalizer.raw_digest.side_effect = lambda dataset: {"count": 24 if dataset.endswith("development") else 12,
                                                            "sha256": dataset + "-raw"}
        client = MagicMock()
        store = MemorySemanticStore(normalizer)
        observed = []

        def transport(key, **request):
            observed.append(copy.deepcopy(request))
            if fail:
                raise OpenAIError("network", "timeout")
            if request["schema_name"] == "semantic_policy_revision":
                return copy.deepcopy(CANDIDATE), copy.deepcopy(DETAILS)
            context = json.loads(request["input_text"])
            candidate = CANDIDATE["instruction_addendum"] in request["instructions"]
            rows = []
            for product in context["products"]:
                row = copy.deepcopy(OUTPUT)
                row["source_record_id"] = product["source_record_id"]
                if imperfect and not candidate and row["source_record_id"].endswith("-000"):
                    row["category"] = "cup_holder"
                    row["evidence"][0]["label"] = "cup_holder"
                rows.append(row)
            return {"products": rows}, copy.deepcopy(DETAILS)

        def budget_factory(path, key, max_calls):
            return ExperimentBudget(path, key, max_calls=max_calls, transport=transport)

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("enrichment.experiment.verify", return_value={"version": "test-freeze"}))
        stack.enter_context(patch("enrichment.experiment.configuration", return_value=("fake-key-not-saved", [])))
        stack.enter_context(patch("enrichment.experiment.connect", return_value=(client, normalizer)))
        stack.enter_context(patch("enrichment.experiment.EnrichmentStore", return_value=store))
        stack.enter_context(patch("enrichment.experiment.ExperimentBudget", side_effect=budget_factory))
        stack.enter_context(patch("enrichment.experiment.raw_snapshots", return_value={}))
        public_loader = stack.enter_context(patch("enrichment.experiment.load_public", side_effect=lambda root, split: copy.deepcopy(products[split])))
        label_loader = stack.enter_context(patch("enrichment.experiment.load_labels", side_effect=lambda root, split: copy.deepcopy(labels[split])))
        return store, client, observed, public_loader, label_loader

    def test_perfect_development_runs_nine_calls_and_skips_revision_and_final(self):
        with tempfile.TemporaryDirectory() as directory:
            store, client, observed, public_loader, label_loader = self.harness(directory)
            with redirect_stdout(io.StringIO()):
                status = execute(Path(directory) / "fixtures", Path(directory) / "artifacts")
            report = json.loads((Path(directory) / "artifacts/report.json").read_text())
            ledger = json.loads((Path(directory) / "artifacts/calls.json").read_text())
            self.assertEqual(status, 0)
            self.assertEqual(len(observed), 9)
            self.assertEqual(report["usage"]["calls"], 9)
            self.assertEqual(report["usage"]["tokens"]["total_tokens"], 9 * USAGE["total_tokens"])
            self.assertEqual(report["decision"]["reason"], "perfect_development_baseline_skip_revision")
            self.assertEqual(set(report["runs"]), {"baseline_development", "baseline_validation"})
            self.assertFalse(report["final_holdout_evaluated"])
            self.assertEqual([call.args[1] for call in public_loader.call_args_list], ["development", "validation"])
            self.assertEqual([call.args[1] for call in label_loader.call_args_list], ["development", "validation"])
            self.assertFalse(any(call["stage"] == "policy_proposal" for call in ledger["calls"]))
            self.assertTrue(all(run["atlas_persistence_verified"] and run["raw_unchanged"] for run in report["runs"].values()))
            self.assertNotIn("fake-key-not-saved", "".join(path.read_text() for path in (Path(directory) / "artifacts").rglob("*.json")))
        client.close.assert_called_once()

    def test_nineteen_call_revision_proposes_from_development_before_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            store, client, observed, _, _ = self.harness(directory, imperfect=True)
            with redirect_stdout(io.StringIO()):
                status = execute(Path(directory) / "fixtures", Path(directory) / "artifacts")
            report = json.loads((Path(directory) / "artifacts/report.json").read_text())
            ledger = json.loads((Path(directory) / "artifacts/calls.json").read_text())
            self.assertEqual(status, 0)
            self.assertEqual(len(observed), 19)
            stages = [call["stage"] for call in ledger["calls"]]
            self.assertEqual(stages, ["baseline_development"] * 6 + ["policy_proposal"] + ["baseline_validation"] * 3
                             + ["candidate_development"] * 6 + ["candidate_validation"] * 3)
            proposal = json.loads(observed[6]["input_text"])
            self.assertTrue(all(row["source_record_id"].startswith("development-") for row in proposal["development_products"]))
            self.assertTrue(all(row["source_record_id"].startswith("development-") for row in proposal["development_predictions"]))
            serialized = json.dumps(proposal)
            for forbidden in ("acceptable_categories", "acceptable_primary_uses", "required_secondary_uses", "optional_secondary_uses", "validation-", "final-"):
                self.assertNotIn(forbidden, serialized)
            self.assertTrue(report["decision"]["accepted"])
            self.assertEqual(report["policies"]["candidate"], CANDIDATE)
            self.assertEqual(report["runs"]["baseline_validation"]["overall"]["correct_field_decisions"], 35)
            self.assertEqual(report["runs"]["candidate_validation"]["overall"]["correct_field_decisions"], 36)
            self.assertEqual(report["usage"]["calls"], 19)
            self.assertEqual(report["usage"]["tokens"]["total_tokens"], 19 * USAGE["total_tokens"])
        client.close.assert_called_once()

    def test_connectivity_failure_stops_after_one_call_and_persists_sanitized_report(self):
        with tempfile.TemporaryDirectory() as directory:
            store, client, observed, _, _ = self.harness(directory, fail=True)
            output = io.StringIO()
            with redirect_stdout(output):
                status = execute(Path(directory) / "fixtures", Path(directory) / "artifacts")
            report = json.loads((Path(directory) / "artifacts/report.json").read_text())
            self.assertEqual(status, 1)
            self.assertEqual(len(observed), 1)
            self.assertEqual(report["error"], {"category": "network", "code": "timeout"})
            self.assertEqual(report["usage"]["calls"], 1)
            self.assertTrue(report["usage"]["stopped"])
            self.assertEqual(report["usage"]["unknown_usage_calls"], 1)
            self.assertEqual(report["runs"], {})
            self.assertEqual(store.failures, ["baseline_development"])
            self.assertNotIn("fake-key-not-saved", output.getvalue())
        client.close.assert_called_once()


class RunStageTests(unittest.TestCase):
    def test_atlas_evidence_tampering_fails_persistence_verification(self):
        products, labels = example_split("development", 1)
        normalizer = MagicMock()
        normalizer.raw_digest.return_value = {"count": 1, "sha256": "raw"}
        store = MemorySemanticStore(normalizer)
        original_read = store.read_results

        def tampered(run_id):
            rows = original_read(run_id)
            rows[0]["evidence_checks"] = [{"field": "category", "code": "missing_evidence", "label": "drinking_cup"}]
            return rows

        store.read_results = tampered
        row = copy.deepcopy(OUTPUT)
        row["source_record_id"] = products[0]["source_record_id"]
        budget = MagicMock()
        budget.ledger = {"calls": []}
        taxonomy = json.loads((Path(__file__).resolve().parents[1] / "enrichment/taxonomy.json").read_text())
        with tempfile.TemporaryDirectory() as directory, patch("enrichment.experiment.enrich_batch", return_value=(
                [row], DETAILS, {row["source_record_id"]: []})), redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "evidence persistence"):
            run_stage(experiment_id="test", variant="baseline", split="development", policy=BASELINE_POLICY,
                products=products, labels=labels, taxonomy=taxonomy, budget=budget, store=store,
                source={"dataset_id": "semantic-v1-development", "raw_digest": {"count": 1, "sha256": "raw"}},
                freeze_hash="frozen", directory=directory)
        self.assertEqual(store.failures, ["baseline_development"])


if __name__ == "__main__":
    unittest.main()
