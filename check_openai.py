"""One real strict-JSON Responses API check; never prints credentials or headers."""

import json
import sys
from datetime import datetime, timezone

from catalog_harness.openai_http import MODEL, ROOT, OpenAIError, configuration, request_json


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean", "enum": [True]}},
          "required": ["ok"], "additionalProperties": False}


def main():
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "model": MODEL,
              "kind": "openai_connectivity_check", "usage": None, "passed": False}
    try:
        key, missing = configuration()
        if missing:
            print("FAIL: missing " + ", ".join(missing))
            report["missing"] = missing
            return 1
        result, details = request_json(key, schema=SCHEMA, schema_name="connectivity_check",
            instructions="Return the JSON object with ok set to true.", input_text="Check structured JSON output.",
            max_output_tokens=128)
        report.update(details)
        if set(result) != {"ok"} or result["ok"] is not True:
            raise OpenAIError("response", "validation_failed", details["usage"])
        report["passed"] = True
        print("PASS: model={} usage={}".format(report["model"], json.dumps(report["usage"], sort_keys=True)))
        return 0
    except OpenAIError as error:
        report.update(category=error.category, code=error.code, usage=error.usage)
        print("FAIL: model={} category={} code={} usage={}".format(MODEL, error.category, error.code,
              json.dumps(error.usage, sort_keys=True)))
        return 1
    except Exception:
        report.update(category="local", code="check_failed")
        print("FAIL: model={} category=local code=check_failed usage=null".format(MODEL))
        return 1
    finally:
        # This artifact contains only allowlisted status and numeric usage.
        try:
            directory = ROOT / "artifacts"
            directory.mkdir(exist_ok=True)
            (directory / "openai-check.json").write_text(json.dumps(report, indent=2) + "\n")
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
