"""Deterministic aggregate flags for already verified historical Raindrop runs.

Contract: https://www.raindrop.ai/docs/sdk/http-api/ — POST /signals/track,
signal_type=default, optional sentiment and scalar properties. An anchor event
identifies a whole evaluation run; these flags do not identify failing calls.
No model inference, event retransmission, evaluator keys or raw I/O is added.
"""

import argparse
import fcntl
import json
import os
import re
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from enrichment.budget import write_json
from enrichment.raindrop import (COUNT_KEYS, OUTBOX, ROOT, RaindropError, _configuration_values,
                                _digest, _flat_signals, _read, configuration, post_array)
from enrichment.raindrop_query import HASH, _precheck, _receipt


DIRECTORY = ROOT / "artifacts/raindrop-monitoring-v1"
VERSION = 1
FLAGS = (
    ("unsupported_assertions", "aggregate_unsupported_assertions_present"),
    ("unnecessary_abstentions", "aggregate_unnecessary_abstentions_present"),
    ("evidence_issue_count", "aggregate_evidence_issues_present"),
    ("incorrect_field_decisions", "aggregate_incorrect_fields_present"),
)
EVIDENCE_CLEAN = "aggregate_evidence_checks_clean"
EXPECTED_RUNS = {("final_holdout", variant, "final") for variant in ("baseline", "candidate")} | {
    ("development_validation", variant, split)
    for variant in ("baseline", "candidate") for split in ("development", "validation")}


def _fail(code):
    raise RaindropError("local", code)


@contextmanager
def _locked(directory):
    try:
        stream = (Path(directory) / "delivery.lock").open("a")
    except OSError:
        _fail("monitoring_unavailable")
    with stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _fail("delivery_locked")
        yield


