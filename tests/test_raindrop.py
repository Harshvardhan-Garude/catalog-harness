"""Saved-trace export and explicit telemetry transport, entirely offline."""

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from enrichment import raindrop


USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
MODEL = "gpt-4.1-mini-2025-04-14"
CONFIG = {"RAINDROP_WRITE_KEY": "private-write-key", "RAINDROP_PROJECT_ID": "catalog-harness-demo"}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def phases(root):
    for final in (True, False):
        folder = Path(root) / ("artifacts/enrichment-v1-final" if final else "artifacts/enrichment-v1")
        stages = (["{}_final_{:02d}".format(variant, batch) for variant in ("baseline", "candidate") for batch in range(1, 7)]
                  if final else ["baseline_development"] * 6 + ["policy_proposal"] + ["baseline_validation"] * 3
                  + ["candidate_development"] * 6 + ["candidate_validation"] * 3)
        report = {"status": "completed", "atlas_persistence_verified": True, "raw_unchanged": True,
                  "phase_id" if final else "experiment_id": "final-phase" if final else "old-experiment",
                  "runs": {}, "fixture_version": "semantic-v1"}
        ledger = {"model": MODEL, "calls": []}
        for ordinal, stage in enumerate(stages, 1):
            request = {"schema": {}, "schema_name": "saved_trace", "instructions": "Use listing evidence only.",
                       "input_text": json.dumps({"products": [{"source_record_id": "synthetic-{:02d}".format(ordinal),
                                                                "name": "Saved product input"}]}), "max_output_tokens": 2048}
            digest = hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()
            raw = {"products": [{"source_record_id": "synthetic-{:02d}".format(ordinal), "primary_use": "store_items",
                                 "raw_marker": "model-original-before-harness-abstention"}]}
            call = {"ordinal": ordinal, "stage": stage, "status": "completed", "model": MODEL, "usage": USAGE}
            response = {"output": raw, "details": {"model": MODEL, "usage": USAGE}, "request_sha256": digest}
            if final:
                call["request_sha256"] = digest
                request["request_sha256"] = digest
            else:
                response = {"output": raw, "model": MODEL, "usage": USAGE}
            request["stage"] = stage
            ledger["calls"].append(call)
            write(folder / ("request-{:02d}.json".format(ordinal)), request)
            write(folder / ("response-{:02d}.json".format(ordinal)), response)
            if stage == "policy_proposal":
                continue
            run_key = stage.split("_", 1)[0] if final else stage
            split = "final" if final else stage.split("_", 1)[1]
            counts = dict.fromkeys(raindrop.COUNT_KEYS, 0)
            counts.update(total_expected=24 if split != "validation" else 12,
                          total_field_decisions=72 if split != "validation" else 36,
                          correct_field_decisions=70 if split != "validation" else 35)
            count = 6 if final or split == "development" else 3
            report["runs"][run_key] = {"run_id": "run-" + run_key, "split": split, "overall": counts,
                "token_usage": {key: value * count for key, value in USAGE.items()}, "model_calls": count,
                "representative_failures": [{"private_expected_answer": "must-not-export"}]}
        write(folder / "calls.json", ledger)
        write(folder / "report.json", report)


