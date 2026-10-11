"""Positive readback recovery, entirely offline; ingestion is never invoked."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from enrichment import raindrop, raindrop_query as query
from enrichment.budget import write_json


CONFIG = {"RAINDROP_QUERY_API_KEY": "private-query-key", "RAINDROP_PROJECT_ID": "catalog-harness"}


def outbox(directory, count=3):
    folder = Path(directory)
    events = [{"event_id": "catalog-harness-call-{}".format(index), "event": "semantic_enrichment_import",
               "user_id": "synthetic-demo", "properties": {"request_sha256": "a" * 64,
                   "run_id": "run-{}".format(index), "historical": True,
                   "model_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                   "request_configuration": {"store": False, "schema": {"type": "object"}}},
               "ai_data": {"model": "gpt-4.1-mini-2025-04-14", "convo_id": "run-{}".format(index),
                           "input": "private-looking saved input {}".format(index),
                           "output": '{"products": []}'}}
              for index in range(count)]
    signals = [{"event_id": events[0]["event_id"], "signal_name": "synthetic_enrichment_evaluation",
                "properties": {"counts": {"correct_field_decisions": 70}, "scope": "variant_aggregate"}}]
    manifest = {"version": 1, "kind": "saved_synthetic_raindrop_import", "historical": True,
                "events": len(events), "signals": len(signals),
                "events_sha256": raindrop._digest(events), "signals_sha256": raindrop._digest(signals)}
    state = {"version": 1, "project_id": CONFIG["RAINDROP_PROJECT_ID"],
             "events": {"status": "unknown", "sha256": manifest["events_sha256"],
                        "error": {"category": "response", "code": "unexpected_status"}},
             "signals": {"status": "pending", "sha256": manifest["signals_sha256"]}}
    for name, payload in (("events", events), ("signals", signals), ("manifest", manifest), ("delivery", state)):
        write_json(folder / (name + ".json"), payload)
    return folder, events


def response(event):
    return {"data": {"id": event["event_id"], "event_name": event["event"], "user_id": event["user_id"],
                     "convo_id": event["ai_data"]["convo_id"], "model": event["ai_data"]["model"],
                     "user_input": event["ai_data"]["input"], "assistant_output": event["ai_data"]["output"],
                     "properties": copy.deepcopy(event["properties"]), "timestamp": "2026-10-11T15:00:00Z"}}


def reader(events):
    by_id = {event["event_id"]: event for event in events}
    return MagicMock(side_effect=lambda identity, config: response(by_id[identity]))


class RecoveryTests(unittest.TestCase):
    def test_all_31_positive_proofs_reconcile_once_preserving_error_and_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, events = outbox(directory, count=31)
            immutable = {name: (folder / name).read_bytes()
                         for name in ("events.json", "signals.json", "manifest.json")}
            transport = reader(events)
            with patch.object(raindrop, "post_array") as ingest:
                result = query.verify_outbox(folder, CONFIG, transport)
                ingest.assert_not_called()
            self.assertEqual(result["status"], "verified")
            self.assertEqual((result["verified_events"], result["network_gets_added"]), (31, 31))
            self.assertTrue(result["events_reconciled"])
            self.assertFalse(result["signal_properties_verified"])
            self.assertEqual(result["signals_status"], "pending")
            self.assertNotIn("private", json.dumps(result))
            state = raindrop._read(folder / "delivery.json")
            self.assertEqual(state["events"]["status"], "sent")
            self.assertEqual(state["events"]["error"], {"category": "response", "code": "unexpected_status"})
            self.assertEqual(state["events"]["query_verification"]["original_status"], "unknown")
            self.assertEqual(state["events"]["query_verification"]["events"], 31)
            self.assertEqual(state["signals"]["status"], "pending")
            receipt = raindrop._read(folder / query.RECEIPT_NAME)
            self.assertEqual(len(receipt["verified"]), 31)
            self.assertNotIn("private", json.dumps(receipt))
            self.assertNotIn("assistant_output", json.dumps(receipt))
            self.assertEqual(state["events"]["query_verification"]["receipt_sha256"], raindrop._digest(receipt))
            for name, contents in immutable.items():
                self.assertEqual((folder / name).read_bytes(), contents)
            transport.reset_mock()
            resumed = query.verify_outbox(folder, CONFIG, transport)
            self.assertEqual(resumed["status"], "verified")
            self.assertEqual(resumed["network_gets_added"], 0)
            self.assertFalse(resumed["events_reconciled"])
            transport.assert_not_called()

    def test_404_stops_keeps_unknown_and_resumes_only_remaining_gets(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, events = outbox(directory)
            transport = MagicMock(side_effect=[response(events[0]), raindrop.RaindropError("not_found", "http_404")])
            result = query.verify_outbox(folder, CONFIG, transport)
            self.assertEqual(result["status"], "unverified")
            self.assertEqual(result["verified_events"], 1)
            self.assertEqual(result["network_gets_added"], 2)
            self.assertEqual(transport.call_count, 2)
            self.assertEqual(raindrop._read(folder / "delivery.json")["events"]["status"], "unknown")
            resumed_transport = reader(events)
            resumed = query.verify_outbox(folder, CONFIG, resumed_transport)
            self.assertEqual(resumed["status"], "verified")
            self.assertEqual([call.args[0] for call in resumed_transport.call_args_list],
                             [event["event_id"] for event in events[1:]])
            self.assertEqual(resumed["network_gets_added"], 2)

    def test_each_content_field_and_nested_provenance_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, events = outbox(directory)
            for field in ("id", "event_name", "user_id", "convo_id", "model", "user_input", "assistant_output"):
                altered = response(events[0])
                altered["data"][field] = "different-sensitive-content"
                transport = MagicMock(return_value=altered)
                result = query.verify_outbox(folder, CONFIG, transport)
                self.assertEqual(result["status"], "unverified", field)
                self.assertEqual(result["error"]["code"], "event_content_mismatch")
                self.assertEqual(result["verified_events"], 0)
                self.assertEqual(transport.call_count, 1)
                self.assertNotIn("sensitive", json.dumps(result))
            for replacement in ({"model_usage": {"input_tokens": 101, "output_tokens": 20, "total_tokens": 121}},
                                {"historical": 1}, {"run_id": "different"}, {"request_sha256": "b" * 64},
                                {"request_configuration": {"store": True, "schema": {"type": "object"}}}):
                altered = response(events[0])
                altered["data"]["properties"].update(replacement)
                self.assertFalse(query._matches(events[0], altered))
            extra = response(events[0])
            extra["data"]["properties"]["server_extra"] = "allowed"
            self.assertTrue(query._matches(events[0], extra))
            self.assertEqual(raindrop._read(folder / "delivery.json")["events"]["status"], "unknown")

    def test_both_payload_hashes_project_and_unique_ids_checked_before_get(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, events = outbox(directory)
            transport = MagicMock()
            for name in ("events", "signals"):
                path = folder / (name + ".json")
                original = path.read_bytes()
                altered = raindrop._read(path)
                altered[0]["event_id"] += "-tampered"
                write_json(path, altered)
                with self.assertRaises(raindrop.RaindropError):
                    query.verify_outbox(folder, CONFIG, transport)
                path.write_bytes(original)
            with self.assertRaises(raindrop.RaindropError) as raised:
                query.verify_outbox(folder, {**CONFIG, "RAINDROP_PROJECT_ID": "another-project"}, transport)
            self.assertEqual(raised.exception.code, "delivery_project_changed")
            events[1]["event_id"] = events[0]["event_id"]
            write_json(folder / "events.json", events)
            manifest, state = raindrop._read(folder / "manifest.json"), raindrop._read(folder / "delivery.json")
            manifest["events_sha256"] = state["events"]["sha256"] = raindrop._digest(events)
            write_json(folder / "manifest.json", manifest)
            write_json(folder / "delivery.json", state)
            with self.assertRaises(raindrop.RaindropError) as raised:
                query.verify_outbox(folder, CONFIG, transport)
            self.assertEqual(raised.exception.code, "event_identity_invalid")
            transport.assert_not_called()

    def test_resume_receipt_cannot_cross_outbox_or_project_and_final_precheck_detects_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, events = outbox(directory)
            transport = MagicMock(side_effect=[response(events[0]), raindrop.RaindropError("network", "timeout")])
            query.verify_outbox(folder, CONFIG, transport)
            receipt_path = folder / query.RECEIPT_NAME
            receipt = raindrop._read(receipt_path)
            receipt["project_id"] = "other-project"
            write_json(receipt_path, receipt)
            fresh = reader(events)
            with self.assertRaises(raindrop.RaindropError) as raised:
                query.verify_outbox(folder, CONFIG, fresh)
            self.assertEqual(raised.exception.code, "query_receipt_changed")
            fresh.assert_not_called()
            receipt_path.unlink()

            def mutating_read(identity, config):
                state = raindrop._read(folder / "delivery.json")
                state["events"]["error"] = {"category": "local", "code": "manual_edit"}
                write_json(folder / "delivery.json", state)
                return response(next(event for event in events if event["event_id"] == identity))

            with self.assertRaises(raindrop.RaindropError) as raised:
                query.verify_outbox(folder, CONFIG, mutating_read)
            self.assertEqual(raised.exception.code, "outbox_changed_during_query")
            self.assertEqual(raindrop._read(folder / "delivery.json")["events"]["status"], "unknown")


class QueryTransportTests(unittest.TestCase):
    def test_variable_name_placeholders_are_missing_without_any_request(self):
        config, missing = query._configuration_values({**CONFIG, "RAINDROP_QUERY_API_KEY": "RAINDROP_QUERY_API_KEY"})
        self.assertIsNone(config)
        self.assertEqual(missing, ["RAINDROP_QUERY_API_KEY"])
        config, missing = raindrop._configuration_values({"RAINDROP_WRITE_KEY": "RAINDROP_WRITE_KEY",
                                                        "RAINDROP_PROJECT_ID": "catalog-harness"})
        self.assertIsNone(config)
        self.assertEqual(missing, ["RAINDROP_WRITE_KEY"])

    def test_single_get_separate_query_key_bounded_body_no_redirect_no_post(self):
        opener = MagicMock()
        opened = opener.open.return_value.__enter__.return_value
        opened.status = 200
        opened.read.return_value = b'{"data": {"id": "event:1"}}'
        with patch.object(query, "build_opener", return_value=opener) as builder:
            self.assertEqual(query.get_event("event:1", CONFIG), {"data": {"id": "event:1"}})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, query.QUERY_URL + "event%3A1")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer private-query-key")
        self.assertEqual(request.get_header("X-raindrop-project-id"), "catalog-harness")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 30})
        self.assertEqual(opener.open.call_count, 1)
        opened.read.assert_called_once_with(query.MAX_BYTES + 1)
        redirect = builder.call_args.args[0]
        self.assertIsInstance(redirect, raindrop.NoRedirect)
        self.assertIsNone(redirect.redirect_request(request, None, 302, "private", {}, "https://other.invalid"))

    def test_auth_404_network_errors_are_sanitized_and_never_retry_or_read_error_body(self):
        for source, category, code in ((HTTPError("private-url", 401, "private-key", {}, io.BytesIO(b"private")), "authentication", "http_401"),
                                       (HTTPError("private-url", 404, "private-key", {}, io.BytesIO(b"private")), "not_found", "http_404"),
                                       (URLError("private-address"), "network", "connection")):
            opener = MagicMock()
            opener.open.side_effect = source
            with patch.object(query, "build_opener", return_value=opener), self.assertRaises(raindrop.RaindropError) as raised:
                query.get_event("event-1", CONFIG)
            self.assertEqual((raised.exception.category, raised.exception.code), (category, code))
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(opener.open.call_count, 1)

    def test_bounded_response_bad_json_and_non_200_fail_without_retry(self):
        for status, body, code in ((200, b"x" * (query.MAX_BYTES + 1), "body_limit"),
                                   (200, b"private invalid", "invalid_json"),
                                   (200, b"[]", "invalid_shape"), (202, b"{}", "unexpected_status")):
            opener = MagicMock()
            opened = opener.open.return_value.__enter__.return_value
            opened.status, opened.read.return_value = status, body
            with patch.object(query, "build_opener", return_value=opener), self.assertRaises(raindrop.RaindropError) as raised:
                query.get_event("event-1", CONFIG)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(opener.open.call_count, 1)

    def test_query_configuration_precedence_and_missing_names_only(self):
        with patch.object(query.os, "environ", {"RAINDROP_QUERY_API_KEY": "process-private-key"}), \
                patch.object(query, "dotenv_values", return_value={**CONFIG, "RAINDROP_WRITE_KEY": "wrong-private-key"}):
            config, missing = query.configuration()
        self.assertEqual(config, {**CONFIG, "RAINDROP_QUERY_API_KEY": "process-private-key"})
        self.assertEqual(missing, [])
        output = io.StringIO()
        with patch.object(query, "configuration", return_value=(None, ["RAINDROP_QUERY_API_KEY"])), \
                patch.object(query, "get_event") as network, redirect_stdout(output):
            self.assertEqual(query.main(["verify"]), 1)
        self.assertEqual(output.getvalue().strip(), "Missing: RAINDROP_QUERY_API_KEY")
        network.assert_not_called()
        with self.assertRaises(raindrop.RaindropError):
            query._configuration_values({**CONFIG, "RAINDROP_PROJECT_ID": "default"})


if __name__ == "__main__":
    unittest.main()