def _write(path, value):
    """Fsync the file and rename's directory before a reserved POST can start."""
    write_json(path, value)
    os.chmod(path, 0o600)
    descriptor = os.open(str(Path(path).parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _inputs(outbox):
    """Reuse the existing exact event/receipt validation; never modify its files."""
    state = _read(outbox / "delivery.json")
    project = state.get("project_id") if isinstance(state, dict) else None
    if (not isinstance(project, str) or project == "default" or not
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", project)):
        _fail("explicit_source_project_required")
    manifest, state, events = _precheck(outbox, {"RAINDROP_PROJECT_ID": project})
    receipt = _receipt(outbox, manifest, events, project)
    identities = {event["event_id"] for event in events}
    proof = state["events"].get("query_verification", {})
    if (len(events) != 31 or manifest.get("signals") != 6 or
            state["events"].get("status") != "sent" or proof.get("method") != "GET" or
            proof.get("events") != 31 or proof.get("core_events_verified") is not True or
            proof.get("events_sha256") != manifest["events_sha256"] or
            proof.get("signals_sha256") != manifest["signals_sha256"] or
            proof.get("receipt_sha256") != _digest(receipt) or
            set(receipt["verified"]) != identities):
        _fail("complete_31_event_proof_required")
    signals = _read(outbox / "signals.json")
    flat = _flat_signals(signals)
    wire = state.get("signals_wire", {})
    if (state["signals"].get("status") != "superseded" or
            state["signals"].get("superseded_by") != "signals_wire" or
            wire.get("status") != "sent" or wire.get("format") != "flat-metrics-v1" or
            type(wire.get("http_status")) is not int or not 200 <= wire["http_status"] < 300 or
            wire.get("sha256") != _digest(flat) or
            wire.get("source_sha256") != manifest["signals_sha256"] or
            wire.get("events_sha256") != manifest["events_sha256"] or
            _digest(_read(outbox / "signals-flat-v1.json")) != _digest(flat)):
        _fail("six_sent_aggregate_signals_required")
    links = _read(outbox / "signal-link-receipt.json")
    anchors = {signal["event_id"] for signal in signals}
    if (len(anchors) != 6 or links.get("status") != "verified" or
            links.get("expected_links") != 6 or links.get("verified_links") != 6 or
            links.get("events_sha256") != manifest["events_sha256"] or
            links.get("signals_sha256") != _digest(flat) or
            not isinstance(links.get("verified"), dict) or set(links["verified"]) != anchors):
        _fail("six_signal_links_required")
    for entry in links["verified"].values():
        if (not isinstance(entry, dict) or entry.get("verified") is not True or
                type(entry.get("matching_signal_count")) is not int or entry["matching_signal_count"] < 1 or
                not isinstance(entry.get("response_sha256"), str) or
                not HASH.fullmatch(entry["response_sha256"])):
            _fail("six_signal_links_required")
    if {(s["properties"].get("phase"), s["properties"].get("variant"),
         s["properties"].get("split")) for s in signals} != EXPECTED_RUNS:
        _fail("six_distinct_runs_required")
    # Cached read-only verification refreshes this time without new GETs/proofs.
    # Exclude only that refresh field; retain receipts, statuses and every ack.
    delivery_binding = {**state, "events": {**state["events"], "query_verification": {
        key: value for key, value in proof.items() if key != "verified_at"}}}
    names = ("manifest.json", "delivery.json", "events.json", "signals.json",
             "signals-flat-v1.json", "query-receipt.json", "signal-link-receipt.json")
    return project, signals, {name: _digest(delivery_binding if name == "delivery.json"
                                          else _read(outbox / name)) for name in names}


def _derive(signals):
    payload = []
    for signal in signals:
        source = signal["properties"]
        counts = source["counts"]
        properties = {key: source[key] for key in
                      ("scope", "phase", "phase_id", "variant", "split", "run_id", "historical", "synthetic_fixture")}
        properties.update({"monitoring_version": "aggregate-flags-v1",
                           "source": "verified_saved_evaluation_aggregate",
                           "event_role": "run_anchor_not_individual_call_attribution",
                           "model_calls_added": 0})
        properties.update({"counts_" + key: counts[key] for key in sorted(COUNT_KEYS)})
        selected = [(key, name, "NEGATIVE") for key, name in FLAGS if counts[key] > 0]
        if counts["evidence_issue_count"] == 0:
            selected.append(("evidence_issue_count", EVIDENCE_CLEAN, "POSITIVE"))
        for key, name, sentiment in selected:
            detail = {**properties, "metric": key, "metric_value": counts[key],
                      "rule": "equals_zero" if name == EVIDENCE_CLEAN else "greater_than_zero"}
            if name == EVIDENCE_CLEAN:
                detail["meaning"] = "No mechanical evidence issues; does not imply semantic correctness"
            payload.append({"event_id": signal["event_id"], "signal_name": name,
                            "signal_type": "default", "sentiment": sentiment, "properties": detail})
    return payload


def _expected(project, signals, hashes):
    payload = _derive(signals)
    manifest = {"version": VERSION, "kind": "historical_aggregate_monitoring_flags",
                "project_id": project, "source_hashes": hashes, "source_events": 31,
                "delivery_binding_ignores": "events.query_verification.verified_at",
                "source_aggregate_signals": 6, "signals": len(payload),
                "signals_sha256": _digest(payload), "model_calls_added": 0,
                "counts_by_signal": dict(Counter(row["signal_name"] for row in payload))}
    return manifest, payload


def _prepared(directory, manifest, payload):
    if (_digest(_read(directory / "manifest.json")) != _digest(manifest) or
            _digest(_read(directory / "signals.json")) != manifest["signals_sha256"]):
        _fail("immutable_monitoring_changed")
    state = _read(directory / "delivery.json")
    if (not isinstance(state, dict) or state.get("version") != VERSION or
            state.get("manifest_sha256") != _digest(manifest) or
            state.get("signals_sha256") != manifest["signals_sha256"] or
            state.get("project_id") != manifest["project_id"]):
        _fail("monitoring_delivery_changed")
    return state


def _summary(manifest, state, posts=0):
    return {"status": state["status"], "signals": manifest["signals"],
            "counts_by_signal": manifest["counts_by_signal"], "source_events_verified": 31,
            "source_aggregate_links_verified": 6, "aggregate_properties_readback_verified": False,
            "project_bound": True,
            "network_posts_added": posts, "events_retransmitted": 0, "model_calls_added": 0,
            "signal_links_verified": False}


def prepare(outbox=OUTBOX, directory=DIRECTORY):
    """Prepare offline; existing content must match exactly, including receipts."""
    outbox, directory = Path(outbox), Path(directory)
    if outbox.resolve() == directory.resolve():
        _fail("separate_monitoring_directory_required")
    with _locked(outbox):
        manifest, payload = _expected(*_inputs(outbox))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _locked(directory):
            if (directory / "manifest.json").exists():
                state = _prepared(directory, manifest, payload)
            else:
                if any(path.name != "delivery.lock" for path in directory.iterdir()):
                    _fail("partial_monitoring_requires_review")
                state = {"version": VERSION, "project_id": manifest["project_id"], "status": "pending",
                         "manifest_sha256": _digest(manifest), "signals_sha256": manifest["signals_sha256"]}
                _write(directory / "signals.json", payload)
                _write(directory / "delivery.json", state)
                _write(directory / "manifest.json", manifest)
            return _summary(manifest, state)


def status(outbox=OUTBOX, directory=DIRECTORY):
    with _locked(Path(outbox)), _locked(Path(directory)):
        manifest, payload = _expected(*_inputs(Path(outbox)))
        return _summary(manifest, _prepared(Path(directory), manifest, payload))


def send(outbox=OUTBOX, directory=DIRECTORY, config=None, transport=None):
    """One explicit POST. Sent skips; reserved, unknown and failed fence resends."""
    config, missing = configuration() if config is None else _configuration_values(config)
    if missing:
        return {"status": "missing_configuration", "missing": missing}
    outbox, directory = Path(outbox), Path(directory)
    with _locked(outbox), _locked(directory):
        manifest, payload = _expected(*_inputs(outbox))
        state = _prepared(directory, manifest, payload)
        if config["RAINDROP_PROJECT_ID"] != manifest["project_id"]:
            _fail("delivery_project_changed")
        if state["status"] == "sent":
            return _summary(manifest, state)
        if state["status"] != "pending" or any(key in state for key in ("error", "http_status", "acknowledged_at")):
            _fail("delivery_uncertain_or_failed")
        state["status"] = "reserved"
        _write(directory / "delivery.json", state)
        try:
            acknowledgement = (transport or post_array)("signals", payload, config)
            if (not isinstance(acknowledgement, dict) or type(acknowledgement.get("http_status")) is not int or
                    not 200 <= acknowledgement["http_status"] < 300):
                raise RaindropError("response", "invalid_acknowledgement")
        except Exception as error:
            safe = error if isinstance(error, RaindropError) else RaindropError("local", "delivery_failed")
            state.update(status="failed" if safe.category in ("authentication", "permission", "configuration") else "unknown",
                         error={"category": safe.category, "code": safe.code})
            _write(directory / "delivery.json", state)
            raise safe from None
        state.update(status="sent", http_status=acknowledgement["http_status"],
                     acknowledged_at=datetime.now(timezone.utc).isoformat())
        _write(directory / "delivery.json", state)
        return _summary(manifest, state, posts=1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "send", "status"))
    parser.add_argument("--outbox", type=Path, default=OUTBOX)
    parser.add_argument("--directory", type=Path, default=DIRECTORY)
    args = parser.parse_args(argv)
    try:
        result = {"prepare": prepare, "send": send, "status": status}[args.command](args.outbox, args.directory)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if result.get("missing") else 0
    except RaindropError as error:
        print("FAIL {}/{}".format(error.category, error.code))
    except Exception:
        print("FAIL local/operation_failed")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
