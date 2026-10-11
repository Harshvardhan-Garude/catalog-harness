"""Durable, resumable final-holdout budget; the completed experiment is untouched.

Prices: https://developers.openai.com/api/docs/models/gpt-4.1-mini
Use uncached prices throughout. Costs are integer billionths of one USD.
"""

import fcntl
import hashlib
import json
from pathlib import Path

from catalog_harness.openai_http import MODEL, OpenAIError, request_json
from enrichment.budget import write_json

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MODEL = "gpt-4.1-mini-2025-04-14"
INPUT_RATE = 400
OUTPUT_RATE = 1600
PHASE_CAP = 500000000
TOTAL_CAP = 1000000000
MAX_CALLS = 12
UNKNOWN_RESERVATION = (65536 + 8192) * INPUT_RATE + 2048 * OUTPUT_RATE
STAGES = {"{}_final_{:02d}".format(policy, batch) for policy in ("baseline", "candidate")
          for batch in range(1, 7)}


def valid_usage(usage):
    return (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
            for k in ("input_tokens", "output_tokens", "total_tokens"))
            and usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"])


def cost(usage):
    return usage["input_tokens"] * INPUT_RATE + usage["output_tokens"] * OUTPUT_RATE


def dollars(nanodollars):
    return round(nanodollars / 1000000000, 9)


def prior_inventory(root=ROOT):
    """Canonical ledgers only: report copies do not count the same call twice."""
    root = Path(root)
    records = []
    path = root / "artifacts/enrichment-v1/calls.json"
    if not path.exists():
        raise OpenAIError("budget", "prior_ledger_missing")
    ledger = json.loads(path.read_text())
    for row in ledger["calls"]:
        records.append({"source": "semantic-v1", "ordinal": row["ordinal"],
                        "usage": row.get("usage"), "model": row.get("model")})
    path = root / "artifacts/model-requests.jsonl"
    if path.exists():
        for index, line in enumerate(path.read_text().splitlines()):
            if line.strip():
                for row in json.loads(line).get("requests", []):
                    records.append({"source": "normalization-{}".format(index),
                                    "ordinal": row.get("attempt"),
                                    "usage": row.get("usage"), "model": row.get("model")})
    path = root / "artifacts/openai-check.json"
    if path.exists():
        row = json.loads(path.read_text())
        records.append({"source": "openai-check", "ordinal": 1,
                        "usage": row.get("usage"), "model": row.get("model")})
    # Refuse a new phase when the completed experiment is not accounted for.
    if len(ledger["calls"]) != 19 or any(r.get("status") != "completed" for r in ledger["calls"]):
        raise OpenAIError("budget", "prior_experiment_incomplete")
    total = 0
    for record in records:
        if record["model"] not in (None, MODEL, EXPECTED_MODEL):
            raise OpenAIError("budget", "prior_model_unpriced")
        total += cost(record["usage"]) if valid_usage(record["usage"]) else UNKNOWN_RESERVATION
    return {"calls": len(records), "upper_bound_nanodollars": total, "records": records}


def estimate_request(kwargs):
    maximum = kwargs.get("max_output_tokens")
    if type(maximum) is not int or not 1 <= maximum <= 2048:
        raise OpenAIError("budget", "output_limit")
    # This is the complete transport JSON, including the schema and instructions.
    body = {"model": MODEL, "store": False, "max_output_tokens": maximum,
            "instructions": kwargs["instructions"], "input": kwargs["input_text"],
            "text": {"format": {"type": "json_schema", "name": kwargs["schema_name"],
                                "strict": True, "schema": kwargs["schema"]}}}
    encoded = json.dumps(body, allow_nan=False).encode("utf-8")
    if len(encoded) > 65536:
        raise OpenAIError("budget", "input_limit")
    # BPE cannot use more tokens than UTF-8 bytes. Extra framing allowance covers
    # server-side message/schema wrappers, with no assumed caching or discounts.
    tokens = len(encoded) + 8192
    return {"input_bytes": len(encoded), "input_tokens_upper_bound": tokens,
            "max_output_tokens": maximum,
            "reserved_nanodollars": tokens * INPUT_RATE + maximum * OUTPUT_RATE}


