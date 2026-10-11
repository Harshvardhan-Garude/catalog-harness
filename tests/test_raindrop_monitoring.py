"""Offline monitoring derivation, source-proof validation and write fencing."""

import copy
import fcntl
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from enrichment import raindrop, raindrop_monitoring as monitoring, raindrop_query
from tests.test_raindrop import CONFIG, phases, write


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        phases(self.root)
        metrics = {
            "baseline": (2, 2, 6, 4), "candidate": (1, 2, 0, 3),
            "baseline_development": (4, 0, 0, 4), "baseline_validation": (4, 1, 3, 3),
            "candidate_development": (3, 1, 0, 4), "candidate_validation": (0, 2, 0, 2),
        }
        for folder in ("enrichment-v1", "enrichment-v1-final"):
            path = self.root / "artifacts" / folder / "report.json"
            report = raindrop._read(path)
            for name, run in report["runs"].items():
                counts = run["overall"]
                for key, value in zip(("unsupported_assertions", "unnecessary_abstentions",
                                       "evidence_issue_count", "incorrect_field_decisions"), metrics[name]):
                    counts[key] = value
                counts["correct_field_decisions"] = counts["total_field_decisions"] - counts["incorrect_field_decisions"]
            write(path, report)
        self.source = Path(raindrop.export_outbox(self.root, include_historical=True)["outbox"])
        self.directory = self.root / "monitoring"
        state = raindrop._read(self.source / "delivery.json")
        state["project_id"] = CONFIG["RAINDROP_PROJECT_ID"]
        state["events"]["status"] = "unknown"
        write(self.source / "delivery.json", state)
        events = {event["event_id"]: event for event in raindrop._read(self.source / "events.json")}

        def get_event(identity, config):
            event = events[identity]
            return {"data": {"id": identity, "event_name": event["event"], "user_id": event["user_id"],
                             "convo_id": event["ai_data"]["convo_id"], "model": event["ai_data"]["model"],
                             "user_input": event["ai_data"]["input"], "assistant_output": event["ai_data"]["output"],
                             "properties": copy.deepcopy(event["properties"])}}

        query_config = {"RAINDROP_PROJECT_ID": CONFIG["RAINDROP_PROJECT_ID"],
                        "RAINDROP_QUERY_API_KEY": "offline-fixture-query-key"}
        self.assertEqual(raindrop_query.verify_outbox(self.source, query_config, get_event)["status"], "verified")
        raindrop.send_signals(self.source, CONFIG, MagicMock(return_value={"http_status": 204}))
        state = raindrop._read(self.source / "delivery.json")
        anchors = [signal["event_id"] for signal in raindrop._read(self.source / "signals.json")]
        write(self.source / "signal-link-receipt.json", {
            "status": "verified", "expected_links": 6, "verified_links": 6,
            "events_sha256": state["events"]["sha256"], "signals_sha256": state["signals_wire"]["sha256"],
            "verified": {identity: {"verified": True, "matching_signal_count": 1, "response_sha256": "a" * 64}
                         for identity in anchors}})

    def prepare(self):
        return monitoring.prepare(self.source, self.directory)

    def test_exact_flags_scalar_properties_aggregate_scope_and_idempotent_preparation(self):
        originals = {path.name: path.read_bytes() for path in self.source.iterdir()}
        with patch.object(monitoring, "post_array") as network:
            result = self.prepare()
            self.assertEqual(self.prepare(), result)
            self.assertEqual(monitoring.status(self.source, self.directory), result)
            network.assert_not_called()
        self.assertEqual(result["signals"], 22)
        self.assertEqual(result["counts_by_signal"], {
            "aggregate_unsupported_assertions_present": 5,
            "aggregate_unnecessary_abstentions_present": 5,
            "aggregate_evidence_issues_present": 2, "aggregate_incorrect_fields_present": 6,
            "aggregate_evidence_checks_clean": 4})
        payload = raindrop._read(self.directory / "signals.json")
        for signal in payload:
            self.assertEqual(signal["signal_type"], "default")
            properties = signal["properties"]
            self.assertEqual(properties["scope"], "variant_aggregate")
            self.assertEqual(properties["event_role"], "run_anchor_not_individual_call_attribution")
            self.assertTrue(all(type(value) in (str, int, bool) for value in properties.values()))
            self.assertNotIn("input", properties)
            self.assertNotIn("output", properties)
            if signal["signal_name"] == monitoring.EVIDENCE_CLEAN:
                self.assertEqual(signal["sentiment"], "POSITIVE")
                self.assertEqual(properties["metric_value"], 0)
                self.assertIn("does not imply semantic correctness", properties["meaning"])
            else:
                self.assertEqual(signal["sentiment"], "NEGATIVE")
                self.assertGreater(properties["metric_value"], 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.source.iterdir()}, originals)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        for filename in ("manifest.json", "signals.json", "delivery.json"):
            self.assertEqual((self.directory / filename).stat().st_mode & 0o777, 0o600)

    def test_durable_reservation_before_one_post_sent_skips_and_original_files_unchanged(self):
        self.prepare()
        originals = {path.name: path.read_bytes() for path in self.source.iterdir()}

        def transport(endpoint, payload, config):
            self.assertEqual(endpoint, "signals")
            self.assertEqual(len(payload), 22)
            self.assertEqual(raindrop._read(self.directory / "delivery.json")["status"], "reserved")
            self.assertGreaterEqual(fsync.call_count, 2)
            return {"http_status": 204}

        with patch.object(monitoring.os, "fsync", wraps=monitoring.os.fsync) as fsync:
            sender = MagicMock(side_effect=transport)
            result = monitoring.send(self.source, self.directory, CONFIG, sender)
        self.assertEqual(result["network_posts_added"], 1)
        self.assertEqual(result["status"], "sent")
        self.assertFalse(result["signal_links_verified"])
        self.assertEqual(monitoring.send(self.source, self.directory, CONFIG, sender)["network_posts_added"], 0)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual({path.name: path.read_bytes() for path in self.source.iterdir()}, originals)

    def test_missing_proof_or_unsent_original_signals_blocks_preparation(self):
        state_path = self.source / "delivery.json"
        original = raindrop._read(state_path)
        for mutate in (lambda state: state["events"]["query_verification"].update(events=30),
                       lambda state: state["signals_wire"].update(status="unknown")):
            state = copy.deepcopy(original)
            mutate(state)
            write(state_path, state)
            with self.assertRaises(raindrop.RaindropError):
                self.prepare()
            self.assertFalse(self.directory.exists())

    def test_receipt_change_or_payload_change_or_project_change_blocks_before_send(self):
        self.prepare()
        transport = MagicMock()
        changed = {**CONFIG, "RAINDROP_PROJECT_ID": "another-project"}
        with self.assertRaises(raindrop.RaindropError):
            monitoring.send(self.source, self.directory, changed, transport)
        path = self.directory / "signals.json"
        original = raindrop._read(path)
        payload = copy.deepcopy(original)
        payload[0]["sentiment"] = "POSITIVE"
        write(path, payload)
        with self.assertRaises(raindrop.RaindropError):
            monitoring.send(self.source, self.directory, CONFIG, transport)
        write(path, original)
        receipt_path = self.source / "signal-link-receipt.json"
        receipt = raindrop._read(receipt_path)
        receipt["checked_at"] = "changed-but-still-valid"
        write(receipt_path, receipt)
        with self.assertRaises(raindrop.RaindropError):
            monitoring.send(self.source, self.directory, CONFIG, transport)
        transport.assert_not_called()

    def test_cached_source_verification_refresh_does_not_invalidate_or_resend(self):
        self.prepare()
        path = self.source / "delivery.json"
        state = raindrop._read(path)
        state["events"]["query_verification"]["verified_at"] = "fresh-cached-verification-time"
        write(path, state)
        self.assertEqual(monitoring.status(self.source, self.directory)["status"], "pending")
        transport = MagicMock(return_value={"http_status": 204})
        monitoring.send(self.source, self.directory, CONFIG, transport)
        state["events"]["query_verification"]["verified_at"] = "another-cached-verification-time"
        write(path, state)
        self.assertEqual(monitoring.send(self.source, self.directory, CONFIG, transport)["network_posts_added"], 0)
        self.assertEqual(transport.call_count, 1)

    def test_numeric_type_payload_mutation_is_rejected_by_canonical_hash(self):
        self.prepare()
        path = self.directory / "signals.json"
        payload = raindrop._read(path)
        payload[0]["properties"]["metric_value"] = float(payload[0]["properties"]["metric_value"])
        write(path, payload)
        transport = MagicMock()
        with self.assertRaises(raindrop.RaindropError):
            monitoring.send(self.source, self.directory, CONFIG, transport)
        transport.assert_not_called()

    def test_timeout_permission_and_invalid_acknowledgement_fence_second_attempt(self):
        self.prepare()
        pending = raindrop._read(self.directory / "delivery.json")
        for side_effect, result, expected in (
                (raindrop.RaindropError("network", "timeout"), None, "unknown"),
                (raindrop.RaindropError("permission", "http_403"), None, "failed"),
                (RuntimeError("secret-raw-error"), None, "unknown"),
                (None, {"http_status": 302}, "unknown")):
            write(self.directory / "delivery.json", pending)
            transport = MagicMock(side_effect=side_effect, return_value=result)
            with self.assertRaises(raindrop.RaindropError) as error:
                monitoring.send(self.source, self.directory, CONFIG, transport)
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(raindrop._read(self.directory / "delivery.json")["status"], expected)
            with self.assertRaises(raindrop.RaindropError):
                monitoring.send(self.source, self.directory, CONFIG, transport)
            self.assertEqual(transport.call_count, 1)

    def test_reserved_crash_and_concurrent_delivery_fence_post(self):
        self.prepare()
        transport = MagicMock()
        with (self.directory / "delivery.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(raindrop.RaindropError) as error:
                monitoring.send(self.source, self.directory, CONFIG, transport)
            self.assertEqual(error.exception.code, "delivery_locked")
        state = raindrop._read(self.directory / "delivery.json")
        state["status"] = "reserved"
        write(self.directory / "delivery.json", state)
        with self.assertRaises(raindrop.RaindropError):
            monitoring.send(self.source, self.directory, CONFIG, transport)
        transport.assert_not_called()

    def test_safe_cli_summary_and_missing_names_only(self):
        self.prepare()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(monitoring.main(["status", "--outbox", str(self.source),
                                              "--directory", str(self.directory)]), 0)
        value = output.getvalue()
        for secret in (CONFIG["RAINDROP_WRITE_KEY"], CONFIG["RAINDROP_PROJECT_ID"], "event_id", "input_text"):
            self.assertNotIn(secret, value)
        with patch.object(monitoring, "post_array") as network:
            result = monitoring.send(self.source, self.directory, {})
            self.assertEqual(result["missing"], ["RAINDROP_WRITE_KEY", "RAINDROP_PROJECT_ID"])
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
