"""Offline contract tests: all HTTP and configuration sources are mocked."""

import copy
import io
import json
import socket
import ssl
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import check_openai
from catalog_harness import openai_http
from catalog_harness.__main__ import main as cli_main
from catalog_harness.openai_http import OpenAIError, request_json
from catalog_harness.openai_provider import OpenAIPlanProvider
from catalog_harness.pipeline import run_plan
from catalog_harness.policy import HarnessPolicy


USAGE = {"input_tokens": 45, "output_tokens": 8, "total_tokens": 53}
SCHEMA = check_openai.SCHEMA
PLAN = {"version": 1, "suppliers": {"supplier": {
    "fields": {"product_id": "sku", "name": "title", "weight": "weight", "unit": "unit"},
    "string_cleanup": ["strip", "collapse_whitespace"],
    "numeric_parser": "decimal_dot", "on_ambiguity": "quarantine"}}}
SAMPLES = [{"dataset_id": "catalog-v1-development-s1-n30", "supplier_id": "supplier",
            "source_record_id": "id", "payload": {"sku": "P1", "title": "Mug", "weight": 2, "unit": "kg"}}]
METADATA = [{"supplier_id": "supplier", "unit_documentation": "Explicit row units only."}]


def response_payload(value=None, **updates):
    payload = {"status": "completed", "model": "gpt-4.1-mini-2025-04-14", "usage": dict(USAGE),
               "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(
                   {"ok": True} if value is None else value)}]}]}
    payload.update(updates)
    return payload


def mocked_opener(payload):
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return opener


def small_request():
    return request_json("test-secret-key", schema=SCHEMA, schema_name="connectivity_check",
                        instructions="Return ok true.", input_text="Check.", max_output_tokens=128)


