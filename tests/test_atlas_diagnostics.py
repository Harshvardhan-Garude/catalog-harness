"""Read-only Atlas diagnostics with all configuration and network mocked."""

import errno
import io
import json
import logging
import os
import socket
import ssl
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dns.exception
import dns.resolver
from pymongo.errors import (ConfigurationError, InvalidURI, NetworkTimeout,
                            OperationFailure, ServerSelectionTimeoutError)

from enrichment import atlas_diagnostics as diagnostics


SECRET = "private-credential-or-host-marker"


def topology_client(errors=()):
    client = MagicMock()
    client.topology_description.server_descriptions.return_value = {
        (SECRET + str(index), 27017): SimpleNamespace(error=error)
        for index, error in enumerate(errors)}
    return client


class SanitizedAtlasErrorTests(unittest.TestCase):
    def test_tls_causes_include_suppressed_exception_context(self):
        inner = ssl.SSLCertVerificationError(1, SECRET)
        wrapper = ConfigurationError(SECRET)
        wrapper.__context__ = inner
        wrapper.__suppress_context__ = True
        self.assertEqual(diagnostics.sanitized_error(wrapper),
                         {"category": "tls", "code": "certificate_verification_failed"})
        tls = ssl.SSLError(1, SECRET)
        tls.reason = "TLSV1_ALERT_INTERNAL_ERROR"
        outer = ServerSelectionTimeoutError(SECRET)
        outer.__cause__ = tls
        self.assertEqual(diagnostics.sanitized_error(outer), {"category": "tls", "code": "peer_internal_alert"})

    def test_unknown_tls_reason_is_replaced_by_static_code(self):
        error = ssl.SSLError(1, SECRET)
        error.reason = SECRET
        self.assertEqual(diagnostics.sanitized_error(error), {"category": "tls", "code": "handshake_failed"})

    def test_dns_and_authentication_codes_are_typed_and_sanitized(self):
        examples = [(socket.gaierror(-2, SECRET), {"category": "dns", "code": "resolution_failed"}),
                    (dns.resolver.NXDOMAIN(), {"category": "dns", "code": "resolution_failed"}),
                    (dns.resolver.NoAnswer(), {"category": "dns", "code": "resolution_failed"}),
                    (dns.exception.Timeout(), {"category": "dns", "code": "timeout"}),
                    (OperationFailure(SECRET, code=13), {"category": "atlas", "code": "permission_denied"}),
                    (OperationFailure(SECRET, code=18), {"category": "atlas", "code": "authentication_failed"})]
        for error, expected in examples:
            with self.subTest(kind=type(error).__name__, code=getattr(error, "code", None)):
                self.assertEqual(diagnostics.sanitized_error(error), expected)
                self.assertNotIn(SECRET, json.dumps(diagnostics.sanitized_error(error)))

    def test_errno_timeout_and_outer_fallbacks_never_emit_exception_text(self):
        examples = [(OSError(errno.ECONNREFUSED, SECRET), "network", "connection_refused"),
                    (OSError(errno.ECONNRESET, SECRET), "network", "connection_reset"),
                    (OSError(errno.ENETUNREACH, SECRET), "network", "network_unreachable"),
                    (OSError(errno.EHOSTUNREACH, SECRET), "network", "host_unreachable"),
                    (OSError(errno.ETIMEDOUT, SECRET), "network", "timeout"),
                    (NetworkTimeout(SECRET), "network", "timeout"),
                    (TimeoutError(SECRET), "network", "timeout"),
                    (InvalidURI(SECRET), "configuration", "invalid_uri"),
                    (ConfigurationError(SECRET), "configuration", "invalid_configuration"),
                    (ServerSelectionTimeoutError(SECRET), "atlas", "server_selection_timeout"),
                    (RuntimeError(SECRET), "atlas", "operation_failed")]
        for error, category, code in examples:
            with self.subTest(kind=type(error).__name__, code=code):
                self.assertEqual(diagnostics.sanitized_error(error), {"category": category, "code": code})

    def test_topology_aggregates_errors_without_addresses_or_messages(self):
        client = topology_client([socket.timeout(SECRET), socket.timeout(SECRET),
                                  ssl.SSLCertVerificationError(1, SECRET), None])
        report = diagnostics.server_failures(client)
        self.assertEqual(report, {"discovered_servers": 4, "server_failures": [
            {"category": "network", "code": "timeout", "count": 2},
            {"category": "tls", "code": "certificate_verification_failed", "count": 1}]})
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn("27017", json.dumps(report))


