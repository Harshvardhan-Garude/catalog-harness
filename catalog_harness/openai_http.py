"""Single-attempt Responses API HTTP transport using only Python's standard library.

Official contract: https://developers.openai.com/api/docs/guides/structured-outputs
No redirects, retry loops, provider SDK, or unverified TLS connections.
"""

import json
import logging
import re
import socket
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://api.openai.com/v1/responses"
MODEL = "gpt-4.1-mini"
TIMEOUT = 30
MAX_RESPONSE_BYTES = 1024 * 1024


class OpenAIError(Exception):
    """Only allowlisted categories/codes are safe to print or persist."""

    def __init__(self, category, code, usage=None):
        self.category = category
        self.code = code
        self.usage = usage
        super().__init__(category + "/" + code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configuration():
    logging.getLogger("dotenv.main").disabled = True
    values = dotenv_values(ROOT / ".env")
    values = {name: (values.get(name) or "").strip() for name in ("OPENAI_API_KEY", "OPENAI_MODEL")}
    missing = [name for name, value in values.items() if not value or value.startswith("YOUR_")]
    if missing:
        return None, missing
    if values["OPENAI_MODEL"] != MODEL:
        raise OpenAIError("configuration", "model_mismatch")
    return values["OPENAI_API_KEY"], []


def token_usage(response):
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "output_tokens", "total_tokens")
    if any(type(usage.get(key)) is not int or usage[key] < 0 for key in keys):
        return None
    return {key: usage[key] for key in keys}


def http_error(error):
    category = {400: "invalid_request", 401: "authentication", 403: "permission",
                404: "not_found", 429: "quota_or_rate_limit"}.get(error.code, "http_error")
    code = "http_{}".format(error.code)
    allowed = {"invalid_api_key", "insufficient_quota", "rate_limit_exceeded", "model_not_found",
               "invalid_request_error", "unsupported_parameter", "invalid_json_schema",
               "context_length_exceeded", "model_access_denied"}
    try:
        payload = json.loads(error.read(MAX_RESPONSE_BYTES))
        candidate = payload.get("error", {}).get("code")
        if isinstance(candidate, str) and candidate in allowed:
            code = candidate
    except Exception:
        pass
    finally:
        error.close()
    return OpenAIError(category, code)


def request_json(api_key, *, schema, schema_name, instructions, input_text, max_output_tokens):
    """One POST, one complete JSON object, bounded bytes/tokens, never retries."""
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 2048:
        raise OpenAIError("configuration", "output_budget")
    body = {"model": MODEL, "store": False, "max_output_tokens": max_output_tokens,
            "instructions": instructions, "input": input_text,
            "text": {"format": {"type": "json_schema", "name": schema_name,
                                "strict": True, "schema": schema}}}
    encoded = json.dumps(body, allow_nan=False).encode("utf-8")
    if len(encoded) > 65536:
        raise OpenAIError("configuration", "input_budget")
    request = Request(ENDPOINT, data=encoded, method="POST", headers={
        "Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=TIMEOUT) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise OpenAIError("response", "too_large")
        result = json.loads(data)
    except HTTPError as error:
        raise http_error(error) from None
    except (socket.timeout, TimeoutError):
        raise OpenAIError("network", "timeout") from None
    except ssl.SSLError:
        raise OpenAIError("network", "tls") from None
    except URLError as error:
        reason = error.reason
        code = "tls" if isinstance(reason, ssl.SSLError) else "timeout" if isinstance(reason, (socket.timeout, TimeoutError)) else "connection"
        raise OpenAIError("network", code) from None
    except (ValueError, UnicodeError):
        raise OpenAIError("response", "invalid_json") from None
    except OSError:
        raise OpenAIError("network", "connection") from None
    if not isinstance(result, dict):
        raise OpenAIError("response", "invalid_shape")
    usage = token_usage(result)
    if result.get("status") != "completed":
        raise OpenAIError("response", "incomplete", usage)
    text = []
    output = result.get("output")
    if not isinstance(output, list):
        raise OpenAIError("response", "invalid_shape", usage)
    for item in output:
        if not isinstance(item, dict):
            raise OpenAIError("response", "invalid_shape", usage)
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise OpenAIError("response", "invalid_shape", usage)
        for part in content:
            if not isinstance(part, dict):
                raise OpenAIError("response", "invalid_shape", usage)
            if part.get("type") == "refusal":
                raise OpenAIError("response", "refusal", usage)
            if part.get("type") == "output_text":
                if not isinstance(part.get("text"), str):
                    raise OpenAIError("response", "invalid_shape", usage)
                text.append(part["text"])
    try:
        def unique_keys(pairs):
            value = {}
            for key, entry in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = entry
            return value
        structured = json.loads("".join(text), object_pairs_hook=unique_keys)
    except (ValueError, TypeError):
        raise OpenAIError("response", "invalid_structured_json", usage) from None
    if not isinstance(structured, dict):
        raise OpenAIError("response", "invalid_shape", usage)
    actual_model = result.get("model", "")
    if not isinstance(actual_model, str) or not re.fullmatch(r"gpt-4\.1-mini(?:-\d{4}-\d{2}-\d{2})?", actual_model):
        raise OpenAIError("response", "unexpected_model", usage)
    if usage is None:
        raise OpenAIError("response", "missing_usage")
    return structured, {"model": actual_model, "usage": usage}
