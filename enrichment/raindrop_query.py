"""Read-only, resumable verification of an already attempted Raindrop import.

Official contract: https://www.raindrop.ai/docs/query-api/sdk/ and the official
raindrop-query 0.1.14 source distribution (events.py, models/events_getop.py).
GET https://query.raindrop.ai/v1/events/{id} returns HTTP 200 and {"data": ...}.
Its data fields include id, event_name, user_id, convo_id, user_input,
assistant_output, model and properties. The query key is separate from the
write key. Project visibility: https://www.raindrop.ai/docs/platform/projects/.

This module cannot send events/signals, run models, or change Atlas. A missing
event does not prove ingestion failed. Only exact positive readback of every
outbox event can reconcile an unknown/reserved delivery. Event detail signals
expose names/IDs, not the aggregate properties, so signal delivery is unchanged.
"""

import argparse
import fcntl
import json
import logging
import os
import re
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from dotenv import dotenv_values

from enrichment.budget import write_json
from enrichment.raindrop import (MAX_BYTES, OUTBOX, ROOT, NoRedirect, RaindropError,
                                _digest, _read)


QUERY_URL = "https://query.raindrop.ai/v1/events/"
CONFIG_NAMES = ("RAINDROP_QUERY_API_KEY", "RAINDROP_PROJECT_ID")
RECEIPT_NAME = "query-receipt.json"
MAX_EVENTS = 31
HASH = re.compile(r"[0-9a-f]{64}")
EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _configuration_values(values):
    result = {name: (values.get(name) or "").strip() for name in CONFIG_NAMES}
    missing = [name for name in CONFIG_NAMES
               if not result[name] or result[name].startswith("YOUR_") or result[name] == name]
    if missing:
        return None, missing
    if (result["RAINDROP_PROJECT_ID"] == "default" or not
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", result["RAINDROP_PROJECT_ID"])):
        raise RaindropError("configuration", "explicit_project_required")
    if any(char in result["RAINDROP_QUERY_API_KEY"] for char in "\r\n"):
        raise RaindropError("configuration", "invalid_query_key")
    return result, []


def configuration(root=ROOT):
    """Read only the separate query key/project; process environment takes priority."""
    logging.getLogger("dotenv.main").disabled = True
    values = dotenv_values(Path(root) / ".env")
    return _configuration_values({name: os.environ.get(name, values.get(name))
                                  for name in CONFIG_NAMES})


def get_event(event_id, config):
    """One GET, 30-second timeout, bounded body, verified TLS, no redirects/retries."""
    config, missing = _configuration_values(config)
    if missing:
        raise RaindropError("configuration", "missing_variables")
    if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
        raise RaindropError("local", "event_identity_invalid")
    request = Request(QUERY_URL + quote(event_id, safe=""), method="GET", headers={
        "Authorization": "Bearer " + config["RAINDROP_QUERY_API_KEY"],
        "X-Raindrop-Project-Id": config["RAINDROP_PROJECT_ID"],
        "Accept": "application/json"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            if response.status != 200:
                raise RaindropError("response", "unexpected_status")
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise RaindropError("response", "body_limit")
            try:
                result = json.loads(body)
            except (ValueError, UnicodeError):
                raise RaindropError("response", "invalid_json") from None
            if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
                raise RaindropError("response", "invalid_shape")
            return result
    except HTTPError as error:
        code = error.code
        error.close()
        category = {401: "authentication", 403: "permission", 404: "not_found",
                    429: "rate_limit"}.get(code, "http_error")
        raise RaindropError(category, "http_{}".format(code)) from None
    except (socket.timeout, TimeoutError):
        raise RaindropError("network", "timeout") from None
    except ssl.SSLError:
        raise RaindropError("network", "tls") from None
    except URLError as error:
        code = ("tls" if isinstance(error.reason, ssl.SSLError) else "timeout"
                if isinstance(error.reason, (socket.timeout, TimeoutError)) else "connection")
        raise RaindropError("network", code) from None
    except OSError:
        raise RaindropError("network", "connection") from None


def _precheck(outbox, config):
    """Validate both immutable payloads and delivery identity before any GET."""
    manifest, state = _read(outbox / "manifest.json"), _read(outbox / "delivery.json")
    if (not isinstance(manifest, dict) or not isinstance(state, dict) or
            manifest.get("version") != 1 or state.get("version") != 1 or
            manifest.get("kind") != "saved_synthetic_raindrop_import"):
        raise RaindropError("local", "outbox_invalid")
    if state.get("project_id") != config["RAINDROP_PROJECT_ID"]:
        raise RaindropError("local", "delivery_project_changed")
    payloads = {}
    for name in ("events", "signals"):
        payload, group = _read(outbox / (name + ".json")), state.get(name)
        if (not isinstance(payload, list) or not isinstance(group, dict) or
                type(manifest.get(name)) is not int or manifest[name] != len(payload) or
                _digest(payload) != manifest.get(name + "_sha256") or
                _digest(payload) != group.get("sha256")):
            raise RaindropError("local", "outbox_changed")
        payloads[name] = payload
    if state["events"].get("status") not in ("unknown", "reserved", "sent"):
        raise RaindropError("local", "events_delivery_not_attempted")
    events, signals = payloads["events"], payloads["signals"]
    if not 1 <= len(events) <= MAX_EVENTS or len(signals) > 6:
        raise RaindropError("local", "event_limit")
    seen = set()
    for event in events:
        if not isinstance(event, dict):
            raise RaindropError("local", "event_identity_invalid")
        identity = event.get("event_id")
        if (not isinstance(identity, str) or not EVENT_ID.fullmatch(identity) or identity in seen or
                not isinstance(event.get("properties"), dict) or
                not isinstance(event.get("ai_data"), dict) or
                any(not isinstance(event.get(field), str) for field in ("event", "user_id")) or
                any(not isinstance(event["ai_data"].get(field), str)
                    for field in ("model", "input", "output", "convo_id"))):
            raise RaindropError("local", "event_identity_invalid")
        seen.add(identity)
    if any(not isinstance(signal, dict) or signal.get("event_id") not in seen for signal in signals):
        raise RaindropError("local", "signal_identity_invalid")
    return manifest, state, events


def _matches(event, response):
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return False
    expected = {"id": event["event_id"], "event_name": event["event"], "user_id": event["user_id"],
                "convo_id": event["ai_data"]["convo_id"], "model": event["ai_data"]["model"],
                "user_input": event["ai_data"]["input"], "assistant_output": event["ai_data"]["output"]}
    if any(data.get(name) != value for name, value in expected.items()):
        return False
    actual_properties = data.get("properties")
    if not isinstance(actual_properties, dict) or not set(event["properties"]) <= set(actual_properties):
        return False
    # Canonical JSON distinguishes booleans from numbers and covers nested schema,
    # exact usage, run links and aggregate metadata without logging their values.
    return _digest(event["properties"]) == _digest({name: actual_properties[name]
                                                   for name in event["properties"]})


def _receipt(outbox, manifest, events, project):
    binding = {"version": 1, "project_id": project, "manifest_sha256": _digest(manifest),
               "events_sha256": manifest["events_sha256"], "signals_sha256": manifest["signals_sha256"]}
    path = outbox / RECEIPT_NAME
    if not path.exists():
        return {**binding, "verified": {}}
    receipt = _read(path)
    if (not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in binding.items())
            or not isinstance(receipt.get("verified"), dict)):
        raise RaindropError("local", "query_receipt_changed")
    expected = {event["event_id"]: _digest(event) for event in events}
    for identity, proof in receipt["verified"].items():
        if (identity not in expected or not isinstance(proof, dict) or
                proof.get("expected_sha256") != expected[identity] or
                not isinstance(proof.get("response_sha256"), str) or
                not HASH.fullmatch(proof["response_sha256"]) or
                not isinstance(proof.get("verified_at"), str) or not proof["verified_at"]):
            raise RaindropError("local", "query_receipt_changed")
    return receipt


def verify_outbox(outbox=OUTBOX, config=None, transport=None):
    """Resume exact positive GET proofs, then reconcile delivery under its lock.

    ``transport(event_id, config)`` returns the complete Query API response. It is
    injected only for offline tests. CLI summaries never contain fetched content,
    event IDs, project values or credentials. Receipts live in ignored artifacts.
    """
    config, missing = configuration() if config is None else _configuration_values(config)
    if missing:
        return {"status": "missing_configuration", "missing": missing}
    outbox, transport = Path(outbox), transport or get_event
    with (outbox / "delivery.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RaindropError("local", "delivery_locked") from None
        manifest, state, events = _precheck(outbox, config)
        original_state_sha256 = _digest(state)
        receipt = _receipt(outbox, manifest, events, config["RAINDROP_PROJECT_ID"])
        calls = 0

        def summary(status, error=None, reconciled=False):
            result = {"status": status, "expected_events": len(events),
                      "verified_events": len(receipt["verified"]), "network_gets_added": calls,
                      "model_calls_added": 0, "events_reconciled": reconciled,
                      "signals_status": state["signals"].get("status"),
                      "signal_properties_verified": False}
            if error is not None:
                result["error"] = {"category": error.category, "code": error.code}
            return result

        for event in events:
            identity = event["event_id"]
            if identity in receipt["verified"]:
                continue
            calls += 1
            try:
                response = transport(identity, config)
                if not _matches(event, response):
                    raise RaindropError("verification", "event_content_mismatch")
                receipt["verified"][identity] = {"expected_sha256": _digest(event),
                    "response_sha256": _digest(response), "verified_at": _now()}
                receipt.pop("last_error", None)
                write_json(outbox / RECEIPT_NAME, receipt)
            except RaindropError as error:
                receipt["last_error"] = {"category": error.category, "code": error.code}
                write_json(outbox / RECEIPT_NAME, receipt)
                return summary("unverified", error)
            except Exception:
                error = RaindropError("local", "query_failed")
                receipt["last_error"] = {"category": error.category, "code": error.code}
                write_json(outbox / RECEIPT_NAME, receipt)
                return summary("unverified", error)

        # Recheck both payloads and state: manual edits outside this advisory lock
        # must not turn a proof of one outbox into approval of a different import.
        current_manifest, current_state, current_events = _precheck(outbox, config)
        if (_digest(current_manifest) != _digest(manifest) or current_events != events or
                _digest(current_state) != original_state_sha256):
            raise RaindropError("local", "outbox_changed_during_query")
        original_status = state["events"]["status"]
        proof = {"method": "GET", "verified_at": _now(), "events": len(events),
                 "events_sha256": manifest["events_sha256"], "signals_sha256": manifest["signals_sha256"],
                 "receipt_sha256": _digest(receipt),
                 "original_status": state["events"].get("query_verification", {}).get("original_status", original_status)}
        # Keep the original error for provenance, including a lost HTTP status.
        state["events"].update(status="sent", query_verification=proof)
        write_json(outbox / "delivery.json", state)
        return summary("verified", reconciled=original_status != "sent")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify",))
    parser.add_argument("--outbox", type=Path, default=OUTBOX)
    args = parser.parse_args(argv)
    try:
        result = verify_outbox(args.outbox)
        if result.get("missing"):
            print("Missing: " + ", ".join(result["missing"]))
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "verified" else 1
    except RaindropError as error:
        print("FAIL {}/{}".format(error.category, error.code))
    except Exception:
        print("FAIL local/operation_failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
