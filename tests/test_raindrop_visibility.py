"""Read-only visibility checks, mocked HTTP and immutable source artifacts."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from enrichment import raindrop, raindrop_monitoring as monitoring, raindrop_query as query
from enrichment import raindrop_visibility as visibility
from enrichment.budget import write_json


CONFIG = {"RAINDROP_QUERY_API_KEY": "private-query-key", "RAINDROP_PROJECT_ID": "catalog-harness"}
RUNS = [("final_holdout", "baseline", "final"), ("final_holdout", "candidate", "final"),
        ("development_validation", "baseline", "development"),
        ("development_validation", "candidate", "development"),
        ("development_validation", "baseline", "validation"),
        ("development_validation", "candidate", "validation")]
ANCHORS = (0, 6, 12, 16, 20, 24)


def response(event, names):
    properties = copy.deepcopy(event["properties"])
    for name in query.OPTIONAL_NESTED:
        properties.pop(name)
    return {"data": {"id": event["event_id"], "event_name": event["event"], "user_id": event["user_id"],
        "convo_id": event["ai_data"]["convo_id"], "model": event["ai_data"]["model"],
        "user_input": event["ai_data"]["input"], "assistant_output": event["ai_data"]["output"],
        "properties": properties, "signals": [{"id": "signal-{}".format(i), "name": name.replace("_", " ").title()}
                                                for i, name in enumerate(sorted(names))]}}


def fixture(root, include_monitoring=False):
    outbox = root / "artifacts/raindrop"
    outbox.mkdir(parents=True)
    events, signals = [], []
    for i in range(31):
        group = max(j for j, anchor in enumerate(ANCHORS) if anchor <= i)
        phase, variant, split = RUNS[group]
        request_config = {"store": False, "schema": {"type": "object"}, "requested_model": "gpt-4.1-mini"}
        props = {"phase": phase, "variant": variant, "split": split, "run_id": "run-{}".format(group),
                 "historical": True, "request_configuration": request_config,
                 "model_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}
        events.append({"event_id": "catalog-call-{}".format(i), "event": "semantic_enrichment_import",
            "user_id": "catalog-harness-demo", "properties": props,
            "ai_data": {"model": "gpt-4.1-mini-2025-04-14", "convo_id": props["run_id"],
                "input": json.dumps({**request_config, "instructions": "private saved prompt", "input_text": "private product"}),
                "output": '{"products": []}'}})
    for group, anchor in enumerate(ANCHORS):
        phase, variant, split = RUNS[group]
        counts = {key: 0 for key in raindrop.COUNT_KEYS}
        counts.update(total_expected=24, total_field_decisions=72, correct_field_decisions=60,
                      incorrect_field_decisions=12, unsupported_assertions=1, unnecessary_abstentions=2)
        signals.append({"event_id": events[anchor]["event_id"], "signal_name": "synthetic_enrichment_evaluation",
            "signal_type": "default", "properties": {"scope": "variant_aggregate", "phase": phase,
                "phase_id": "saved-phase", "variant": variant, "split": split, "run_id": "run-{}".format(group),
                "historical": True, "synthetic_fixture": True, "counts": counts,
                "usage": {"input_tokens": 600, "output_tokens": 120, "total_tokens": 720}, "model_calls": 6}})
    manifest = {"version": 1, "kind": "saved_synthetic_raindrop_import", "events": 31, "signals": 6,
        "events_sha256": raindrop._digest(events), "signals_sha256": raindrop._digest(signals)}
    receipt = {"version": 2, "project_id": CONFIG["RAINDROP_PROJECT_ID"],
        "manifest_sha256": raindrop._digest(manifest), "events_sha256": manifest["events_sha256"],
        "signals_sha256": manifest["signals_sha256"], "verified": {event["event_id"]: {
            "expected_sha256": raindrop._digest(event), "response_sha256": "a" * 64,
            "verified_at": "2026-10-11T22:00:00Z", "coverage": query._coverage_record(query.OPTIONAL_NESTED)}
            for event in events}}
    flat = raindrop._flat_signals(signals)
    links = {"status": "verified", "expected_links": 6, "verified_links": 6,
        "events_sha256": manifest["events_sha256"], "signals_sha256": raindrop._digest(flat),
        "verified": {signal["event_id"]: {"verified": True, "matching_signal_count": 1,
            "response_sha256": "b" * 64} for signal in signals}}
    state = {"version": 1, "project_id": CONFIG["RAINDROP_PROJECT_ID"], "events": {
        "status": "sent", "sha256": manifest["events_sha256"], "query_verification": {
            "method": "GET", "events": 31, "core_events_verified": True,
            "events_sha256": manifest["events_sha256"], "signals_sha256": manifest["signals_sha256"],
            "receipt_sha256": raindrop._digest(receipt)}},
        "signals": {"status": "superseded", "sha256": manifest["signals_sha256"], "superseded_by": "signals_wire"},
        "signals_wire": {"status": "sent", "format": "flat-metrics-v1", "http_status": 204,
            "sha256": raindrop._digest(flat), "source_sha256": manifest["signals_sha256"],
            "events_sha256": manifest["events_sha256"]}}
    for name, data in (("events", events), ("signals", signals), ("manifest", manifest), ("delivery", state),
                       ("query-receipt", receipt), ("signal-link-receipt", links), ("signals-flat-v1", flat)):
        write_json(outbox / (name + ".json"), data)
    if include_monitoring:
        monitoring.prepare(outbox, root / "artifacts/raindrop-monitoring-v1")
    return outbox, events, signals


def transports(events, signals, monitor_rows=()):
    expected = {row["signal_name"] for row in signals + list(monitor_rows)}
    definitions = {"data": [{"id": "custom-{}".format(i), "name": name.replace("_", " ").title(),
                             "type": "instrumented"} for i, name in enumerate(sorted(expected))] + [
        {"id": "unrelated", "name": "Person private-email@example.test", "type": "code"}],
        "meta": {"has_more": False, "cursor": None}}
    request = MagicMock(side_effect=lambda kind, config: copy.deepcopy(definitions) if kind == "signals" else {"data": {"total": 31}})
    by_id = {event["event_id"]: event for event in events}
    links = {row["event_id"]: {row["signal_name"]} for row in signals}
    for row in monitor_rows:
        links[row["event_id"]].add(row["signal_name"])
    reader = MagicMock(side_effect=lambda identity, config: response(by_id[identity], links[identity]))
    return request, reader


class VisibilityTests(unittest.TestCase):
    def run_check(self, root, events, signals, request, reader, **kwargs):
        with patch.object(visibility, "_phase", side_effect=lambda root, final:
                          (events[:12], signals[:2]) if final else (events[12:], signals[2:])):
            return visibility.check_visibility(root, config=CONFIG, request=request, event_transport=reader, **kwargs)

    def test_eight_fresh_gets_six_core_proofs_preserve_every_original_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outbox, events, signals = fixture(root)
            before = {path.name: path.read_bytes() for path in outbox.iterdir() if path.is_file()}
            request, reader = transports(events, signals)
            with patch.object(raindrop, "post_array") as post:
                result = self.run_check(root, events, signals, request, reader)
                repeated = self.run_check(root, events, signals, request, reader)
                post.assert_not_called()
            self.assertEqual((result["status"], result["total"], result["network_gets_added"]), ("verified", 31, 8))
            self.assertEqual(result["fresh_core_events_verified"], 6)
            self.assertEqual(result["prior_core_events_verified"], 31)
            self.assertEqual(len(result["aggregate_links"]), 6)
            self.assertTrue(result["core_content_matches"])
            self.assertFalse(result["nested_metadata_verified"])
            self.assertTrue(result["local_usage_only"])
            self.assertFalse(result["signal_properties_verified"])
            self.assertFalse(result["historical_costs_backfilled"])
            self.assertEqual(result["missing_nested_metadata"], {"model_usage": 6, "request_configuration": 6})
            self.assertEqual(repeated["network_gets_added"], 8)
            self.assertEqual((request.call_count, reader.call_count), (4, 12))
            self.assertNotIn("private", json.dumps(result))
            self.assertNotIn("catalog-call", json.dumps(result))
            self.assertEqual(before, {path.name: path.read_bytes() for path in outbox.iterdir() if path.is_file()})
            self.assertFalse((root / visibility.RECEIPT).exists())

    def test_monitoring_names_are_verified_per_anchor_and_missing_links_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unused, events, signals = fixture(root, include_monitoring=True)
            rows = raindrop._read(root / "artifacts/raindrop-monitoring-v1/signals.json")
            request, reader = transports(events, signals, rows)
            result = self.run_check(root, events, signals, request, reader, save_receipt=True)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["monitoring_signals_expected"], len(rows))
            self.assertEqual(result["monitoring_status"], "pending")
            self.assertEqual(raindrop._read(root / visibility.RECEIPT), result)
            self.assertTrue(all(len(row["matched_signal_names"]) > 1 for row in result["aggregate_links"]))
            request, reader = transports(events, signals, rows)
            first = response(events[0], {signals[0]["signal_name"]})
            original_reader = reader.side_effect
            reader.side_effect = lambda identity, config: first if identity == events[0]["event_id"] else original_reader(identity, config)
            result = self.run_check(root, events, signals, request, reader)
            self.assertEqual(result["status"], "unverified")
            self.assertTrue(result["aggregate_links"][0]["missing_signal_names"])

    def test_count_missing_definition_wrong_type_or_core_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unused, events, signals = fixture(root)
            for case in ("count", "definition", "type", "core"):
                request, reader = transports(events, signals)
                if case == "count":
                    original = request.side_effect
                    request.side_effect = lambda kind, config: {"data": {"total": 32}} if kind == "count" else original(kind, config)
                elif case in ("definition", "type"):
                    listing = request("signals", CONFIG)
                    request.reset_mock()
                    if case == "definition":
                        listing["data"] = listing["data"][-1:]
                    else:
                        listing["data"][0]["type"] = "code"
                    request.side_effect = lambda kind, config: listing if kind == "signals" else {"data": {"total": 31}}
                else:
                    altered = response(events[0], {signals[0]["signal_name"]})
                    altered["data"]["assistant_output"] = "private incorrect output"
                    reader.side_effect = None
                    reader.return_value = altered
                result = self.run_check(root, events, signals, request, reader)
                self.assertEqual(result["status"], "unverified", case)
                self.assertNotIn("private", json.dumps(result))
                if case == "core":
                    self.assertEqual(result["fresh_core_events_verified"], 0)
                    self.assertEqual(result["error"]["code"], "event_content_mismatch")
                    self.assertEqual(reader.call_count, 1)

    def test_project_hash_source_and_monitoring_mutations_prevent_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outbox, events, signals = fixture(root, include_monitoring=True)
            request, reader = transports(events, signals)
            target = outbox / "events.json"
            original = target.read_bytes()
            altered = copy.deepcopy(events)
            altered[0]["event_id"] = "changed"
            write_json(target, altered)
            result = self.run_check(root, events, signals, request, reader)
            self.assertEqual(result["error"]["code"], "outbox_changed")
            target.write_bytes(original)
            bad_source = copy.deepcopy(events)
            bad_source[0]["ai_data"]["output"] = "different model response"
            result = self.run_check(root, bad_source, signals, request, reader)
            self.assertEqual(result["error"]["code"], "source_provenance_mismatch")
            target = root / "artifacts/raindrop-monitoring-v1/signals.json"
            data = raindrop._read(target)
            data[0]["properties"]["metric_value"] = 999
            write_json(target, data)
            result = self.run_check(root, events, signals, request, reader)
            self.assertEqual(result["error"]["code"], "immutable_monitoring_changed")
            result = visibility.check_visibility(root, config={**CONFIG, "RAINDROP_PROJECT_ID": "different-project"},
                                                 request=request, event_transport=reader)
            self.assertEqual(result["error"]["code"], "delivery_project_changed")
            request.assert_not_called()
            reader.assert_not_called()

    def test_missing_configuration_prints_names_only_and_network_failure_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unused, events, signals = fixture(root)
            request, reader = transports(events, signals)
            result = visibility.check_visibility(root, config={}, request=request, event_transport=reader)
            self.assertEqual(result["status"], "missing_configuration")
            self.assertEqual(result["missing"], list(query.CONFIG_NAMES))
            request.assert_not_called()
            request.side_effect = raindrop.RaindropError("authentication", "http_401")
            result = self.run_check(root, events, signals, request, reader)
            self.assertEqual(result["error"], {"category": "authentication", "code": "http_401"})
            self.assertEqual(result["network_gets_added"], 1)
            reader.assert_not_called()
            with patch.object(visibility, "check_visibility", return_value=result), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(visibility.main(["check"]), 1)
            self.assertNotIn(CONFIG["RAINDROP_QUERY_API_KEY"], output.getvalue())


class TransportTests(unittest.TestCase):
    def test_allowlisted_gets_timeout_body_limit_no_redirect_and_no_retry(self):
        for kind in ("signals", "count"):
            response_mock = MagicMock()
            response_mock.__enter__.return_value = response_mock
            response_mock.status = 200
            response_mock.read.return_value = b'{"data": {"total": 31}}'
            opener = MagicMock()
            opener.open.return_value = response_mock
            with patch.object(visibility, "build_opener", return_value=opener) as build:
                self.assertEqual(visibility.get_json(kind, CONFIG)["data"]["total"], 31)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(opener.open.call_args.kwargs, {"timeout": 30})
            self.assertIsInstance(build.call_args.args[0], raindrop.NoRedirect)
            response_mock.read.assert_called_once_with(raindrop.MAX_BYTES + 1)
            self.assertIn(visibility.QUERY_PATHS[kind], request.full_url)
            opener.open.assert_called_once()
        with patch.object(visibility, "build_opener") as build:
            with self.assertRaises(raindrop.RaindropError):
                visibility.get_json("signals/track", CONFIG)
            build.assert_not_called()

    def test_auth_errors_bounded_responses_and_invalid_shapes_are_sanitized(self):
        error = HTTPError("https://private.example", 401, "private token", {}, io.BytesIO(b"private"))
        opener = MagicMock()
        opener.open.side_effect = error
        with patch.object(visibility, "build_opener", return_value=opener):
            with self.assertRaises(raindrop.RaindropError) as raised:
                visibility.get_json("signals", CONFIG)
        self.assertEqual(str(raised.exception), "authentication/http_401")
        opener.open.assert_called_once()
        for body, expected in ((b"x" * (raindrop.MAX_BYTES + 1), "body_limit"), (b"private invalid json", "invalid_json"),
                               (b"[]", "invalid_shape")):
            response_mock = MagicMock()
            response_mock.__enter__.return_value = response_mock
            response_mock.status, response_mock.read.return_value = 200, body
            opener = MagicMock()
            opener.open.return_value = response_mock
            with patch.object(visibility, "build_opener", return_value=opener):
                with self.assertRaises(raindrop.RaindropError) as raised:
                    visibility.get_json("signals", CONFIG)
            self.assertEqual(raised.exception.code, expected)
        for data in ({"data": {}}, {"data": [], "meta": {"has_more": "false"}},
                     {"data": [{"id": "id", "name": "private", "type": "unknown"}], "meta": {"has_more": False}}):
            with self.assertRaises(raindrop.RaindropError):
                visibility._definitions(data)


if __name__ == "__main__":
    unittest.main()
