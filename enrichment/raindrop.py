"""Export saved synthetic traces; ingest only through the explicit send command.

Official HTTP contract: https://www.raindrop.ai/docs/sdk/http-api/
An import describes already completed model calls. It never reruns inference,
changes Atlas, or sends evaluator answer keys or individual failure examples.
"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import dotenv_values

from catalog_harness.openai_http import MODEL
from catalog_harness.storage import ROOT
from enrichment.budget import write_json

BASE_URL = "https://api.raindrop.ai/v1"
MAX_BYTES = 1024 * 1024
OUTBOX = ROOT / "artifacts/raindrop"
REQUEST_KEYS = {"schema", "schema_name", "instructions", "input_text", "max_output_tokens"}
FORBIDDEN_KEYS = {"answer_keys", "expected_answers", "acceptable_categories", "acceptable_primary_uses",
                  "required_secondary_uses", "optional_secondary_uses", "evaluator_report",
                  "authorization", "api_key", "openai_api_key", "raindrop_write_key"}
COUNT_KEYS = {"total_expected", "total_field_decisions", "correct_field_decisions", "incorrect_field_decisions",
              "unsupported_assertions", "necessary_abstentions", "unnecessary_abstentions", "missing_outputs",
              "duplicate_outputs", "unexpected_outputs", "evidence_issue_count"}


class RaindropError(Exception):
    def __init__(self, category, code):
        self.category, self.code = category, code
        super().__init__(category + "/" + code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _encode(value):
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise RaindropError("local", "invalid_artifact") from None
    if len(encoded) > MAX_BYTES:
        raise RaindropError("local", "payload_limit")
    return encoded


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _read(path):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise RaindropError("local", "payload_limit")
        return json.loads(data)
    except (OSError, ValueError):
        raise RaindropError("local", "artifact_unavailable") from None


def _safe_content(value):
    if isinstance(value, dict):
        if any(isinstance(key, str) and key.lower() in FORBIDDEN_KEYS for key in value):
            raise RaindropError("local", "private_context_rejected")
        for child in value.values():
            _safe_content(child)
    elif isinstance(value, list):
        for child in value:
            _safe_content(child)


def _usage(value):
    names = ("input_tokens", "output_tokens", "total_tokens")
    if (not isinstance(value, dict) or any(type(value.get(key)) is not int or value[key] < 0 for key in names)
            or value["total_tokens"] != value["input_tokens"] + value["output_tokens"]):
        raise RaindropError("local", "usage_unverified")
    return {key: value[key] for key in names}


def _phase(root, final):
    directory = Path(root) / ("artifacts/enrichment-v1-final" if final else "artifacts/enrichment-v1")
    report, ledger = _read(directory / "report.json"), _read(directory / "calls.json")
    expected = 12 if final else 19
    if (report.get("status") != "completed" or not report.get("atlas_persistence_verified") or
            not report.get("raw_unchanged") or len(ledger.get("calls", [])) != expected or
            any(row.get("status") != "completed" for row in ledger["calls"])):
        raise RaindropError("local", "completed_phase_required")
    phase_id = report.get("phase_id" if final else "experiment_id")
    if not isinstance(phase_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", phase_id):
        raise RaindropError("local", "phase_identity_invalid")
    phase = "final_holdout" if final else "development_validation"
    events, signals, first_events = [], [], {}
    seen = set()
    for row in ledger["calls"]:
        ordinal, stage = row.get("ordinal"), row.get("stage")
        if type(ordinal) is not int or not 1 <= ordinal <= expected or ordinal in seen:
            raise RaindropError("local", "call_identity_invalid")
        seen.add(ordinal)
        if not isinstance(stage, str) or not re.fullmatch(r"(?:baseline|candidate)_final_0[1-6]" if final else
                r"(?:baseline|candidate)_(?:development|validation)|policy_proposal", stage):
            raise RaindropError("local", "call_identity_invalid")
        request = _read(directory / ("request-{:02d}.json".format(ordinal)))
        response = _read(directory / ("response-{:02d}.json".format(ordinal)))
        if set(request) - REQUEST_KEYS - {"stage", "request_sha256"} or not REQUEST_KEYS <= set(request) or request.get("stage") != stage:
            raise RaindropError("local", "request_provenance_invalid")
        kwargs = {key: request[key] for key in REQUEST_KEYS}
        request_hash = _digest(kwargs)
        if final and any(value != request_hash for value in
                         (request.get("request_sha256"), response.get("request_sha256"), row.get("request_sha256"))):
            raise RaindropError("local", "request_provenance_invalid")
        details = response.get("details", response)
        usage = _usage(details.get("usage"))
        model = details.get("model")
        if (usage != _usage(row.get("usage")) or model != row.get("model") or model != ledger.get("model") or
                not isinstance(model, str) or not re.fullmatch(r"gpt-4\.1-mini(?:-\d{4}-\d{2}-\d{2})?", model)):
            raise RaindropError("local", "response_provenance_invalid")
        if not isinstance(request["instructions"], str) or not isinstance(request["input_text"], str) or "output" not in response:
            raise RaindropError("local", "request_provenance_invalid")
        try:
            parsed_input = json.loads(request["input_text"])
        except ValueError:
            raise RaindropError("local", "request_provenance_invalid") from None
        _safe_content(parsed_input)
        _safe_content(request["schema"])
        _safe_content(response["output"])
        request_configuration = {"schema": request["schema"], "schema_name": request["schema_name"],
                                 "max_output_tokens": request["max_output_tokens"], "store": False,
                                 "requested_model": MODEL, "strict_json_schema": True}
        variant = "proposal" if stage == "policy_proposal" else stage.split("_", 1)[0]
        split = "final" if final else "development" if stage == "policy_proposal" else stage.split("_", 1)[1]
        run_key = variant if final else stage
        run = report.get("runs", {}).get(run_key, {})
        run_id = run.get("run_id")
        if variant != "proposal" and (not isinstance(run_id, str) or run.get("split") != split):
            raise RaindropError("local", "run_provenance_invalid")
        event_id = "catalog-harness-{}-{}-{}".format(phase_id, ordinal, request_hash[:16])
        properties = {"phase": phase, "phase_id": phase_id, "variant": variant, "split": split, "stage": stage,
                      "historical": True, "synthetic_fixture": True, "source": "saved_call_ledger",
                      "call_ordinal": ordinal, "request_sha256": request_hash, "model_usage": usage,
                      "request_configuration": request_configuration,
                      "run_id": run_id, "fixture_version": report.get("fixture_version", "semantic-v1"),
                      "prior_experiment_id": report.get("prior_experiment_id", report.get("experiment_id"))}
        events.append({"user_id": "catalog-harness-demo", "event": "semantic_enrichment_import",
                       "event_id": event_id, "properties": properties,
                       "ai_data": {"model": model,
                                   "input": json.dumps({**request_configuration, "instructions": request["instructions"],
                                                        "input_text": request["input_text"]}, sort_keys=True),
                                   "output": json.dumps(response["output"], sort_keys=True, allow_nan=False),
                                   "convo_id": run_id or phase_id}})
        if variant != "proposal":
            first_events.setdefault(run_key, event_id)
    for run_key, event_id in first_events.items():
        run = report["runs"][run_key]
        counts = run.get("overall", {})
        if any(type(counts.get(key)) is not int or counts[key] < 0 for key in COUNT_KEYS):
            raise RaindropError("local", "aggregate_unverified")
        variant = run_key if final else run_key.split("_", 1)[0]
        signals.append({"event_id": event_id, "signal_name": "synthetic_enrichment_evaluation",
                        "signal_type": "default", "properties": {
                            "scope": "variant_aggregate", "phase": phase, "phase_id": phase_id,
                            "variant": variant, "split": run["split"], "run_id": run["run_id"],
                            "historical": True, "synthetic_fixture": True,
                            "counts": {key: counts[key] for key in sorted(COUNT_KEYS)},
                            "usage": _usage(run["token_usage"]), "model_calls": run["model_calls"]}})
    return events, signals


def export_outbox(root=ROOT, outbox=None, include_historical=False):
    outbox = Path(outbox) if outbox is not None else Path(root) / "artifacts/raindrop"
    events, signals = _phase(root, final=True)
    if include_historical:
        old_events, old_signals = _phase(root, final=False)
        events += old_events
        signals += old_signals
    event_hash, signal_hash = _digest(events), _digest(signals)
    manifest = {"version": 1, "kind": "saved_synthetic_raindrop_import", "historical": True,
                "includes_original_experiment": bool(include_historical), "events": len(events), "signals": len(signals),
                "events_sha256": event_hash, "signals_sha256": signal_hash, "model_calls_added": 0}
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (outbox / "delivery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (outbox / "manifest.json").exists():
            if _read(outbox / "manifest.json") != manifest:
                raise RaindropError("local", "immutable_outbox_changed")
            if not (outbox / "delivery.json").exists():
                raise RaindropError("local", "delivery_state_missing")
            if (_digest(_read(outbox / "events.json")) != event_hash or
                    _digest(_read(outbox / "signals.json")) != signal_hash):
                raise RaindropError("local", "outbox_changed")
        else:
            if (outbox / "delivery.json").exists():
                raise RaindropError("local", "partial_outbox_requires_review")
            write_json(outbox / "events.json", events)
            write_json(outbox / "signals.json", signals)
            write_json(outbox / "delivery.json", {"version": 1, "project_id": None, "events": {
                "status": "pending", "sha256": event_hash}, "signals": {"status": "pending", "sha256": signal_hash}})
            write_json(outbox / "manifest.json", manifest)
    return {"outbox": str(outbox), "events": len(events), "signals": len(signals),
            "network_calls_added": 0, "model_calls_added": 0, "historical": True}


def _configuration_values(values):
    values = {name: (values.get(name) or "").strip() for name in ("RAINDROP_WRITE_KEY", "RAINDROP_PROJECT_ID")}
    missing = [name for name, value in values.items()
               if not value or value.startswith("YOUR_") or value == name]
    if missing:
        return None, missing
    project = values["RAINDROP_PROJECT_ID"]
    if project == "default" or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", project):
        raise RaindropError("configuration", "explicit_project_required")
    return values, []


def configuration(root=ROOT):
    logging.getLogger("dotenv.main").disabled = True
    file_values = dotenv_values(Path(root) / ".env")
    return _configuration_values({name: os.environ.get(name, file_values.get(name))
                                  for name in ("RAINDROP_WRITE_KEY", "RAINDROP_PROJECT_ID")})


def post_array(endpoint, payload, config):
    """One POST, 30 seconds, verified TLS, no redirects, no retries.

    The HTTP guide shows 204; the official raindrop-ai SDK accepts all 2xx
    responses with raise_for_status(). An acknowledgement is not readback.
    """
    if endpoint not in ("events", "signals") or not isinstance(payload, list):
        raise RaindropError("configuration", "invalid_payload")
    config, missing = _configuration_values(config)
    if missing:
        raise RaindropError("configuration", "missing_variables")
    request = Request(BASE_URL + "/" + endpoint + "/track", data=_encode(payload), method="POST", headers={
        "Authorization": "Bearer " + config["RAINDROP_WRITE_KEY"],
        "X-Raindrop-Project-Id": config["RAINDROP_PROJECT_ID"], "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            status = response.status
            if type(status) is not int or not 200 <= status < 300:
                code = "http_{}".format(status) if type(status) is int else "invalid_status"
                raise RaindropError("response", code)
            return {"http_status": status}
    except HTTPError as error:
        code = error.code
        error.close()
        category = {401: "authentication", 403: "permission", 429: "rate_limit"}.get(code, "http_error")
        raise RaindropError(category, "http_{}".format(code)) from None
    except (socket.timeout, TimeoutError):
        raise RaindropError("network", "timeout") from None
    except ssl.SSLError:
        raise RaindropError("network", "tls") from None
    except URLError as error:
        code = "tls" if isinstance(error.reason, ssl.SSLError) else "timeout" if isinstance(error.reason, (socket.timeout, TimeoutError)) else "connection"
        raise RaindropError("network", code) from None
    except OSError:
        raise RaindropError("network", "connection") from None


def send_outbox(outbox=OUTBOX, config=None, transport=None):
    if config is None:
        config, missing = configuration()
        if missing:
            return {"status": "missing_configuration", "missing": missing}
    else:
        config, missing = _configuration_values(config)
        if missing:
            return {"status": "missing_configuration", "missing": missing}
    transport = transport or post_array
    outbox = Path(outbox)
    with (outbox / "delivery.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RaindropError("local", "delivery_locked") from None
        manifest, state = _read(outbox / "manifest.json"), _read(outbox / "delivery.json")
        if state.get("project_id") not in (None, config["RAINDROP_PROJECT_ID"]):
            raise RaindropError("local", "delivery_project_changed")
        if any(state[name]["status"] not in ("pending", "sent") for name in ("events", "signals")):
            raise RaindropError("local", "delivery_uncertain_or_failed")
        payloads = {}
        for endpoint in ("events", "signals"):
            payload = _read(outbox / (endpoint + ".json"))
            if not isinstance(payload, list) or _digest(payload) != manifest[endpoint + "_sha256"] or _digest(payload) != state[endpoint]["sha256"]:
                raise RaindropError("local", "outbox_changed")
            payloads[endpoint] = payload
        state["project_id"] = config["RAINDROP_PROJECT_ID"]
        for endpoint in ("events", "signals"):
            payload = payloads[endpoint]
            if state[endpoint]["status"] == "sent":
                continue
            if endpoint == "signals" and state["events"]["status"] != "sent":
                raise RaindropError("local", "events_not_sent")
            state[endpoint]["status"] = "reserved"
            write_json(outbox / "delivery.json", state)
            try:
                acknowledgement = transport(endpoint, payload, config)
            except RaindropError as error:
                state[endpoint].update(status="unknown" if error.category in ("network", "response") else "failed",
                                       error={"category": error.category, "code": error.code})
                write_json(outbox / "delivery.json", state)
                raise
            except Exception:
                state[endpoint].update(status="unknown", error={"category": "local", "code": "delivery_failed"})
                write_json(outbox / "delivery.json", state)
                raise RaindropError("local", "delivery_failed") from None
            state[endpoint]["status"] = "sent"
            state[endpoint]["acknowledged_at"] = datetime.now(timezone.utc).isoformat()
            if (isinstance(acknowledgement, dict) and type(acknowledgement.get("http_status")) is int
                    and 200 <= acknowledgement["http_status"] < 300):
                state[endpoint]["http_status"] = acknowledgement["http_status"]
            write_json(outbox / "delivery.json", state)
    return {"status": "sent", "events": manifest["events"], "signals": manifest["signals"], "model_calls_added": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("export", "send", "status"))
    parser.add_argument("--historical", action="store_true", help="Also import the completed original 19 calls")
    parser.add_argument("--outbox", type=Path, default=OUTBOX)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_outbox(outbox=args.outbox, include_historical=args.historical)
        elif args.command == "send":
            result = send_outbox(args.outbox)
            if result.get("missing"):
                print("Missing: " + ", ".join(result["missing"]))
                return 1
        else:
            state = _read(args.outbox / "delivery.json")
            result = {"version": state.get("version"), "project_configured": bool(state.get("project_id")),
                      "events": state.get("events"), "signals": state.get("signals")}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except RaindropError as error:
        print("FAIL {}/{}".format(error.category, error.code))
    except Exception:
        print("FAIL local/operation_failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
