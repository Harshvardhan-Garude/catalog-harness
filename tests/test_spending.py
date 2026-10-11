import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from catalog_harness.openai_http import OpenAIError
from enrichment.spending import (FinalBudget, EXPECTED_MODEL, estimate_request,
                                 cost, prior_inventory, PHASE_CAP, TOTAL_CAP)


REQUEST = dict(schema={"type": "object"}, schema_name="test", instructions="Frozen instructions",
               input_text="Public supplier facts only", max_output_tokens=2048)
DETAILS = {"model": EXPECTED_MODEL, "usage": {"input_tokens": 150, "output_tokens": 20, "total_tokens": 170}}


class SpendingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        previous = self.root / 'artifacts/enrichment-v1'
        previous.mkdir(parents=True)
        previous.joinpath('calls.json').write_text(json.dumps({"calls": [
            {"ordinal": i + 1, "status": "completed", **DETAILS} for i in range(19)]}))
        self.directory = self.root / 'artifacts/enrichment-v1-final'

    def tearDown(self):
        self.temporary.cleanup()

    def budget(self, transport):
        return FinalBudget(self.directory, 'test-key-never-persisted', transport=transport, root=self.root)

    def test_completed_stage_resumes_without_transmitting(self):
        transport = Mock(return_value=({"products": []}, copy.deepcopy(DETAILS)))
        budget = self.budget(transport)
        first = budget.request('baseline_final_01', **REQUEST)
        budget.close()
        budget = self.budget(transport)
        self.assertEqual(budget.request('baseline_final_01', **REQUEST), first)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(budget.summary()['calls'], 1)
        self.assertEqual(budget.summary()['phase_actual_usd'], cost(DETAILS['usage']) / 1e9)
        budget.close()
        self.assertNotIn('test-key-never-persisted', ''.join(p.read_text() for p in self.directory.glob('*.json')))

    def test_uncertain_reservation_cannot_repeat_or_skip(self):
        transport = Mock(return_value=({}, copy.deepcopy(DETAILS)))
        budget = self.budget(transport)
        budget.request('baseline_final_01', **REQUEST)
        (self.directory / 'response-01.json').unlink()
        budget.ledger['calls'][0]['status'] = 'reserved'
        from enrichment.budget import write_json
        write_json(budget.path, budget.ledger)
        budget.close()
        budget = self.budget(transport)
        for stage in ('baseline_final_01', 'baseline_final_02'):
            with self.assertRaises(OpenAIError):
                budget.request(stage, **REQUEST)
        self.assertEqual(transport.call_count, 1)
        budget.close()

    def test_response_durable_before_completed_status_recovers(self):
        transport = Mock(return_value=({}, copy.deepcopy(DETAILS)))
        budget = self.budget(transport)
        budget.request('baseline_final_01', **REQUEST)
        budget.ledger['calls'][0]['status'] = 'reserved'
        from enrichment.budget import write_json
        write_json(budget.path, budget.ledger)
        budget.close()
        budget = self.budget(transport)
        budget.request('baseline_final_01', **REQUEST)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(budget.ledger['calls'][0]['status'], 'completed')
        budget.close()

    def test_failure_stops_all_calls_without_retry(self):
        transport = Mock(side_effect=OpenAIError('network', 'timeout'))
        budget = self.budget(transport)
        for stage in ('baseline_final_01', 'baseline_final_02'):
            with self.assertRaises(OpenAIError):
                budget.request(stage, **REQUEST)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(budget.summary()['unknown_usage_calls'], 1)
        self.assertGreater(budget.summary()['phase_reserved_usd'], 0)
        budget.close()

    def test_estimate_includes_schema_instructions_maximum_output(self):
        estimate = estimate_request(REQUEST)
        self.assertGreater(estimate['input_tokens_upper_bound'], len(REQUEST['input_text']))
        self.assertGreater(estimate['reserved_nanodollars'], 2048 * 1600)
        changed = dict(REQUEST, schema={"description": 'a' * 1000})
        self.assertGreater(estimate_request(changed)['reserved_nanodollars'], estimate['reserved_nanodollars'])

    def test_phase_and_cumulative_caps_block_before_transport(self):
        for cap in ('phase', 'cumulative'):
            transport = Mock(return_value=({}, copy.deepcopy(DETAILS)))
            directory = self.root / cap
            budget = FinalBudget(directory, 'key', transport=transport, root=self.root)
            if cap == 'phase':
                budget.ledger['calls'] = [{"stage": "baseline_final_01", "request_sha256": "old", "status": "completed",
                    "ordinal": 1, "usage": DETAILS['usage'], "actual_nanodollars": PHASE_CAP,
                    "estimate": {"reserved_nanodollars": PHASE_CAP}}]
            else:
                budget.ledger['prior']['upper_bound_nanodollars'] = TOTAL_CAP
            with self.assertRaises(OpenAIError):
                budget.request('baseline_final_02', **REQUEST)
            transport.assert_not_called()
            budget.close()

    def test_stage_fingerprint_and_model_are_fixed(self):
        transport = Mock(return_value=({}, copy.deepcopy(DETAILS)))
        budget = self.budget(transport)
        budget.request('baseline_final_01', **REQUEST)
        with self.assertRaises(OpenAIError):
            budget.request('baseline_final_01', **dict(REQUEST, input_text='changed'))
        with self.assertRaises(OpenAIError):
            budget.request('proposal', **REQUEST)
        budget.close()
        self.assertEqual(transport.call_count, 1)

    def test_canonical_prior_records_not_duplicate_reports(self):
        art = self.root / 'artifacts'
        art.joinpath('model-requests.jsonl').write_text(json.dumps({'requests': [DETAILS]}) + '\n')
        art.joinpath('openai-check.json').write_text(json.dumps(DETAILS))
        art.joinpath('duplicate-report.json').write_text(json.dumps({'usage': DETAILS['usage']}))
        inventory = prior_inventory(self.root)
        self.assertEqual(inventory['calls'], 21)
        self.assertEqual(inventory['upper_bound_nanodollars'], 21 * cost(DETAILS['usage']))

    def test_changed_returned_model_stops_phase_without_retry(self):
        details = copy.deepcopy(DETAILS)
        details['model'] = 'gpt-4.1-mini-2099-01-01'
        transport = Mock(return_value=({}, details))
        budget = self.budget(transport)
        with self.assertRaises(OpenAIError) as caught:
            budget.request('baseline_final_01', **REQUEST)
        self.assertEqual(caught.exception.code, 'model_changed')
        self.assertTrue(budget.summary()['stopped'])
        with self.assertRaises(OpenAIError):
            budget.request('baseline_final_02', **REQUEST)
        self.assertEqual(transport.call_count, 1)
        budget.close()

    def test_unknown_historical_usage_is_reserved_conservatively(self):
        from enrichment.spending import UNKNOWN_RESERVATION
        path = self.root / 'artifacts/enrichment-v1/calls.json'
        ledger = json.loads(path.read_text())
        ledger['calls'][0]['usage'] = None
        path.write_text(json.dumps(ledger))
        inventory = prior_inventory(self.root)
        self.assertEqual(inventory['upper_bound_nanodollars'],
                         18 * cost(DETAILS['usage']) + UNKNOWN_RESERVATION)


if __name__ == '__main__':
    unittest.main()
