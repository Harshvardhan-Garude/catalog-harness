"""Freeze synthetic fixture inputs and the comparison contract before inference.

This manifest has only hashes, public policy text, and resource limits. It
contains no answer-key values and is never sent to a model. Verification reads
all three split files solely to hash bytes; it does not evaluate final records.
"""

import copy
import hashlib
import json
import os
from pathlib import Path

from catalog_harness.storage import ROOT
from enrichment_eval.fixtures import FIXTURE_VERSION, SPLIT_COUNTS, SPLITS, generate
from enrichment_eval.scoring import COMPARISON_OBJECTIVE, OBJECTIVE_VERSION, SCORING_VERSION

from .provider import BASELINE_POLICY


FREEZE_VERSION = "semantic-v1-freeze-1"
MODEL = "gpt-4.1-mini"
RESOURCE_CAPS = {
    "batch_size": 4,
    "enrichment_max_output_tokens": 2048,
    "proposal_max_output_tokens": 1024,
    "max_live_calls": 20,
    "planned_live_calls": 19,
    "request_timeout_seconds": 30,
    "automatic_retries": 0,
}
SOURCE_FILES = {
    "enrichment/taxonomy.json": ROOT / "enrichment" / "taxonomy.json",
    "enrichment_eval/fixtures.py": ROOT / "enrichment_eval" / "fixtures.py",
    "enrichment_eval/scoring.py": ROOT / "enrichment_eval" / "scoring.py",
    "enrichment/evidence.py": ROOT / "enrichment" / "evidence.py",
    "enrichment/provider.py": ROOT / "enrichment" / "provider.py",
}
DEFAULT_FIXTURES = ROOT / "data" / "enrichment-v1"
DEFAULT_ARTIFACTS = ROOT / "artifacts" / "enrichment-v1"


def manifest_digest(manifest):
    """Stable digest also links Atlas run records and the persistent call ledger."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _current_manifest(fixtures_root):
    paths = dict(SOURCE_FILES)
    for folder in ("agent_visible", "evaluator_only"):
        for split in SPLITS:
            key = "fixtures/{}/{}.jsonl".format(folder, split)
            paths[key] = Path(fixtures_root) / folder / (split + ".jsonl")
    hashes = {}
    for key, path in sorted(paths.items()):
        try:
            content = Path(path).read_bytes()
        except OSError:
            raise ValueError("Frozen experiment input is missing or unreadable") from None
        hashes[key] = hashlib.sha256(content).hexdigest()
    return {
        "version": FREEZE_VERSION,
        "fixture_version": FIXTURE_VERSION,
        "scoring_version": SCORING_VERSION,
        "objective_version": OBJECTIVE_VERSION,
        "split_counts": dict(SPLIT_COUNTS),
        "model": MODEL,
        "resource_caps": dict(RESOURCE_CAPS),
        "final_holdout_evaluated": False,
        "baseline_policy": copy.deepcopy(BASELINE_POLICY),
        "objective": copy.deepcopy(COMPARISON_OBJECTIVE),
        "sha256": hashes,
    }


def _read_frozen(artifact_dir):
    try:
        with (Path(artifact_dir) / "freeze.json").open(encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        raise ValueError("Experiment freeze is missing or invalid") from None
    if not isinstance(manifest, dict):
        raise ValueError("Experiment freeze is missing or invalid")
    return manifest


def verify(fixtures_root=DEFAULT_FIXTURES, artifact_dir=DEFAULT_ARTIFACTS):
    """Refuse changed inputs or contract; never regenerate or repair them."""
    frozen = _read_frozen(artifact_dir)
    current = _current_manifest(fixtures_root)
    if frozen != current:
        raise ValueError("Frozen experiment inputs or comparison contract differ")
    return frozen


def prepare(fixtures_root=DEFAULT_FIXTURES, artifact_dir=DEFAULT_ARTIFACTS):
    """Generate independent fixtures and exclusively freeze before live calls."""
    fixtures_root, artifact_dir = Path(fixtures_root), Path(artifact_dir)
    frozen_path = artifact_dir / "freeze.json"
    if frozen_path.exists():
        # Once frozen, even missing files must fail closed rather than being
        # silently regenerated before verification notices their absence.
        return verify(fixtures_root, artifact_dir)
    generate(fixtures_root)
    manifest = _current_manifest(fixtures_root)
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact_dir.chmod(0o700)
    content = json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n"
    # Exclusive creation prevents accidental replacement of an experiment freeze.
    try:
        descriptor = os.open(str(frozen_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return verify(fixtures_root, artifact_dir)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    return manifest
