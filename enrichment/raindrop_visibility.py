"""Fresh, read-only visibility of existing historical Raindrop telemetry.

Contracts: https://www.raindrop.ai/docs/query-api/sdk/ and official
raindrop-query 0.1.14 signals_listop/events_countop/events_getop models.
Only GET /v1/signals?limit=50, GET /v1/events/count with exact user/event
filters, and the six existing aggregate-anchor event GETs are performed.
Names/links are observable; aggregate signal properties and historical usage
are not established by event-detail signal names. No telemetry is sent.
"""

import argparse
import json
import re
import socket
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from enrichment.budget import write_json
from enrichment.raindrop import MAX_BYTES, OUTBOX, ROOT, NoRedirect, RaindropError, _digest, _phase, _read
from enrichment.raindrop_monitoring import DIRECTORY, _expected, _inputs, _prepared
from enrichment.raindrop_query import _configuration_values, _coverage, _now, _precheck, configuration, get_event


QUERY_BASE = "https://query.raindrop.ai/v1/"
QUERY_PATHS = {
    "signals": "signals?limit=50",
    "count": "events/count?" + urlencode({"user_id": "catalog-harness-demo",
                                           "event_name": "semantic_enrichment_import"}),
}
SIGNAL_TYPES = {"topic", "regex", "instrumented", "metric", "code"}
RECEIPT = Path("artifacts/raindrop-visibility/latest.json")


def normalize_name(name):
    """Raindrop presents underscore instrumented names as human-readable titles."""
    if not isinstance(name, str) or not 1 <= len(name) <= 256:
        raise RaindropError("response", "signal_name_invalid")
    return re.sub(r"[\s_-]+", " ", name.strip()).lower()


def get_json(kind, config):
    """One allowlisted GET; TLS defaults, no redirects/retries, 30s/1MiB bounds."""
    config, missing = _configuration_values(config)
    if missing:
        raise RaindropError("configuration", "missing_variables")
    if kind not in QUERY_PATHS:
        raise RaindropError("local", "query_not_allowlisted")
    request = Request(QUERY_BASE + QUERY_PATHS[kind], method="GET", headers={
        "Authorization": "Bearer " + config["RAINDROP_QUERY_API_KEY"],
        "X-Raindrop-Project-Id": config["RAINDROP_PROJECT_ID"], "Accept": "application/json"})
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
            if not isinstance(result, dict):
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


def _original(root, outbox, config):
    """Validate original proofs and reconstruct immutable saved-call provenance."""
    manifest, unused, events = _precheck(outbox, config)
    project, signals, hashes = _inputs(outbox)
    final_events, final_signals = _phase(root, True)
    old_events, old_signals = _phase(root, False)
    if (project != config["RAINDROP_PROJECT_ID"] or len(events) != 31 or len(signals) != 6 or
            _digest(events) != _digest(final_events + old_events) or
            _digest(signals) != _digest(final_signals + old_signals)):
        raise RaindropError("local", "source_provenance_mismatch")
    return manifest, events, signals, hashes


def _monitoring(directory, project, signals, hashes):
    if not directory.exists():
        return [], "not_prepared", None
    manifest, payload = _expected(project, signals, hashes)
    state = _prepared(directory, manifest, payload)
    if state.get("status") not in {"pending", "reserved", "sent", "unknown", "failed"}:
        raise RaindropError("local", "monitoring_delivery_changed")
    return payload, state["status"], _digest(manifest)


def _definitions(response):
    data, meta = response.get("data"), response.get("meta")
    if (not isinstance(data, list) or len(data) > 50 or not isinstance(meta, dict) or
            type(meta.get("has_more")) is not bool):
        raise RaindropError("response", "signals_shape_invalid")
    result = {}
    for row in data:
        if (not isinstance(row, dict) or row.get("type") not in SIGNAL_TYPES or
                not isinstance(row.get("id"), str) or not 1 <= len(row["id"]) <= 256):
            raise RaindropError("response", "signals_shape_invalid")
        name = normalize_name(row.get("name"))
        if name in result:
            raise RaindropError("response", "signal_name_ambiguous")
        result[name] = {"name": row["name"], "type": row["type"]}
    return result, meta["has_more"]


def _linked_names(response):
    rows = response.get("data", {}).get("signals", [])
    if not isinstance(rows, list) or len(rows) > 100:
        raise RaindropError("response", "event_signals_shape_invalid")
    result = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or
                not 1 <= len(row["id"]) <= 256):
            raise RaindropError("response", "event_signals_shape_invalid")
        result.add(normalize_name(row.get("name")))
    return result


