"""One read-only Atlas ping with static, credential-free failure diagnostics."""

import errno
import json
import logging
import os
import platform
import socket
import ssl
import sys
from collections import Counter
from pathlib import Path

import dns.exception
import dns.resolver
import pymongo
from dotenv import dotenv_values, load_dotenv
from pymongo import MongoClient
from pymongo.errors import (ConfigurationError, InvalidURI, NetworkTimeout,
                            OperationFailure, ServerSelectionTimeoutError)

from catalog_harness.storage import ROOT


def _chain(error):
    pending, seen = [error], set()
    while pending and len(seen) < 16:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend((current.__cause__, current.__context__))


def sanitized_error(error):
    """Inspect typed causes only; never serialize messages, args, or details."""
    chain = list(_chain(error))
    for item in chain:
        if isinstance(item, ssl.SSLCertVerificationError):
            return {"category": "tls", "code": "certificate_verification_failed"}
        if isinstance(item, ssl.SSLError):
            reason = getattr(item, "reason", None)
            code = {"TLSV1_ALERT_INTERNAL_ERROR": "peer_internal_alert",
                    "TLSV1_ALERT_PROTOCOL_VERSION": "protocol_version",
                    "SSLV3_ALERT_HANDSHAKE_FAILURE": "handshake_failed",
                    "UNEXPECTED_EOF_WHILE_READING": "unexpected_eof",
                    "WRONG_VERSION_NUMBER": "wrong_protocol",
                    "CERTIFICATE_VERIFY_FAILED": "certificate_verification_failed"}.get(reason, "handshake_failed")
            return {"category": "tls", "code": code}
        if isinstance(item, (socket.gaierror, dns.resolver.NXDOMAIN, dns.resolver.NoAnswer)):
            return {"category": "dns", "code": "resolution_failed"}
        if isinstance(item, dns.exception.Timeout):
            return {"category": "dns", "code": "timeout"}
        if isinstance(item, dns.exception.DNSException):
            return {"category": "dns", "code": "resolution_failed"}
        if isinstance(item, OperationFailure) and item.code in (13, 18):
            return {"category": "atlas", "code": "authentication_failed" if item.code == 18 else "permission_denied"}
    for item in chain:
        if isinstance(item, (socket.timeout, TimeoutError, NetworkTimeout)):
            return {"category": "network", "code": "timeout"}
        if isinstance(item, OSError):
            code = {errno.ECONNREFUSED: "connection_refused", errno.ECONNRESET: "connection_reset",
                    errno.ENETUNREACH: "network_unreachable", errno.EHOSTUNREACH: "host_unreachable",
                    errno.ETIMEDOUT: "timeout"}.get(item.errno)
            if code:
                return {"category": "network", "code": code}
    if isinstance(error, InvalidURI):
        return {"category": "configuration", "code": "invalid_uri"}
    if isinstance(error, ConfigurationError):
        return {"category": "configuration", "code": "invalid_configuration"}
    if isinstance(error, ServerSelectionTimeoutError):
        return {"category": "atlas", "code": "server_selection_timeout"}
    return {"category": "atlas", "code": "operation_failed"}


def server_failures(client):
    """Use descriptions' values only; topology keys contain server addresses."""
    descriptions = list(client.topology_description.server_descriptions().values())
    counts = Counter()
    for description in descriptions:
        if description.error is not None:
            safe = sanitized_error(description.error)
            counts[(safe["category"], safe["code"])] += 1
    return {"discovered_servers": len(descriptions), "server_failures": [
        {"category": category, "code": code, "count": count}
        for (category, code), count in sorted(counts.items())]}


def configuration_metadata():
    """Match project dotenv precedence; report names/presence, never values."""
    names = ("MONGODB_URI", "MONGODB_DATABASE")
    before = {name: os.environ.get(name) for name in names}
    file_values = dotenv_values(ROOT / ".env", interpolate=False)
    load_dotenv(ROOT / ".env", override=False)
    metadata = {}
    for name in names:
        file_value = file_values.get(name)
        value = os.environ.get(name, "").strip()
        comparable = file_value is not None and "${" not in file_value
        metadata[name] = {"file_present": bool(file_value), "process_present": before[name] is not None,
            "effective_usable": bool(value) and not value.startswith("YOUR_"),
            "effective_source": "process_environment" if before[name] is not None else "project_dotenv",
            "process_matches_file": (before[name] == file_value) if before[name] is not None and comparable else None}
    return metadata


def main():
    # Process-local command: suppress driver children as well as parent loggers.
    logging.disable(logging.CRITICAL)
    report = {"kind": "read_only_atlas_diagnostic", "python": platform.python_version(),
              "interpreter": sys.executable, "working_directory": str(Path.cwd()),
              "configuration_file": str(ROOT / ".env"), "pymongo": pymongo.version,
              "openssl": ssl.OPENSSL_VERSION, "model_calls": 0, "stage": "configuration"}
    client = None
    try:
        report["configuration"] = configuration_metadata()
        missing = [name for name, state in report["configuration"].items() if not state["effective_usable"]]
        if missing:
            report.update(status="FAIL", missing=missing)
        else:
            report["stage"] = "client_creation"
            client = MongoClient(os.environ["MONGODB_URI"].strip(), serverSelectionTimeoutMS=10000,
                                 connectTimeoutMS=10000, socketTimeoutMS=10000, timeoutMS=20000,
                                 appname="catalog-harness-diagnostic")
            report["stage"] = "ping"
            client.admin.command("ping")
            report.update(status="PASS", **server_failures(client))
    except Exception as error:
        report.update(status="FAIL", error=sanitized_error(error))
        if client is not None:
            try:
                report.update(server_failures(client))
            except Exception:
                report["topology_diagnostics_available"] = False
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as error:
                report.update(status="FAIL", close_error=sanitized_error(error))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