class OpenAIHTTPTests(unittest.TestCase):
    def test_one_bounded_post_with_strict_schema_and_no_redirects(self):
        opener = mocked_opener(response_payload())
        with patch.object(openai_http, "build_opener", return_value=opener) as builder:
            value, details = small_request()
        self.assertEqual(value, {"ok": True})
        self.assertEqual(details["usage"], USAGE)
        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(opener.open.call_args[1], {"timeout": 30})
        body = json.loads(request.data)
        self.assertEqual(body["model"], "gpt-4.1-mini")
        self.assertIs(body["store"], False)
        self.assertEqual(body["max_output_tokens"], 128)
        self.assertEqual(body["text"]["format"], {"type": "json_schema", "name": "connectivity_check",
                                                  "strict": True, "schema": SCHEMA})
        handler = builder.call_args[0][0]
        self.assertIsInstance(handler, openai_http.NoRedirect)
        self.assertIsNone(handler.redirect_request(Request(openai_http.ENDPOINT), None, 302, "redirect", {},
                                                   "https://other.example"))

    def test_output_and_input_budgets_prevent_requests(self):
        with patch.object(openai_http, "build_opener") as builder:
            for budget in (0, 2049, True):
                with self.subTest(budget=budget), self.assertRaises(OpenAIError) as raised:
                    request_json("key", schema=SCHEMA, schema_name="check", instructions="", input_text="",
                                 max_output_tokens=budget)
                self.assertEqual(raised.exception.code, "output_budget")
            with self.assertRaises(OpenAIError) as raised:
                request_json("key", schema=SCHEMA, schema_name="check", instructions="", input_text="x" * 65537,
                             max_output_tokens=128)
            self.assertEqual(raised.exception.code, "input_budget")
            builder.assert_not_called()

    def test_http_errors_are_sanitized_and_never_retried(self):
        for supplied_code, expected in (("invalid_api_key", "invalid_api_key"),
                                        ("secret-key-in-error-code", "http_401")):
            error = HTTPError(openai_http.ENDPOINT, 401, "secret-key-in-message", {}, io.BytesIO(json.dumps(
                {"error": {"code": supplied_code, "message": "secret-key-in-message"}}).encode()))
            opener = MagicMock()
            opener.open.side_effect = error
            with self.subTest(code=supplied_code), patch.object(openai_http, "build_opener", return_value=opener):
                with self.assertRaises(OpenAIError) as raised:
                    small_request()
                self.assertEqual((raised.exception.category, raised.exception.code), ("authentication", expected))
                self.assertNotIn("secret-key", str(raised.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_network_errors_are_sanitized_and_never_retried(self):
        for error, expected in ((socket.timeout("secret"), "timeout"),
                                (URLError(ssl.SSLError("secret")), "tls"),
                                (URLError("secret"), "connection")):
            opener = MagicMock()
            opener.open.side_effect = error
            with self.subTest(expected=expected), patch.object(openai_http, "build_opener", return_value=opener):
                with self.assertRaises(OpenAIError) as raised:
                    small_request()
                self.assertEqual((raised.exception.category, raised.exception.code), ("network", expected))
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_refusal_and_incomplete_preserve_usage(self):
        responses = [(response_payload(status="incomplete"), "incomplete"),
                     (response_payload(output=[{"type": "message", "content": [
                         {"type": "refusal", "refusal": "secret refusal text"}]}]), "refusal")]
        for payload, expected in responses:
            with self.subTest(expected=expected), patch.object(openai_http, "build_opener", return_value=mocked_opener(payload)):
                with self.assertRaises(OpenAIError) as raised:
                    small_request()
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(raised.exception.usage, USAGE)
                self.assertNotIn("secret", str(raised.exception))

    def test_malformed_response_shapes_preserve_usage_and_are_sanitized(self):
        for output in (None, {}, [None], [{"type": "message", "content": None}],
                       [{"type": "message", "content": [None]}]):
            payload = response_payload(output=output)
            with self.subTest(output=output), patch.object(openai_http, "build_opener", return_value=mocked_opener(payload)):
                with self.assertRaises(OpenAIError) as raised:
                    small_request()
                self.assertEqual(raised.exception.code, "invalid_shape")
                self.assertEqual(raised.exception.usage, USAGE)

    def test_duplicate_structured_keys_and_missing_usage_fail(self):
        payload = response_payload()
        payload["output"][0]["content"][0]["text"] = '{"ok":true,"ok":true}'
        with patch.object(openai_http, "build_opener", return_value=mocked_opener(payload)):
            with self.assertRaises(OpenAIError) as raised:
                small_request()
            self.assertEqual(raised.exception.code, "invalid_structured_json")
        with patch.object(openai_http, "build_opener", return_value=mocked_opener(response_payload(usage=None))):
            with self.assertRaises(OpenAIError) as raised:
                small_request()
            self.assertEqual(raised.exception.code, "missing_usage")

    def test_configuration_missing_names_include_whitespace_only_values(self):
        with patch.object(openai_http, "dotenv_values", return_value={"OPENAI_API_KEY": "  ", "OPENAI_MODEL": " "}):
            key, missing = openai_http.configuration()
        self.assertIsNone(key)
        self.assertEqual(missing, ["OPENAI_API_KEY", "OPENAI_MODEL"])


class OpenAIProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = patch("catalog_harness.openai_provider.configuration", return_value=("test-key", []))
        self.config.start()
        self.addCleanup(self.config.stop)

    def test_valid_plan_excludes_labels_context_and_enforces_request_budget(self):
        samples = copy.deepcopy(SAMPLES)
        samples[0].update(expected_status="private-status", product={"answer": "private-answer"},
                          evaluator_report="private-report", source_record_id="private-source-identity")
        metadata = copy.deepcopy(METADATA)
        metadata[0].update(expected_answers="private-metadata-answer", display_name="private-display-name")
        provider = OpenAIPlanProvider(max_requests=1)
        with patch("catalog_harness.openai_provider.request_json", return_value=(copy.deepcopy(PLAN),
                   {"model": "gpt-4.1-mini", "usage": dict(USAGE)})) as request:
            result = provider.obtain_plan(samples, metadata, HarnessPolicy())
            with self.assertRaises(OpenAIError) as raised:
                provider.obtain_plan(samples, metadata, HarnessPolicy())
        self.assertEqual(result, PLAN)
        self.assertEqual(raised.exception.code, "request_limit")
        self.assertEqual(request.call_count, 1)
        arguments = request.call_args[1]
        context = json.loads(arguments["input_text"])
        self.assertEqual(set(context), {"samples", "supplier_metadata"})
        self.assertEqual(set(context["samples"][0]), {"supplier_id", "payload"})
        self.assertEqual(set(context["supplier_metadata"][0]), {"supplier_id", "unit_documentation"})
        self.assertNotIn("private-", arguments["input_text"])
        self.assertEqual(arguments["max_output_tokens"], 2048)
        self.assertFalse(arguments["schema"]["additionalProperties"])
        self.assertEqual(provider.request_events[0]["status"], "validated")
        self.assertEqual(provider.request_events[0]["usage"], USAGE)

    def test_invalid_plan_rejection_preserves_usage(self):
        for mutation in ("unsafe_operation", "unobserved_field", "missing_supplier"):
            plan = copy.deepcopy(PLAN)
            if mutation == "unsafe_operation":
                plan["suppliers"]["supplier"]["on_ambiguity"] = "guess"
            elif mutation == "unobserved_field":
                plan["suppliers"]["supplier"]["fields"]["weight"] = "invented_weight"
            else:
                plan["suppliers"] = {"other_supplier": plan["suppliers"].pop("supplier")}
            provider = OpenAIPlanProvider()
            with self.subTest(mutation=mutation), patch("catalog_harness.openai_provider.request_json", return_value=(plan,
                     {"model": "gpt-4.1-mini", "usage": dict(USAGE)})):
                with self.assertRaises(OpenAIError) as raised:
                    provider.obtain_plan(SAMPLES, METADATA, HarnessPolicy())
                self.assertEqual((raised.exception.category, raised.exception.code), ("plan", "invalid_plan"))
                self.assertEqual(raised.exception.usage, USAGE)
                self.assertEqual(provider.request_events[0]["usage"], USAGE)
                self.assertEqual(provider.request_events[0]["status"], "failed")

    def test_failed_request_is_not_retried_and_usage_is_recorded(self):
        provider = OpenAIPlanProvider(max_requests=1)
        with patch("catalog_harness.openai_provider.request_json", side_effect=OpenAIError("response", "incomplete", USAGE)) as request:
            with self.assertRaises(OpenAIError):
                provider.obtain_plan(SAMPLES, METADATA, HarnessPolicy())
            with self.assertRaises(OpenAIError) as raised:
                provider.obtain_plan(SAMPLES, METADATA, HarnessPolicy())
        self.assertEqual(request.call_count, 1)
        self.assertEqual(raised.exception.code, "request_limit")
        self.assertEqual(provider.request_events[0]["usage"], USAGE)

    def test_holdout_and_sample_limits_prevent_model_requests(self):
        final_samples = copy.deepcopy(SAMPLES)
        final_samples[0]["dataset_id"] = "catalog-v1-final-s2-n30"
        for samples, expected in ((final_samples, "holdout_forbidden"),
                                  (SAMPLES * 9, "sample_budget")):
            provider = OpenAIPlanProvider()
            with self.subTest(expected=expected), patch("catalog_harness.openai_provider.request_json") as request:
                with self.assertRaises(OpenAIError) as raised:
                    provider.obtain_plan(samples, METADATA, HarnessPolicy())
                self.assertEqual(raised.exception.code, expected)
                request.assert_not_called()
                self.assertEqual(provider.requests_made, 0)

    def test_pipeline_ai_baseline_is_development_only(self):
        store, events = MagicMock(), MagicMock()
        store.dataset.return_value = {"split": "final"}
        with self.assertRaises(ValueError):
            run_plan(store, "final", PLAN, events, kind="ai_development_baseline",
                     policy=HarnessPolicy(), final_report=True)
        store.save_policy.assert_not_called()
        store.runs.insert_one.assert_not_called()
        store.save_results.assert_not_called()

    def test_cli_rejects_final_before_sampling_or_model_request(self):
        client, store = MagicMock(), MagicMock()
        store.dataset.return_value = {"split": "final"}
        with patch("catalog_harness.__main__.connect", return_value=(client, store)), \
                patch("catalog_harness.openai_provider.OpenAIPlanProvider") as provider, redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(["run-ai-baseline", "--dataset", "catalog-v1-final-s2-n30"]), 1)
        store.sample.assert_not_called()
        provider.assert_not_called()
        client.close.assert_called_once()


class OpenAICheckTests(unittest.TestCase):
    def test_missing_configuration_prints_only_missing_names(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(check_openai, "ROOT", Path(directory)), \
                patch.object(check_openai, "configuration", return_value=(None, ["OPENAI_API_KEY"])), \
                patch.object(check_openai, "request_json") as request, redirect_stdout(output):
            self.assertEqual(check_openai.main(), 1)
        self.assertEqual(output.getvalue().strip(), "FAIL: missing OPENAI_API_KEY")
        request.assert_not_called()

    def test_check_validates_true_and_reports_model_usage(self):
        for value, expected in (({"ok": True}, 0), ({"ok": False}, 1), ({"ok": True, "extra": 1}, 1)):
            output = io.StringIO()
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory, \
                    patch.object(check_openai, "ROOT", Path(directory)), \
                    patch.object(check_openai, "configuration", return_value=("test-secret-key", [])), \
                    patch.object(check_openai, "request_json", return_value=(value,
                        {"model": "gpt-4.1-mini", "usage": dict(USAGE)})) as request, redirect_stdout(output):
                self.assertEqual(check_openai.main(), expected)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args[1]["max_output_tokens"], 128)
                artifact = json.loads((Path(directory) / "artifacts/openai-check.json").read_text())
                self.assertEqual(artifact["passed"], expected == 0)
                self.assertEqual(artifact["usage"], USAGE)
            self.assertNotIn("test-secret-key", output.getvalue())
            self.assertIn("model=gpt-4.1-mini", output.getvalue())