class ConfigurationMetadataTests(unittest.TestCase):
    def load_file(self, values):
        def load(path, override=False):
            self.assertFalse(override)
            for name, value in values.items():
                if value is not None:
                    os.environ.setdefault(name, value)
            return True
        return load

    def test_process_overrides_file_without_emitting_either_value(self):
        values = {"MONGODB_URI": "file-" + SECRET, "MONGODB_DATABASE": "database-" + SECRET}
        with patch.dict(os.environ, {"MONGODB_URI": "process-" + SECRET}, clear=True), \
                patch.object(diagnostics, "dotenv_values", return_value=values) as reader, \
                patch.object(diagnostics, "load_dotenv", side_effect=self.load_file(values)) as loader:
            metadata = diagnostics.configuration_metadata()
        self.assertEqual(metadata["MONGODB_URI"], {"file_present": True, "process_present": True,
            "effective_usable": True, "effective_source": "process_environment", "process_matches_file": False})
        self.assertEqual(metadata["MONGODB_DATABASE"], {"file_present": True, "process_present": False,
            "effective_usable": True, "effective_source": "project_dotenv", "process_matches_file": None})
        self.assertNotIn(SECRET, json.dumps(metadata))
        reader.assert_called_once_with(diagnostics.ROOT / ".env", interpolate=False)
        loader.assert_called_once_with(diagnostics.ROOT / ".env", override=False)

    def test_empty_process_value_and_placeholders_are_unusable(self):
        values = {"MONGODB_URI": "file-" + SECRET, "MONGODB_DATABASE": "YOUR_DATABASE"}
        with patch.dict(os.environ, {"MONGODB_URI": " "}, clear=True), \
                patch.object(diagnostics, "dotenv_values", return_value=values), \
                patch.object(diagnostics, "load_dotenv", side_effect=self.load_file(values)):
            metadata = diagnostics.configuration_metadata()
        self.assertFalse(metadata["MONGODB_URI"]["effective_usable"])
        self.assertEqual(metadata["MONGODB_URI"]["effective_source"], "process_environment")
        self.assertFalse(metadata["MONGODB_DATABASE"]["effective_usable"])

    def test_interpolated_file_values_do_not_claim_comparability(self):
        values = {"MONGODB_URI": "${PRIVATE_VALUE}", "MONGODB_DATABASE": "db"}
        with patch.dict(os.environ, {"MONGODB_URI": SECRET}, clear=True), \
                patch.object(diagnostics, "dotenv_values", return_value=values), \
                patch.object(diagnostics, "load_dotenv", side_effect=self.load_file(values)):
            metadata = diagnostics.configuration_metadata()
        self.assertIsNone(metadata["MONGODB_URI"]["process_matches_file"])