class ExportTests(unittest.TestCase):
    def test_final_and_historical_export_preserve_real_io_and_aggregate_only_signals(self):
        with tempfile.TemporaryDirectory() as directory:
            phases(directory)
            with patch.object(raindrop, "build_opener") as network:
                result = raindrop.export_outbox(directory, include_historical=True)
                network.assert_not_called()
            self.assertEqual((result["events"], result["signals"]), (31, 6))
            folder = Path(result["outbox"])
            events = json.loads((folder / "events.json").read_text())
            signals = json.loads((folder / "signals.json").read_text())
            self.assertTrue(all(event["properties"]["historical"] for event in events))
            self.assertIn("model-original-before-harness-abstention", events[0]["ai_data"]["output"])
            actual_input = json.loads(events[0]["ai_data"]["input"])
            self.assertEqual(actual_input["instructions"], "Use listing evidence only.")
            self.assertIn("Saved product input", actual_input["input_text"])
            self.assertEqual(actual_input["schema"], {})
            self.assertEqual(actual_input["schema_name"], "saved_trace")
            self.assertEqual(actual_input["max_output_tokens"], 2048)
            self.assertIs(actual_input["store"], False)
            self.assertIs(actual_input["strict_json_schema"], True)
            self.assertEqual(actual_input["requested_model"], "gpt-4.1-mini")
            self.assertEqual(events[0]["properties"]["request_configuration"],
                             {key: actual_input[key] for key in ("schema", "schema_name", "max_output_tokens",
                                                               "store", "strict_json_schema", "requested_model")})
            self.assertEqual(events[0]["ai_data"]["model"], MODEL)
            self.assertEqual(events[0]["properties"]["model_usage"], USAGE)
            self.assertTrue(all(signal["properties"]["scope"] == "variant_aggregate" for signal in signals))
            self.assertTrue({signal["event_id"] for signal in signals} <= {event["event_id"] for event in events})
            self.assertNotIn("must-not-export", json.dumps(signals))
            self.assertNotIn("representative_failures", json.dumps(signals))
            self.assertNotIn("private-write-key", "".join(path.read_text() for path in folder.glob("*.json")))
            self.assertEqual(raindrop.export_outbox(directory, include_historical=True), result)

    def test_changed_request_hash_or_private_context_prevents_export(self):
        with tempfile.TemporaryDirectory() as directory:
            phases(directory)
            path = Path(directory) / "artifacts/enrichment-v1-final/response-01.json"
            response = json.loads(path.read_text())
            response["request_sha256"] = "wrong"
            write(path, response)
            with self.assertRaises(raindrop.RaindropError) as raised:
                raindrop.export_outbox(directory)
            self.assertEqual(raised.exception.code, "request_provenance_invalid")
            with self.assertRaises(raindrop.RaindropError) as raised:
                raindrop._safe_content({"nested": {"acceptable_categories": ["private-answer"]}})
            self.assertEqual(raised.exception.code, "private_context_rejected")
            self.assertNotIn("private-answer", str(raised.exception))

    def test_export_is_immutable_and_payloads_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            phases(directory)
            raindrop.export_outbox(directory)
            with self.assertRaises(raindrop.RaindropError) as raised:
                raindrop.export_outbox(directory, include_historical=True)
            self.assertEqual(raised.exception.code, "immutable_outbox_changed")
            with self.assertRaises(raindrop.RaindropError) as raised:
                raindrop._encode([{"large": "x" * (1024 * 1024)}])
            self.assertEqual(raised.exception.code, "payload_limit")


class TransportTests(unittest.TestCase):
    def test_one_post_array_bearer_project_header_timeout_and_204(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.status = 204
        with patch.object(raindrop, "build_opener", return_value=opener) as builder:
            raindrop.post_array("events", [{"event_id": "trace"}], CONFIG)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.raindrop.ai/v1/events/track")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), [{"event_id": "trace"}])
        self.assertEqual(request.get_header("Authorization"), "Bearer private-write-key")
        self.assertEqual(request.get_header("X-raindrop-project-id"), "catalog-harness-demo")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 30})
        self.assertEqual(opener.open.call_count, 1)
        self.assertIsInstance(builder.call_args.args[0], raindrop.NoRedirect)
        self.assertIsNone(builder.call_args.args[0].redirect_request(request, None, 302, "private", {}, "https://other.invalid"))

    def test_other_2xx_acknowledgements_match_the_official_sdk_without_reading_body(self):
        for status in (200, 201, 202, 204, 299):
            with self.subTest(status=status):
                opener = MagicMock()
                response = opener.open.return_value.__enter__.return_value
                response.status = status
                with patch.object(raindrop, "build_opener", return_value=opener):
                    self.assertEqual(raindrop.post_array("signals", [], CONFIG), {"http_status": status})
                self.assertEqual(opener.open.call_count, 1)
                response.read.assert_not_called()

    def test_unexpected_status_retains_only_numeric_diagnostic_and_never_retries(self):
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 302
        with patch.object(raindrop, "build_opener", return_value=opener), self.assertRaises(raindrop.RaindropError) as raised:
            raindrop.post_array("events", [], CONFIG)
        self.assertEqual((raised.exception.category, raised.exception.code), ("response", "http_302"))
        self.assertEqual(opener.open.call_count, 1)
        response.read.assert_not_called()

    def test_failures_are_sanitized_and_never_retry_or_read_error_body(self):
        for error, category, code in ((HTTPError("private-host", 401, "private-key", {}, io.BytesIO(b"private-body")), "authentication", "http_401"),
                                      (URLError("private-network-message"), "network", "connection")):
            opener = MagicMock()
            opener.open.side_effect = error
            with patch.object(raindrop, "build_opener", return_value=opener), self.assertRaises(raindrop.RaindropError) as raised:
                raindrop.post_array("signals", [], CONFIG)
            self.assertEqual((raised.exception.category, raised.exception.code), (category, code))
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_missing_configuration_prints_names_and_default_project_is_refused(self):
        output = io.StringIO()
        with patch.object(raindrop, "configuration", return_value=(None, ["RAINDROP_WRITE_KEY", "RAINDROP_PROJECT_ID"])), \
                patch.object(raindrop, "post_array") as transport, redirect_stdout(output):
            status = raindrop.main(["send"])
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue().strip(), "Missing: RAINDROP_WRITE_KEY, RAINDROP_PROJECT_ID")
        transport.assert_not_called()
        with patch.object(raindrop.os, "environ", {}), patch.object(raindrop, "dotenv_values", return_value={**CONFIG, "RAINDROP_PROJECT_ID": "default"}), \
                self.assertRaises(raindrop.RaindropError) as raised:
            raindrop.configuration()
        self.assertEqual(raised.exception.code, "explicit_project_required")