def check_visibility(root=ROOT, outbox=None, monitoring=None, config=None,
                     request=None, event_transport=None, save_receipt=False):
    """Read eight fresh responses; never reuse cached live data or update ledgers.

    Optional receipts contain aggregate proof coverage only and are written to
    the fixed ignored artifacts path. Transports are injectable for offline tests.
    Fresh core-content claims concern six anchors; the other 25 retain their
    separately validated original readback proofs, not a new verification claim.
    """
    root = Path(root)
    outbox = Path(outbox) if outbox is not None else root / "artifacts/raindrop"
    monitoring = Path(monitoring) if monitoring is not None else root / "artifacts/raindrop-monitoring-v1"
    result = {"kind": "raindrop_visibility", "status": "unverified", "checked_at": _now(),
              "expected_events": 31, "expected_anchors": 6, "fresh_core_events_verified": 0,
              "network_gets_added": 0, "network_posts_added": 0, "model_calls_added": 0,
              "events_retransmitted": 0, "signal_properties_verified": False,
              "historical_costs_backfilled": False, "aggregate_links": []}
    try:
        config, missing = configuration(root) if config is None else _configuration_values(config)
        if missing:
            result.update(status="missing_configuration", missing=missing)
            return result
        manifest, events, signals, hashes = _original(root, outbox, config)
        monitor_rows, monitor_status, monitor_hash = _monitoring(
            monitoring, config["RAINDROP_PROJECT_ID"], signals, hashes)
        result.update(original_provenance_verified=True, prior_core_events_verified=31,
                      monitoring_status=monitor_status, monitoring_signals_expected=len(monitor_rows),
                      events_sha256=manifest["events_sha256"], monitoring_manifest_sha256=monitor_hash)
        expected = {row["signal_name"] for row in signals + monitor_rows}
        if len({normalize_name(name) for name in expected}) != len(expected):
            raise RaindropError("local", "expected_signal_name_ambiguous")
        request, event_transport = request or get_json, event_transport or get_event
        result["network_gets_added"] += 1
        definitions, has_more = _definitions(request("signals", config))
        result["signal_list_has_more"] = has_more
        result["custom_signals"] = [{"expected_name": name, "discovered": normalize_name(name) in definitions,
            **definitions.get(normalize_name(name), {})} for name in sorted(expected)]
        result["network_gets_added"] += 1
        count = request("count", config)
        total = count.get("data", {}).get("total") if isinstance(count.get("data"), dict) else None
        if type(total) is not int or total < 0:
            raise RaindropError("response", "count_shape_invalid")
        result.update(total=total, count_matches=total == 31)
        by_id = {event["event_id"]: event for event in events}
        missing_metadata = {}
        for signal in signals:
            identity, properties = signal["event_id"], signal["properties"]
            names = {signal["signal_name"]} | {row["signal_name"] for row in monitor_rows
                                               if row["event_id"] == identity}
            result["network_gets_added"] += 1
            response = event_transport(identity, config)
            coverage = _coverage(by_id[identity], response)
            if coverage is None:
                raise RaindropError("verification", "event_content_mismatch")
            result["fresh_core_events_verified"] += 1
            for name in coverage["missing_nested_metadata"]:
                missing_metadata[name] = missing_metadata.get(name, 0) + 1
            linked = _linked_names(response)
            matched = sorted(name for name in names if normalize_name(name) in linked)
            result["aggregate_links"].append({"phase": properties["phase"], "variant": properties["variant"],
                "split": properties["split"], "scope": "variant_aggregate",
                "event_role": "run_anchor_not_individual_call_attribution", "core_content_match": True,
                "expected_signal_names": sorted(names), "matched_signal_names": matched,
                "missing_signal_names": sorted(names - set(matched))})
        # Recheck immutable input bindings after network I/O; never alter a ledger.
        if _original(root, outbox, config)[3] != hashes:
            raise RaindropError("local", "source_changed_during_verification")
        if _monitoring(monitoring, config["RAINDROP_PROJECT_ID"], signals, hashes)[2] != monitor_hash:
            raise RaindropError("local", "monitoring_changed_during_verification")
        result.update(core_content_matches=True, missing_nested_metadata=missing_metadata,
                      nested_metadata_verified=not missing_metadata,
                      local_usage_only="model_usage" in missing_metadata)
        definitions_match = all(row["discovered"] and row.get("type") == "instrumented"
                                for row in result["custom_signals"])
        links_match = all(not row["missing_signal_names"] for row in result["aggregate_links"])
        result["status"] = "verified" if total == 31 and definitions_match and links_match else "unverified"
        if result["status"] != "verified":
            result["error"] = {"category": "verification", "code": "visibility_incomplete"}
    except RaindropError as error:
        result["error"] = {"category": error.category, "code": error.code}
    except Exception:
        result["error"] = {"category": "local", "code": "visibility_failed"}
    result["checked_at"] = _now()
    if save_receipt:
        path = root / RECEIPT
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json(path, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--receipt", action="store_true", help="Save aggregate proof in ignored artifacts")
    args = parser.parse_args(argv)
    try:
        result = check_visibility(save_receipt=args.receipt)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "verified" else 1
    except Exception:
        print("FAIL local/visibility_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