class DiagnosticMainTests(unittest.TestCase):
    def setUp(self):
        previous_disable = logging.root.manager.disable
        self.addCleanup(logging.disable, previous_disable)

    def usable_metadata(self):
        return {name: {"effective_usable": True} for name in ("MONGODB_URI", "MONGODB_DATABASE")}

    def test_missing_configuration_prints_names_and_makes_no_client(self):
        metadata = {"MONGODB_URI": {"effective_usable": False}, "MONGODB_DATABASE": {"effective_usable": False}}
        output = io.StringIO()
        with patch.object(diagnostics, "configuration_metadata", return_value=metadata), \
                patch.object(diagnostics, "MongoClient") as factory, redirect_stdout(output):
            status = diagnostics.main()
        report = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["missing"], ["MONGODB_URI", "MONGODB_DATABASE"])
        self.assertEqual(report["model_calls"], 0)
        factory.assert_not_called()

    def test_success_is_one_bounded_read_only_ping_and_suppresses_driver_logs(self):
        client = topology_client([None, None, None])

        def ping(command):
            logging.getLogger("pymongo.topology").critical(SECRET)
            return {"ok": 1}

        client.admin.command.side_effect = ping
        output, error_output = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"MONGODB_URI": "mongodb+srv://" + SECRET}, clear=True), \
                patch.object(diagnostics, "configuration_metadata", return_value=self.usable_metadata()), \
                patch.object(diagnostics, "MongoClient", return_value=client) as factory, \
                redirect_stdout(output), redirect_stderr(error_output):
            status = diagnostics.main()
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["discovered_servers"], 3)
        self.assertEqual(report["server_failures"], [])
        self.assertEqual(report["model_calls"], 0)
        client.admin.command.assert_called_once_with("ping")
        client.close.assert_called_once()
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs, {"serverSelectionTimeoutMS": 10000,
            "connectTimeoutMS": 10000, "socketTimeoutMS": 10000, "timeoutMS": 20000,
            "appname": "catalog-harness-diagnostic"})
        self.assertNotIn(SECRET, output.getvalue() + error_output.getvalue())

    def test_tls_failure_reports_static_error_and_topology_counts_then_closes(self):
        nested = ssl.SSLCertVerificationError(1, SECRET)
        wrapper = ServerSelectionTimeoutError(SECRET)
        wrapper.__context__ = nested
        wrapper.__suppress_context__ = True
        client = topology_client([nested, nested, nested])
        client.admin.command.side_effect = wrapper
        output = io.StringIO()
        with patch.dict(os.environ, {"MONGODB_URI": "mongodb+srv://" + SECRET}, clear=True), \
                patch.object(diagnostics, "configuration_metadata", return_value=self.usable_metadata()), \
                patch.object(diagnostics, "MongoClient", return_value=client), redirect_stdout(output):
            status = diagnostics.main()
        report = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(report["error"], {"category": "tls", "code": "certificate_verification_failed"})
        self.assertEqual(report["server_failures"], [{"category": "tls", "code": "certificate_verification_failed", "count": 3}])
        self.assertNotIn(SECRET, output.getvalue())
        client.admin.command.assert_called_once_with("ping")
        client.close.assert_called_once()

    def test_unavailable_topology_never_exposes_secondary_exception(self):
        client = topology_client()
        client.admin.command.side_effect = socket.timeout(SECRET)
        client.topology_description.server_descriptions.side_effect = RuntimeError(SECRET)
        output = io.StringIO()
        with patch.dict(os.environ, {"MONGODB_URI": "mongodb+srv://" + SECRET}, clear=True), \
                patch.object(diagnostics, "configuration_metadata", return_value=self.usable_metadata()), \
                patch.object(diagnostics, "MongoClient", return_value=client), redirect_stdout(output):
            status = diagnostics.main()
        report = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertFalse(report["topology_diagnostics_available"])
        self.assertEqual(report["error"], {"category": "network", "code": "timeout"})
        self.assertNotIn(SECRET, output.getvalue())

    def test_client_close_failure_is_sanitized_without_a_traceback(self):
        client = topology_client([None])
        client.close.side_effect = RuntimeError(SECRET)
        output = io.StringIO()
        with patch.dict(os.environ, {"MONGODB_URI": "mongodb+srv://" + SECRET}, clear=True), \
                patch.object(diagnostics, "configuration_metadata", return_value=self.usable_metadata()), \
                patch.object(diagnostics, "MongoClient", return_value=client), redirect_stdout(output):
            status = diagnostics.main()
        report = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(report["close_error"], {"category": "atlas", "code": "operation_failed"})
        self.assertNotIn(SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()
