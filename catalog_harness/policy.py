"""Harness choices are separate from plans and immutable safety/evaluation rules."""

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class HarnessPolicy:
    version: str = "fixture-v1"
    sampling_strategy: str = "per_supplier"
    samples_per_supplier: int = 8
    context_fields: tuple = ("payload", "supplier_metadata")
    verification_steps: tuple = ("unit_evidence", "readback")

    def __post_init__(self):
        if not self.version or len(self.version) > 64:
            raise ValueError("Invalid policy version")
        if self.sampling_strategy not in ("per_supplier", "reverse_per_supplier"):
            raise ValueError("Unsupported sampling strategy")
        if type(self.samples_per_supplier) is not int or not 1 <= self.samples_per_supplier <= 50:
            raise ValueError("Sample budget must be between 1 and 50 per supplier")
        if not set(self.context_fields) <= {"payload", "supplier_metadata", "development_failures"}:
            raise ValueError("Unsupported context selection")
        if not {"unit_evidence", "readback"} <= set(self.verification_steps):
            raise ValueError("Required safety verification cannot be removed")
        if not set(self.verification_steps) <= {"unit_evidence", "readback", "spot_check"}:
            raise ValueError("Unsupported verification step")

    def as_dict(self):
        return asdict(self)


class PlanProvider(Protocol):
    """Future adapter receives bounded records/metadata, never evaluator labels.

    Baseline and candidate must use the same adapter/model, tool capabilities,
    and comparable request/token budgets. Implementations return validated JSON,
    not executable code. An OpenAI HTTP adapter implements this interface without
    a provider SDK; real model access is checked separately from editor access.
    """

    def obtain_plan(self, samples: Sequence[Mapping[str, Any]],
                    supplier_metadata: Sequence[Mapping[str, Any]],
                    policy: HarnessPolicy) -> Dict[str, Any]:
        ...
