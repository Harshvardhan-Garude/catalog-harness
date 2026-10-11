"""Reproducible synthetic data and private labels, generated independently of plans."""

import json
import random
from pathlib import Path
from uuid import UUID


SUPPLIERS = (
    {"supplier_id": "northstar", "display_name": "Northstar Supply",
     "unit_documentation": "Weights carry an explicit unit; there is no default."},
    {"supplier_id": "cedar", "display_name": "Cedar Wholesale", "default_weight_unit": "kg",
     "unit_documentation": "Cedar export specification: mass is kg unless the row declares a unit."},
    {"supplier_id": "harbor", "display_name": "Harbor Goods",
     "unit_documentation": "Units must be established by each row; there is no default."},
)
FIELDS = {"northstar": ("sku", "title", "weight", "weight_unit"),
          "cedar": ("product_code", "description", "mass", "measurement_unit"),
          "harbor": ("item_id", "label", "shipping_weight", "uom")}
# Independent ground-truth constants, intentionally not imported from tools.
FACTORS = {"g": 1.0, "kg": 1000.0, "lb": 453.59237}
GENERATOR_VERSION = 1


def generate_batch(split, seed, size):
    if (split not in ("development", "final") or type(seed) is not int
            or type(size) is not int or not 30 <= size <= 1000000):
        raise ValueError("Use development/final, an integer seed, and 30 to 1000000 records")
    rng = random.Random(seed)
    dataset_id = "catalog-v{}-{}-s{}-n{}".format(GENERATOR_VERSION, split, seed, size)
    counts = [int(size * .65), int(size * .25)]
    counts.append(size - sum(counts))
    rows, labels = [], []
    for supplier, count in zip(SUPPLIERS, counts):
        sid = supplier["supplier_id"]
        for _ in range(count):
            source_id = str(UUID(int=rng.getrandbits(128), version=4))
            product_id = "P-" + format(rng.getrandbits(48), "012x")
            name = rng.choice(("Travel Mug", "Desk Lamp", "Garden Kit", "Coffee Tin", "Cable Pack"))
            amount = rng.randint(1, 8000) / 1000.0
            unit = rng.choice(("g", "kg", "lb"))
            # Scenarios are independent of public IDs and are never emitted.
            scenario = rng.choices(("valid", "whitespace", "suffix", "default", "missing_unit", "conflict", "ambiguous_number"),
                                   weights=(30, 25, 15, 10, 10, 5, 5))[0]
            keys = FIELDS[sid]
            payload = dict(zip(keys, (product_id, name, amount, unit)))
            if scenario == "whitespace":
                payload[keys[0]] = "  " + product_id + "  "
                payload[keys[1]] = " \t" + name.replace(" ", "   ") + "  "
                payload[keys[2]] = "  {:.3f}  ".format(amount)
                payload[keys[3]] = " " + unit.upper() + " "
            elif scenario == "suffix":
                payload[keys[2]] = "{:.3f} {}".format(amount, {"g": "grams", "kg": "kg", "lb": "lbs"}[unit])
                del payload[keys[3]]
            elif scenario in ("default", "missing_unit"):
                del payload[keys[3]]
                if sid == "cedar":
                    unit = "kg"
            elif scenario == "conflict":
                other = "kg" if unit != "kg" else "lb"
                payload[keys[2]] = "{:.3f} {}".format(amount, other)
            elif scenario == "ambiguous_number":
                payload[keys[2]] = "1,234"
            ambiguous = scenario in ("conflict", "ambiguous_number") or (
                scenario in ("default", "missing_unit") and sid != "cedar")
            raw = {"dataset_id": dataset_id, "supplier_id": sid,
                   "source_record_id": source_id, "payload": payload}
            label = {key: raw[key] for key in ("dataset_id", "supplier_id", "source_record_id")}
            if ambiguous:
                label["expected_status"] = "quarantined"
            else:
                label.update(expected_status="normalized", product={"product_id": product_id,
                             "name": name, "weight_g": amount * FACTORS[unit]})
            rows.append(raw)
            labels.append(label)
    rng.shuffle(rows)
    rng.shuffle(labels)
    return dataset_id, rows, labels


def generate(root=Path("data"), size=300, dev_seed=1101, final_seed=2202):
    if dev_seed == final_seed:
        raise ValueError("Development and final seeds must differ")
    manifest = []
    for split, seed in (("development", dev_seed), ("final", final_seed)):
        dataset, rows, labels = generate_batch(split, seed, size)
        public = root / "agent_visible" / dataset
        private = root / "evaluator_only" / dataset
        # Avoid replacing a different dataset or hand-edited generated files.
        public.mkdir(parents=True, exist_ok=True)
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        private.chmod(0o700)
        documents = {
            public / "records.jsonl": "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
            public / "suppliers.json": json.dumps(list(SUPPLIERS), indent=2, sort_keys=True) + "\n",
            public / "manifest.json": json.dumps({"dataset_id": dataset, "split": split, "size": size,
                                                   "generator_version": GENERATOR_VERSION}, indent=2) + "\n",
            private / "labels.jsonl": "".join(json.dumps(r, sort_keys=True) + "\n" for r in labels),
        }
        for path, content in documents.items():
            if path.exists() and path.read_text() != content:
                raise ValueError("Existing generated data differs; use a new seed")
            if not path.exists():
                path.write_text(content)
        (private / "labels.jsonl").chmod(0o600)
        manifest.append({"dataset_id": dataset, "split": split, "size": size})
    return manifest
