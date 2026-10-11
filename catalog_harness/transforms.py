"""A small JSON language interpreted by deterministic Python, never eval/exec."""

import math
import re
from decimal import (Context, Decimal, DecimalException, DivisionByZero,
                     InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext)


UNIT_FACTORS = {"g": Decimal("1"), "gram": Decimal("1"), "grams": Decimal("1"),
                "kg": Decimal("1000"), "kilogram": Decimal("1000"),
                "kilograms": Decimal("1000"), "lb": Decimal("453.59237"),
                "lbs": Decimal("453.59237"), "pound": Decimal("453.59237"),
                "pounds": Decimal("453.59237")}
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
QUANTITY = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z]+)?\s*$", re.ASCII)
WEIGHT_CONTEXT = Context(prec=64, rounding=ROUND_HALF_EVEN, Emin=-999999,
                         Emax=999999, traps=[InvalidOperation, DivisionByZero, Overflow])


class InvalidPlan(ValueError):
    pass


class Quarantine(ValueError):
    pass


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"version", "suppliers"}:
        raise InvalidPlan("Plan must contain only version and suppliers")
    if type(plan["version"]) is not int or plan["version"] != 1:
        raise InvalidPlan("Unsupported plan version")
    suppliers = plan["suppliers"]
    if not isinstance(suppliers, dict) or not 1 <= len(suppliers) <= 20:
        raise InvalidPlan("Plan requires 1 to 20 supplier mappings")
    for supplier, spec in suppliers.items():
        if not isinstance(supplier, str) or not FIELD.fullmatch(supplier):
            raise InvalidPlan("Invalid supplier identifier")
        if not isinstance(spec, dict) or set(spec) != {"fields", "string_cleanup", "numeric_parser", "on_ambiguity"}:
            raise InvalidPlan("Unsupported transformation operation")
        fields = spec["fields"]
        if not isinstance(fields, dict) or set(fields) != {"product_id", "name", "weight", "unit"}:
            raise InvalidPlan("Invalid field mapping")
        for target, source in fields.items():
            if source is None and target == "unit":
                continue
            if not isinstance(source, str) or not FIELD.fullmatch(source):
                raise InvalidPlan("Only simple source field names are allowed")
        steps = spec["string_cleanup"]
        if not isinstance(steps, list) or len(steps) > 2 or any(s not in ("strip", "collapse_whitespace") for s in steps):
            raise InvalidPlan("Unsupported string cleanup")
        if len(set(steps)) != len(steps):
            raise InvalidPlan("Duplicate cleanup step")
        if spec["numeric_parser"] != "decimal_dot" or spec["on_ambiguity"] != "quarantine":
            raise InvalidPlan("Only decimal_dot parsing and quarantine on ambiguity are allowed")
    return plan


def clean_string(value, steps):
    if not isinstance(value, str) or not value.strip():
        raise Quarantine("missing_identity_or_name")
    for step in steps:
        if step == "strip":
            value = value.strip()
        elif step == "collapse_whitespace":
            value = " ".join(value.split())
    return value


def unit_factor(value):
    if not isinstance(value, str) or value.strip().lower() not in UNIT_FACTORS:
        raise Quarantine("unknown_unit")
    return UNIT_FACTORS[value.strip().lower()]


def weight_in_grams(value, record_unit, metadata):
    suffix = None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Quarantine("invalid_weight")
    if isinstance(value, str):
        match = QUANTITY.fullmatch(value)
        if not match:
            raise Quarantine("invalid_or_ambiguous_number")
        number, suffix = match.groups()
    else:
        number = str(value)
    try:
        amount = Decimal(number)
    except InvalidOperation:
        raise Quarantine("invalid_weight")
    if not amount.is_finite() or amount <= 0:
        raise Quarantine("invalid_weight")
    # Explicit evidence overrides a documented default; conflicting explicit
    # evidence must never silently choose one unit over the other.
    explicit = record_unit is not None and record_unit != ""
    if isinstance(record_unit, str) and not record_unit.strip():
        explicit = False
    factors = []
    if explicit:
        factors.append(unit_factor(record_unit))
    if suffix:
        factors.append(unit_factor(suffix))
    if len(set(factors)) > 1:
        raise Quarantine("conflicting_units")
    if not factors:
        default = metadata.get("default_weight_unit")
        documentation = metadata.get("unit_documentation")
        if not default or not isinstance(documentation, str) or not documentation.strip():
            raise Quarantine("missing_unit_evidence")
        factors.append(unit_factor(default))
    # Conversion must not depend on a caller's mutable Decimal context. Extreme
    # magnitudes are invalid records, rather than errors that abort a whole run.
    try:
        with localcontext(WEIGHT_CONTEXT):
            grams = float(amount * factors[0])
    except (DecimalException, OverflowError, ValueError):
        raise Quarantine("invalid_weight")
    if not math.isfinite(grams) or grams <= 0:
        raise Quarantine("invalid_weight")
    return grams


def transform_record(record, plan, metadata):
    """Return a new result; neither raw record nor metadata is modified."""
    validate_plan(plan)
    result = {key: record[key] for key in ("dataset_id", "supplier_id", "source_record_id")}
    spec = plan["suppliers"].get(record["supplier_id"])
    try:
        if spec is None:
            raise Quarantine("supplier_not_in_plan")
        fields = spec["fields"]
        payload = record["payload"]
        if not isinstance(payload, dict):
            raise Quarantine("invalid_payload")
        product = {"product_id": clean_string(payload.get(fields["product_id"]), spec["string_cleanup"]),
                   "name": clean_string(payload.get(fields["name"]), spec["string_cleanup"]),
                   "weight_g": weight_in_grams(payload.get(fields["weight"]),
                                               payload.get(fields["unit"]), metadata)}
        result.update(status="normalized", product=product)
    except Quarantine as exc:
        result.update(status="quarantined", reason=str(exc))
    return result
