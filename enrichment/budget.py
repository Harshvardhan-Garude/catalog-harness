"""One durable, fail-closed live-call budget for the complete experiment."""

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from catalog_harness.openai_http import OpenAIError, request_json


STAGES = {"baseline_development", "baseline_validation", "candidate_development",
          "candidate_validation", "policy_proposal"}


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class ExperimentBudget:
    """Reservations count before transmission; failures stop all further calls.

    A prior ledger is not resumed automatically: unknown in-flight requests must
    never be silently retried or escape accounting by restarting a helper.
    """

    def __init__(self, directory, api_key, max_calls=20, transport=None):
        if type(max_calls) is not int or not 1 <= max_calls <= 20:
            raise ValueError("Experiment budget must be 1 to 20 calls")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / "experiment.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError("Another experiment process holds the lock") from None
        self.path = self.directory / "calls.json"
        if self.path.exists():
            self.close()
            raise ValueError("Existing call ledger: no automatic restart or retry")
        self.api_key = api_key
        self.transport = transport or request_json
        self.ledger = {"max_calls": max_calls, "calls": [], "stopped": False, "model": None}
        write_json(self.path, self.ledger)

    def close(self):
        if not self.lock.closed:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()

    def request(self, stage, **kwargs):
        if stage not in STAGES:
            raise ValueError("Unknown experiment stage")
        if self.ledger["stopped"]:
            raise OpenAIError("budget", "experiment_stopped")
        if len(self.ledger["calls"]) >= self.ledger["max_calls"]:
            raise OpenAIError("budget", "call_limit")
        if type(kwargs.get("max_output_tokens")) is not int or not 1 <= kwargs["max_output_tokens"] <= 2048:
            raise OpenAIError("budget", "output_limit")
        ordinal = len(self.ledger["calls"]) + 1
        record = {"ordinal": ordinal, "stage": stage, "status": "reserved", "usage": None,
                  "started_at": datetime.now(timezone.utc).isoformat(),
                  "max_output_tokens": kwargs["max_output_tokens"]}
        self.ledger["calls"].append(record)
        # Synthetic public inputs and model outputs only; never API credentials.
        write_json(self.directory / ("request-{:02d}.json".format(ordinal)), {"stage": stage, **kwargs})
        write_json(self.path, self.ledger)
        try:
            output, details = self.transport(self.api_key, **kwargs)
            record.update(status="completed", model=details["model"], usage=details["usage"])
            write_json(self.directory / ("response-{:02d}.json".format(ordinal)), {"output": output, **details})
            if self.ledger["model"] is None:
                self.ledger["model"] = details["model"]
            elif self.ledger["model"] != details["model"]:
                raise OpenAIError("response", "model_changed", details["usage"])
            return output, details
        except OpenAIError as error:
            record.update(status="failed", category=error.category, code=error.code, usage=error.usage)
            self.ledger["stopped"] = True
            raise
        except Exception:
            record.update(status="failed", category="local", code="request_failed")
            self.ledger["stopped"] = True
            raise
        finally:
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            write_json(self.path, self.ledger)

    def summary(self):
        groups = {}
        totals = {key: 0 for key in ("input_tokens", "output_tokens", "total_tokens")}
        unknown_usage_calls = 0
        for call in self.ledger["calls"]:
            group = groups.setdefault(call["stage"], {"calls": 0, **{key: 0 for key in totals}})
            group["calls"] += 1
            usage = call["usage"]
            if usage is None:
                unknown_usage_calls += 1
            else:
                for key in totals:
                    group[key] += usage[key]
                    totals[key] += usage[key]
        return {"calls": len(self.ledger["calls"]), "max_calls": self.ledger["max_calls"],
                "model": self.ledger["model"],
                "tokens": totals, "unknown_usage_calls": unknown_usage_calls,
                "by_stage": groups, "stopped": self.ledger["stopped"]}
