# Catalog Harness

Catalog Harness normalizes inconsistent supplier product catalogs and quarantines
records whose meaning cannot be established safely. The intended agent loop is:
inspect → propose bounded transformations → execute on copies → evaluate → propose
a harness-policy change → retain or reject after independent evaluation.

Harness policies will control sampling, context selection, and verification, never
the evaluator, safety constraints, or model weights. A bounded OpenAI adapter is
prepared; automatic policy improvement and Raindrop integration remain future work.

## Verification status — October 11, 2026

- All **37 focused local tests** pass, including mocked OpenAI transport/provider
  checks. Mocked responses do not establish real model access.
- Atlas passed the single connection check from the managed Codex runtime: ping,
  unique setup-document insert/read/delete. The 300-record **hand-authored fixture**
  then passed against Atlas: 213/213 recoverable records normalized and 87/87
  ambiguous records quarantined, with zero incorrect, unresolved, missing,
  duplicate, or unexpected outputs. Results were re-read and evaluated from a
  separate process, and the current raw digest matched the stored run snapshot.
  Repeated seeding kept 300 records and the same raw SHA-256 digest. Fixture run:
  `dd1b9a927edd410ca6e7b79fe68a687d`.
- The independent OpenAI check made **one** real Responses API request using
  `gpt-4.1-mini`, strict JSON, `store=false`, 128 output tokens, a 30-second HTTP
  timeout, and no retries. It returned **HTTP 429** (`quota_or_rate_limit/http_429`);
  no token usage was returned. This does not establish successful model access.
  No AI baseline requests were made. Inspect the API project's billing/quota and
  rate-limit status before manually running the check again.
- The provider adapter and development baseline command are implemented and
  verified with mocked responses only. A real generated plan/baseline remains
  unverified. Final holdout data has not been used in live runs or policy selection.
- The foundation commit is retained; this milestone is committed separately.
  GitHub CLI is unavailable, no remote is configured, and no push has occurred.

The managed runtime and your normal terminal can have different network access.
For comparison, run from `/home/harsh/Desktop/MongoDB_Hackathon` with
`.venv/bin/python` (Python 3.8.10). The Atlas checker loads `.env` beside its script;
existing process variables override dotenv values. The OpenAI checker reads only
the project `.env`. Never print those values while comparing environments. No
Atlas settings or TLS verification were changed.

## Run the milestone

Use the existing Python 3.8 virtual environment. Dependencies are pinned in
`requirements.txt`; no system Python or global Git settings are changed.

Keep `MONGODB_URI` and `MONGODB_DATABASE` in the ignored local `.env`, using
`.env.example` as the placeholder template. Never commit credentials.