class FinalBudget:
    """Cache completed stages; never repeat uncertain or failed transmissions."""

    def __init__(self, directory, api_key, transport=None, root=ROOT):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / "spending.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise OpenAIError("budget", "phase_locked") from None
        self.path = self.directory / "calls.json"
        self.api_key = api_key
        self.transport = transport or request_json
        try:
            prior = prior_inventory(root)
            if self.path.exists():
                self.ledger = json.loads(self.path.read_text())
                if (self.ledger.get("prior") != prior or self.ledger.get("max_calls") != MAX_CALLS
                        or self.ledger.get("phase_cap_nanodollars") != PHASE_CAP
                        or self.ledger.get("cumulative_cap_nanodollars") != TOTAL_CAP
                        or self.ledger.get("model") != EXPECTED_MODEL):
                    raise OpenAIError("budget", "ledger_contract_changed")
                if len(self.ledger["calls"]) > MAX_CALLS:
                    raise OpenAIError("budget", "call_limit")
            else:
                self.ledger = {"kind": "final_holdout_budget", "max_calls": MAX_CALLS,
                               "model": EXPECTED_MODEL, "prior": prior, "calls": [],
                               "phase_cap_nanodollars": PHASE_CAP,
                               "cumulative_cap_nanodollars": TOTAL_CAP, "stopped": False,
                               "pricing": {"input_nanodollars_per_token": INPUT_RATE,
                                           "output_nanodollars_per_token": OUTPUT_RATE,
                                           "source": "https://developers.openai.com/api/docs/models/gpt-4.1-mini"}}
                write_json(self.path, self.ledger)
        except Exception:
            self.close()
            raise

    def close(self):
        if not self.lock.closed:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()

    def _encumbered(self):
        return sum(r.get("actual_nanodollars", r["estimate"]["reserved_nanodollars"])
                   for r in self.ledger["calls"])

    def request(self, stage, **kwargs):
        if stage not in STAGES:
            raise OpenAIError("budget", "invalid_stage")
        encoded = json.dumps(kwargs, sort_keys=True, allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        for record in self.ledger["calls"]:
            if record["stage"] != stage:
                continue
            if record["request_sha256"] != digest:
                raise OpenAIError("budget", "request_changed")
            response_path = self.directory / "response-{:02d}.json".format(record["ordinal"])
            if record["status"] in ("reserved", "completed") and response_path.exists():
                saved = json.loads(response_path.read_text())
                details = saved["details"]
                if (saved["request_sha256"] != digest or details["model"] != EXPECTED_MODEL
                        or not valid_usage(details.get("usage"))):
                    raise OpenAIError("budget", "cached_response_invalid")
                record.update(status="completed", usage=details["usage"], model=details["model"],
                              actual_nanodollars=cost(details["usage"]))
                write_json(self.path, self.ledger)
                return saved["output"], details
            raise OpenAIError("budget", "uncertain_or_failed_request")
        if self.ledger["stopped"] or any(r["status"] != "completed" for r in self.ledger["calls"]):
            raise OpenAIError("budget", "phase_stopped")
        if len(self.ledger["calls"]) >= MAX_CALLS:
            raise OpenAIError("budget", "call_limit")
        estimate = estimate_request(kwargs)
        projected = self._encumbered() + estimate["reserved_nanodollars"]
        if projected > PHASE_CAP:
            raise OpenAIError("budget", "phase_spend_limit")
        if projected + self.ledger["prior"]["upper_bound_nanodollars"] > TOTAL_CAP:
            raise OpenAIError("budget", "cumulative_spend_limit")
        ordinal = len(self.ledger["calls"]) + 1
        record = {"ordinal": ordinal, "stage": stage, "request_sha256": digest,
                  "status": "reserved", "usage": None, "estimate": estimate}
        self.ledger["calls"].append(record)
        write_json(self.directory / "request-{:02d}.json".format(ordinal),
                   {"stage": stage, "request_sha256": digest, **kwargs})
        write_json(self.path, self.ledger)  # durable reservation before any transmission
        try:
            output, details = self.transport(self.api_key, **kwargs)
            if not valid_usage(details.get("usage")):
                raise OpenAIError("response", "missing_usage")
            record.update(usage=details["usage"], model=details.get("model"),
                          actual_nanodollars=cost(details["usage"]))
            if details.get("model") != EXPECTED_MODEL:
                raise OpenAIError("response", "model_changed", details["usage"])
            if record["actual_nanodollars"] > estimate["reserved_nanodollars"]:
                raise OpenAIError("budget", "estimate_exceeded", details["usage"])
            # Write response before completed status, so a crash here can recover
            # the returned response without transmitting a second request.
            write_json(self.directory / "response-{:02d}.json".format(ordinal),
                       {"request_sha256": digest, "output": output, "details": details})
            record["status"] = "completed"
            return output, details
        except OpenAIError as error:
            record.update(status="failed", category=error.category, code=error.code)
            if valid_usage(error.usage):
                record.update(usage=error.usage, actual_nanodollars=cost(error.usage))
            self.ledger["stopped"] = True
            raise
        except Exception:
            record.update(status="failed", category="local", code="request_failed")
            self.ledger["stopped"] = True
            raise OpenAIError("local", "request_failed") from None
        finally:
            write_json(self.path, self.ledger)

    def summary(self):
        totals = dict(input_tokens=0, output_tokens=0, total_tokens=0)
        groups = {}
        unknown = 0
        actual = 0
        for row in self.ledger["calls"]:
            group = groups.setdefault(row["stage"], {"calls": 0, **dict.fromkeys(totals, 0)})
            group["calls"] += 1
            if valid_usage(row.get("usage")):
                actual += cost(row["usage"])
                for key in totals:
                    totals[key] += row["usage"][key]
                    group[key] += row["usage"][key]
            else:
                unknown += 1
        encumbered = self._encumbered()
        prior = self.ledger["prior"]["upper_bound_nanodollars"]
        return {"calls": len(self.ledger["calls"]), "max_calls": MAX_CALLS,
                "model": EXPECTED_MODEL, "tokens": totals, "by_stage": groups,
                "unknown_usage_calls": unknown, "stopped": self.ledger["stopped"],
                "phase_actual_usd": dollars(actual), "phase_reserved_usd": dollars(encumbered),
                "prior_recorded_upper_bound_usd": dollars(prior),
                "prior_recorded_calls": self.ledger["prior"]["calls"],
                "cumulative_upper_bound_usd": dollars(prior + encumbered),
                "phase_cap_usd": 0.5, "cumulative_cap_usd": 1.0}