class DurableDeliveryTests(unittest.TestCase):
    def test_success_sends_events_before_linked_signals_once_and_resume_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            phases(directory)
            exported = raindrop.export_outbox(directory)
            transport = MagicMock()
            result = raindrop.send_outbox(exported["outbox"], CONFIG, transport)
            self.assertEqual(result["status"], "sent")
            self.assertEqual([call.args[0] for call in transport.call_args_list], ["events", "signals"])
            raindrop.send_outbox(exported["outbox"], CONFIG, transport)
            self.assertEqual(transport.call_count, 2)
            self.assertNotIn("private-write-key", (Path(exported["outbox"]) / "delivery.json").read_text())

    def test_success_persists_acknowledgement_status_and_time(self):
        with tempfile.TemporaryDirectory() as directory:
            phases(directory)
            exported = raindrop.export_outbox(directory)
            raindrop.send_outbox(exported["outbox"], CONFIG, MagicMock(return_value={"http_status": 200}))
            state = json.loads((Path(exported["outbox"]) / "delivery.json").read_text())
            for endpoint in ("events", "signals"):
                self.assertEqual(state[endpoint]["http_status"], 200)
                self.assertIn("acknowledged_at", state[endpoint])

    def test_unknown_completion_and_crash_reservation_block_all_retransmission(self):
        with tempfile.TemporaryDirectory() as directory:
            phases(directory)
            exported = raindrop.export_outbox(directory)
            transport = MagicMock(side_effect=raindrop.RaindropError("network", "timeout"))
            with self.assertRaises(raindrop.RaindropError):
                raindrop.send_outbox(exported["outbox"], CONFIG, transport)
            path = Path(exported["outbox"]) / "delivery.json"
            state = json.loads(path.read_text())
            self.assertEqual(state["events"]["status"], "unknown")
            self.assertEqual(state["signals"]["status"], "pending")
            with self.assertRaises(raindrop.RaindropError) as raised:
                raindrop.send_outbox(exported["outbox"], CONFIG, transport)
            self.assertEqual(raised.exception.code, "delivery_uncertain_or_failed")
            self.assertEqual(transport.call_count, 1)
            state["events"]["status"] = "reserved"
            write(path, state)
            with self.assertRaises(raindrop.RaindropError):
                raindrop.send_outbox(exported["outbox"], CONFIG, transport)
            self.assertEqual(transport.call_count, 1)

    def test_outbox_mutation_is_detected_before_network_send(self):
        with tempfile.TemporaryDirectory() as directory:
            phases(directory)
            exported = raindrop.export_outbox(directory)
            path = Path(exported["outbox"]) / "events.json"
            events = json.loads(path.read_text())
            events[0]["event_id"] += "-changed"
            write(path, events)
            transport = MagicMock()
            with self.assertRaises(raindrop.RaindropError) as raised:
                raindrop.send_outbox(exported["outbox"], CONFIG, transport)
            self.assertEqual(raised.exception.code, "outbox_changed")
            transport.assert_not_called()

    def test_changed_signals_are_checked_before_any_events_are_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            phases(directory)
            exported = raindrop.export_outbox(directory)
            path = Path(exported["outbox"]) / "signals.json"
            signals = json.loads(path.read_text())
            signals[0]["event_id"] += "-changed"
            write(path, signals)
            transport = MagicMock()
            with self.assertRaises(raindrop.RaindropError):
                raindrop.send_outbox(exported["outbox"], CONFIG, transport)
            transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