From the repository root, reproduce the small development demonstration:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m catalog_harness generate --size 300
.venv/bin/python -m catalog_harness validate-plan
.venv/bin/python -m catalog_harness seed --dataset catalog-v1-development-s1101-n300
.venv/bin/python -m catalog_harness seed --dataset catalog-v1-development-s1101-n300
.venv/bin/python -m catalog_harness summary --dataset catalog-v1-development-s1101-n300
.venv/bin/python -m catalog_harness sample --dataset catalog-v1-development-s1101-n300 --supplier harbor --limit 8
.venv/bin/python -m catalog_harness run-fixture --dataset catalog-v1-development-s1101-n300
```

The two seed calls must print the same record count and SHA-256 digest. Seeding
uses insert-only upserts; changed input with an existing dataset identity is
rejected. Running the fixture creates a new run ID, streams raw records through
the tools, writes results, verifies the raw-data digest, then independently reads
and evaluates the stored results. It does not change or remove raw records.
MongoDB failures return exit code 1 with sanitized output. Evaluation rejection
returns 2. Success returns 0. Never disable certificate checks to fix connectivity.

`generate` creates **300 records per split**, with separate default seeds 1101
and 2202. Change `--size`, `--dev-seed`, and `--final-seed` together with the
dataset IDs in subsequent commands to run a different reproducible batch.
Generation refuses to overwrite files whose contents differ. Dependencies stay
at the versions in `requirements.txt`; restoring them, if necessary, uses
`.venv/bin/python -m pip install -r requirements.txt`.

## Schema and architecture

Raw envelopes retain `dataset_id`, `supplier_id`, `source_record_id`, and the
unaltered supplier `payload`. A normalized result retains those identifiers plus:

```json
{"status": "normalized", "product": {"product_id": "P-123", "name": "Travel Mug", "weight_g": 250.0}}
```

Quarantined results carry `status: "quarantined"` and a machine-readable reason,
with no product value. Three suppliers have different source field names and a
65% / 25% / 10% representation. Data includes numbers and numeric strings,
whitespace, g/kg/lb units, unit suffixes, documented metadata defaults, conflicting
units, missing unit evidence, and ambiguous comma notation. IDs are random and
contain no scenario or answer annotations.

| Part | Responsibility |
| --- | --- |
| `catalog_harness/transforms.py` | Validate and interpret bounded JSON plans; no model code execution |
| `catalog_harness/storage.py` | Atlas access, insert-only seeding, bounded samples/bulk writes, summaries |
| `catalog_harness/pipeline.py` | Execute copies, record run provenance, verify raw-data integrity |
| `catalog_harness/policy.py` | Separate harness policy and future `PlanProvider` interface |
| `catalog_harness/openai_http.py` | Single-attempt standard-library Responses API transport |
| `catalog_harness/openai_provider.py` | Bounded structured plan generation; no label or fixture access |
| `evaluation/generate.py` | Synthetic public records and separate evaluator-only labels |
| `evaluation/evaluator.py` | Independent scoring, fixed tolerances and acceptance rules |
| `fixtures/plan.json` | **Hand-authored fixture**, not an AI baseline or learned policy |

Plans allow field mapping, `strip` / `collapse_whitespace`, `decimal_dot` parsing,
g/kg/lb conversion, and quarantine. They reject extra operations, unsafe field
paths, duplicate JSON keys, unsupported units, and plans larger than 32 KiB.
No `eval`, model-generated Python, shell operations, or arbitrary expressions are
executed. Unit defaults come only from documented supplier metadata, never from a
plan constant. Unknown or conflicting evidence requires quarantine.

Collections are `catalog_harness_raw`, `catalog_harness_suppliers`,
`catalog_harness_results`, `catalog_harness_runs`, `catalog_harness_policies`, and
`catalog_harness_datasets`. Unique indexes cover dataset/supplier/source identity,
dataset/supplier metadata, and run/result identity. Samples return at most 50
records per supplier; cursor and output-write batches hold at most 100 records.
Summaries aggregate record counts by supplier. There are no database-drop or
catalog-delete operations. Each fixture execution is a separate auditable run;
repeating seeding creates no duplicate raw records.

## Independent evaluation and holdout

Generated files are ignored by Git:

- `data/agent_visible/<dataset>/`: records, supplier metadata, manifest.
- `data/evaluator_only/<dataset>/labels.jsonl`: expected products and quarantine
  decisions; readable only by the current OS user, outside the tool/context data.
- `artifacts/<run_id>.json` and `artifacts/events.jsonl`: local evaluation and run
  events, with identifiers and metrics rather than credentials or raw records.

The evaluator reports counts and explicit denominators overall and by supplier:
correct normalization / recoverable records, correct quarantine / ambiguous
records, unnecessarily unresolved / recoverable records, and incorrect outputs /
expected records. Missing outputs form a separate category. Duplicate extras and
unexpected outputs are additional diagnostics; they fail acceptance rather than
being silently deduplicated. Weight comparison uses absolute tolerance **1e-6 g**
and relative tolerance **1e-9**. Boolean/nonfinite/string weights are invalid.

Acceptance requires zero incorrect, missing, duplicate, or unexpected outputs,
all ambiguous records quarantined, and at least 95% correct recoverable coverage
both overall and per supplier. The future candidate guard additionally requires
the identical development-record identities, no supplier coverage regression,
and a strict increase in correctly normalized recoverable records. It refuses
final-holdout reports. Comparable model/tool budgets are an orchestration
requirement for the next milestone, not something this fixture claims to verify.

Development failure evidence contains IDs and failure categories, not expected
answers. Final data cannot be sampled through exploration tools. Do not pass the
generator, evaluator code, evaluator-only directory, or final reports to the
future agent. Directory separation is **not** an OS security boundary for another
process running as the same user; the next agent runner must expose only the
allowlisted data/tool interface, with no unrestricted filesystem or database
access. The prepared provider receives only selected development samples and
supplier metadata; it has no filesystem, database, evaluator, or execution tools.

After policy selection is finished and frozen, an operator may separately seed
the final batch and run `final-report`. That command evaluates frozen plans and
emits aggregates without per-record failure evidence. Do not run it while tuning:

```bash
.venv/bin/python -m catalog_harness seed --dataset catalog-v1-final-s2202-n300
.venv/bin/python -m catalog_harness final-report --dataset catalog-v1-final-s2202-n300
```

Re-evaluate a stored development run with
`.venv/bin/python -m catalog_harness evaluate --run RUN_ID`.

## Current status and next milestone

The deterministic tools, synthetic generation, database operations, policy/plan
interfaces, independent evaluator, local event recording, and OpenAI adapter are implemented.
This fixture demonstrates plumbing, not AI ability, learning, or improvement.
There is no provider SDK, automatic policy search, Raindrop integration, UI, vector
search, deployment, or agent framework. Codex editor access does not establish
application model access. This synthetic schema covers weights and product names;
it does not yet model arbitrary supplier formats or multilingual numeric locales.
Generation and evaluation keep labels/identities in memory; transformation and
database I/O stream records. This is suitable for hundreds of records first.

After resolving OpenAI API access, set `OPENAI_API_KEY` and
`OPENAI_MODEL=gpt-4.1-mini` in the ignored local `.env` and run:

```bash
.venv/bin/python check_openai.py
# Only after the OpenAI check and Atlas connection check pass:
.venv/bin/python -m catalog_harness run-ai-baseline --dataset catalog-v1-development-s1101-n300
```

The OpenAI check validates exactly `{"ok": true}` and prints only PASS/FAIL, model,
numeric usage, or sanitized error category/code. It records the same safe status
in `artifacts/openai-check.json`. The implementation follows the official
[Responses API structured-output contract](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).

The baseline command uses **one** additional model request (within the authorized
maximum of three), at most eight records per supplier across three suppliers,
24 KiB of sample/metadata JSON, 2048 output tokens, a 30-second HTTP timeout,
`store=false`, and no retries or HTTP redirects. It never loads a fixture plan or
expected answers into model context. Every returned plan passes the existing
allowlist and observed-field validation before deterministic execution. Final
datasets are rejected before any model request. The full validated plan, actual
model and usage, policy ID, raw digest, and evaluation are stored with
`kind: "ai_development_baseline"`, separately from `hand_authored_fixture` runs.
Attempted model-request status/usage is also recorded in the ignored
`artifacts/model-requests.jsonl`, including failure attempts. A blocked request or
invalid plan creates no claimed AI evaluation result.

The next smallest step is one real development baseline after the API check
passes. Then add policy proposals using
development failures with the model, tools, and budgets held comparable. Keep the
evaluator/safety code fixed and final labels isolated. Add Raindrop tracing after
the first provider-backed run works.

## Git and publishing

Local history uses the existing author identity and real commit timestamps on
`main`. Secrets, virtual environments, caches, generated data, and local artifacts
are ignored. No global Git configuration is changed.

If GitHub CLI is unavailable, sign in to the intended personal account in the
browser and open <https://github.com/new>. Create a **new public** repository named
`catalog-harness`, without a README, license, or `.gitignore`. If the name already
exists, stop and choose a new name; do not reuse unrelated history. After creating
the empty repository, add its clone URL as `origin` and push `main` using your
existing Git authentication. Never put a token in a URL or command argument.
