import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from catalog_harness.openai_http import OpenAIError
from enrichment.budget import ExperimentBudget


ARGS = {"schema": {}, "schema_name": "test", "instructions": "Synthetic", "input_text": "{}", "max_output_tokens": 128}
DETAILS = {"model": "gpt-4.1-mini-2025-04-14", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}


class SharedBudgetTests(unittest.TestCase):
    def test_global_cap_counts_baseline_and_proposer_and_candidate(self):
        transport = MagicMock(return_value=({}, DETAILS))
        with tempfile.TemporaryDirectory() as directory:
            budget = ExperimentBudget(directory, "fake-key-not-saved", max_calls=3, transport=transport)
            try:
                for stage in ("baseline_development", "policy_proposal", "candidate_validation"):
                    budget.request(stage, **ARGS)
                with self.assertRaises(OpenAIError):
                    budget.request("candidate_development", **ARGS)
                self.assertEqual(transport.call_count, 3)
                self.assertEqual(budget.summary()["tokens"]["total_tokens"], 45)
                self.assertNotIn("fake-key-not-saved", "".join(p.read_text() for p in Path(directory).glob('*.json')))
            finally:
                budget.close()
            with self.assertRaises(ValueError):
                ExperimentBudget(directory, "fake-key-not-saved", transport=transport)

    def test_failure_stops_requests_across_helpers(self):
        transport = MagicMock(side_effect=OpenAIError("quota_or_rate_limit", "http_429"))
        with tempfile.TemporaryDirectory() as directory:
            budget = ExperimentBudget(directory, "fake-key", transport=transport)
            try:
                with self.assertRaises(OpenAIError):
                    budget.request("baseline_development", **ARGS)
                with self.assertRaises(OpenAIError):
                    budget.request("policy_proposal", **ARGS)
                self.assertEqual(transport.call_count, 1)
                self.assertTrue(budget.summary()["stopped"])
                self.assertEqual(budget.summary()["unknown_usage_calls"], 1)
            finally:
                budget.close()

    def test_model_snapshot_change_stops_experiment_and_keeps_usage(self):
        changed = {**DETAILS, "model": "gpt-4.1-mini-2026-01-01"}
        transport = MagicMock(side_effect=[({}, DETAILS), ({}, changed)])
        with tempfile.TemporaryDirectory() as directory:
            budget = ExperimentBudget(directory, "fake-key", transport=transport)
            try:
                budget.request("baseline_development", **ARGS)
                with self.assertRaisesRegex(OpenAIError, "model_changed"):
                    budget.request("candidate_development", **ARGS)
                with self.assertRaises(OpenAIError):
                    budget.request("candidate_validation", **ARGS)
                self.assertEqual(transport.call_count, 2)
                self.assertEqual(budget.summary()["tokens"]["total_tokens"], 30)
                self.assertTrue(budget.summary()["stopped"])
            finally:
                budget.close()
