# Catalog Harness

Catalog Harness turns inconsistent supplier catalogs into structured product
records with evidence for each prediction. It combines deterministic data
normalization, LLM enrichment, output validation, and a review workflow.

[Architecture](docs/architecture.md) · [Saved results and evidence](docs/README.md)

## How it works

1. Preserve supplier records and normalize copies into a common format.
2. Ask an LLM for product categories and uses under a fixed taxonomy.
3. Validate structured output and check that cited excerpts occur in the source.
4. Leave affected fields unknown when their evidence fails validation.
5. Evaluate correctness and abstention against separately held-out answers.
6. Map supported predictions into store-specific categories for review.

The interactive viewer displays saved experiment results. Search, filtering,
and store previews use those saved predictions.

## Recorded ML experiment

The original experiment used GPT-4.1 mini on synthetic supplier records. The
candidate added guidance for conflicting sources and changed the handling of
invalid citations. The model and scoring rules stayed fixed.

| Split | Correct field decisions, baseline → candidate | Unsupported assertions |
| --- | --- | --- |
| Validation, 12 products | 33/36 → 34/36 | 4 → 0 |
| Final holdout, 24 products | 68/72 → 69/72 | 2 → 1 |

The candidate was selected using validation before evaluating the final holdout.
The two changes were tested together. One unsupported assertion remained on
the final set, illustrating why citation validity and semantic correctness
need separate checks.

These small synthetic experiments measure the recorded comparison. The
[results index](docs/README.md) links the policy, sanitized prediction example,
evaluation reports, and limitations.

## Inspect or run locally

The committed reports in [docs/](docs/README.md) can be read directly on GitHub.

For a fresh checkout, create an isolated Python environment and install the
dependencies.

```bash
git clone https://github.com/Harshvardhan-Garude/catalog-harness.git
cd catalog-harness
python -m venv .venv
```

Windows PowerShell

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m unittest discover -s tests -q
```

Linux or macOS

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -q
```

The review on November 27, 2026 passed all 201 offline tests on Linux with
Python 3.12. The interactive viewer needs the original saved data or complete
local artifacts. See [the architecture and setup requirements](docs/architecture.md#saved-results-web-application).

## Further implementation details

- [Normalization and validation](docs/architecture.md#deterministic-normalization)
- [Structured LLM outputs and evidence checks](docs/architecture.md#semantic-enrichment-and-evidence)
- [Store placement](docs/architecture.md#store-placement)
- [Separate deterministic database benchmark](docs/README.md#database-benchmark)

The database benchmark measures processing and queries on synthetic records.
Its workload is separate from the ML evaluation above.
