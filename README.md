# Catalog Harness

Catalog Harness normalizes inconsistent supplier product catalogs and quarantines
records whose meaning cannot be established safely. The intended agent loop is:
inspect → propose bounded transformations → execute on copies → evaluate → propose
a harness-policy change → retain or reject after independent evaluation.

Harness policies control agent instructions and review behavior, never the
evaluator, safety constraints, or model weights. A separate semantic enrichment
experiment follows deterministic normalization and tests at most one revision.
Raindrop integration remains future work.

## Verification status — October 11, 2026

- The original **37 local regression tests** passed before this milestone.
  Current semantic tests and live experiment results are recorded below.
- Atlas passed the single connection check from the managed Codex runtime: ping,
  unique setup-document insert/read/delete. The 300-record **hand-authored fixture**
  then passed against Atlas: 213/213 recoverable records normalized and 87/87
  ambiguous records quarantined, with zero incorrect, unresolved, missing,
  duplicate, or unexpected outputs. Results were re-read and evaluated from a
  separate process, and the current raw digest matched the stored run snapshot.
  Repeated seeding kept 300 records and the same raw SHA-256 digest. Fixture run:
  `dd1b9a927edd410ca6e7b79fe68a687d`.
- A prior independent OpenAI check returned HTTP 429. Subsequently the user
  reported a successful real v1 AI baseline, run
  `0de1ab139fbb4629a0a3bf83cff6c201`, model `gpt-4.1-mini-2025-04-14`:
  213/213 correctly normalized, 87/87 correctly quarantined, zero errors, raw
  records unchanged. Its local artifact records one request and 2,005 tokens
  (1,846 input / 159 output). This is a baseline, not evidence of improvement;
  this milestone does not rerun that historical baseline.
- Final holdout data has not been used in live runs or policy selection.
- Foundation `730331f` and milestone `82021e6` are preserved. The user supplied
  successful terminal push output for both commits to
  [Harshvardhan-Garude/catalog-harness](https://github.com/Harshvardhan-Garude/catalog-harness).
  New local commits require a subsequent push.

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

## V1 model adapter

The deterministic tools, synthetic generation, database operations, policy/plan
interfaces, independent evaluator, local event recording, and OpenAI adapter are implemented.
The hand-authored normalization fixture demonstrates plumbing. The user-reported
AI baseline is a distinct run. There is no provider SDK, Raindrop integration,
UI, vector search, deployment, or new agent framework. Codex editor access does not establish
application model access. This synthetic schema covers weights and product names;
it does not yet model arbitrary supplier formats or multilingual numeric locales.
Generation and evaluation keep labels/identities in memory; transformation and
database I/O stream records. This is suitable for hundreds of records first.

For manual v1 verification, set `OPENAI_API_KEY` and
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

## Semantic enrichment experiment (`semantic-v1`)

The separate `enrichment/` and `enrichment_eval/` packages leave the v1 normalizer,
schema, evaluator, and acceptance rules unchanged. A hand-authored mapping first
normalizes each semantic fixture's identity/name/weight; semantic inference then
receives those normalized fields plus the original name, description, and supplier
notes. Normalization fixture results and actual AI enrichment results have
different run kinds.

The taxonomy covers drinking cups, measuring cups, desk organizers, cup holders,
decorative vessels, lighting, gardening kits, cable accessories, storage containers,
and unknown. Outputs contain category, primary use, secondary uses, exact source
excerpts for each assertion, and review reasons. The strong baseline distinguishes
vessels/accessories, abstains on sparse/conflicting evidence, and forbids invented
repurposing or food/heat/medical suitability. Literal evidence validation checks
allowlisted labels and excerpt provenance; it **does not establish semantic truth**.
Semantic correctness is checked independently against authored fixture keys.

The synthetic fixtures contain **24 development, 12 validation, and 24 final**
records. Different products cover clear, sparse, conflicting, and multifunctional
listings. Multiple accepted categories/primary uses are permitted where grounded;
required secondary uses must be present and optional acceptable ones may be absent.
The evaluator always scores three field decisions per expected listing, including
missing/duplicate predictions. It also reports unsupported assertions (union of
semantic and citation failures), evidence issues, missing/duplicate/unexpected
outputs, and necessary/unnecessary abstentions. Empty secondary lists count as
necessary abstentions only when no secondary use is expected; omitting required
secondary uses counts as unnecessary abstention. Bounded local failure examples
exclude expected labels.

Before live inference, `prepare` freezes all split files and private keys, taxonomy,
baseline instructions, evidence/scoring sources, versions, resource caps, and
comparison objective in `artifacts/enrichment-v1/freeze.json`. Subsequent changes
fail verification. The model sees only allowlisted public fields/taxonomy; the
proposal sees at most six development inputs, predictions, and answer-free failure
codes. Neither receives keys, evaluator code, validation diagnoses, final records,
credentials, filesystem tools, or Atlas tools. File separation is not a same-user
OS security boundary. Final files are hashed for freezing but never inferred,
seeded, scored, or used for policy selection by this experiment.

The one proposed revision must change actual instructions and/or execute
`abstain_invalid_evidence` review behavior. The latter replaces affected fields
with unknown/empty plus review and rechecks citations; original outputs remain
available for audit. A candidate is accepted **only on validation**: strictly more
correct field decisions, no increase in unsupported assertions, and zero missing,
duplicate, or unexpected outputs, with identical evaluation identities/keys and
denominators. Development diagnoses the revision. A perfect development baseline
skips proposal/candidate inference. A rejected candidate remains a recorded result;
there is no second revision or automatic improvement loop.

Both variants use the same `gpt-4.1-mini` model and returned model snapshot,
taxonomy, evidence, batches of four, 16 KiB input cap, 2,048 output-token cap,
30-second HTTP timeout, `store=false`, and no automatic retries. The proposal has
a 1,024 output-token cap. One persistent, locked ledger reserves every call before
transmission and limits the entire milestone to **20 calls**, including failures
and the proposal. Planned maximum is 19 (9 baseline + 1 proposal + 9 candidate);
a perfect development baseline uses 9. A prior ledger refuses automatic restart;
do not delete/reset it to evade the cap. Authentication, quota, connectivity, model
changes, invalid responses, or verification failures stop further model calls.

Unlike v1's single schema-planning request, this performs **one inference per
listing per variant**, grouped four per HTTP request. Run reports record product
inferences, actual input/output/total tokens, and average tokens per listing;
proposal overhead is separate. Costs are token counts, not a dollar estimate.
Use the ledger/run totals for accounting: per-result `usage_batch` repeats the
whole batch's usage and must not be summed as per-listing usage.

Atlas uses separate `catalog_harness_enrichment_results`, `_runs`, `_policies`,
and `_experiments` collections. Run/policy IDs link outputs to raw dataset/source
IDs and deterministic normalization runs. Output ordinals retain duplicate
predictions for scoring. Each run re-reads outputs, evidence diagnostics, and its
stored evaluation; checks raw digests; and checks repeatable seeding. Existing v1
development raw digests are also checked. The source adapter marks both semantic
splits as development for v1's unchanged manifest schema and preserves
`semantic_split`; only the semantic validation role selects a revision.

From the project root:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m enrichment.experiment prepare
# One live experiment only; requires the ignored .env Atlas/OpenAI settings.
.venv/bin/python -m enrichment.experiment run
# Safe to repeat: reads the existing report, makes no model requests.
.venv/bin/python -m enrichment.experiment report
```

Generated public/private fixtures remain under ignored `data/enrichment-v1/`;
full safe request/response payloads, budget ledger, per-batch predictions, policies,
and evaluation reports remain under ignored `artifacts/enrichment-v1/`. Findings
apply to this small synthetic fixture, not production catalog accuracy.

### Newly verified semantic milestone status — October 11, 2026

**101 tests pass**, including the original 37 and 64 semantic/diagnostic tests.
Mocked orchestration verifies the nine-call perfect-baseline path, 19-call
single-revision path, failure stopping, and detection of persisted evidence
tampering. Mocked outputs are tests, not AI results or improvement evidence.

The pre-live freeze is checked into `fixtures/semantic-v1-freeze.json` with digest
`5474b4421fcf6f0ba083069329292c50d486b69fe24034d30642172719a22dd2`.
An initial managed-runtime attempt, experiment
`1ea3a967416d4cc1b447013bf573fbee`, failed Atlas preflight with sanitized status
`atlas/operation_failed` before model transmission. The user reported the same
failure from the normal terminal, experiment `d6b87def758b499e9998a17f29b97514`.
Both made zero model calls. A subsequent **single read-only diagnostic ping passed**,
using Python 3.8.10 / PyMongo 4.10.1 and the project `.env`, without environment
overrides. The original generic errors did not retain the underlying cause; no
specific network/TLS/credential cause is established. No Atlas settings or TLS
verification were changed. For future diagnosis, use
`.venv/bin/python -m enrichment.atlas_diagnostics`; it performs one bounded ping,
prints configuration names/presence and static error categories only, and makes
no database writes or model requests. Experiment failures now record the active
stage and a sanitized specific code when available.

After that successful ping, the bounded experiment completed:
`a33a4aedd2ba4ace89cb8a6ec023d178`, model `gpt-4.1-mini-2025-04-14`.
These are **real AI enrichment results on the frozen synthetic fixture**, separate
from the hand-authored normalization fixture and user-reported historical v1 run.
The scores below describe outputs after the configured harness review behavior.

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| Development correct field decisions | 68/72 | 68/72 |
| Development unsupported assertions | 4 | 3 |
| Development necessary / unnecessary abstentions | 23 / 0 | 26 / 1 |
| Validation correct field decisions | 33/36 (91.67%) | 34/36 (94.44%) |
| Validation unsupported assertions | 4 | 0 |
| Validation evidence issues | 3 | 0 |
| Validation necessary / unnecessary abstentions | 13 / 1 | 15 / 2 |
| Missing / duplicate / unexpected outputs, both splits | 0 / 0 / 0 | 0 / 0 / 0 |

The candidate was **accepted by the frozen validation rule**: one more correct
field decision, fewer unsupported assertions, and complete unique outputs. This
is one field decision, not one additional fully correct product. Development
accuracy did not improve and validation had one extra unnecessary abstention.
This single synthetic comparison does not establish production generalization;
the final 24 records remain unused.

The exact candidate is in `docs/semantic-v1-policy.json`. It adds an instruction
requiring cross-source conflict checks and abstention despite a stronger-looking
source, and changes `review_behavior` from `report_only` to
`abstain_invalid_evidence`. Both changes were executed during candidate inference.
The proposal used only two development cases/predictions and failure codes, before
validation inference; no expected labels or validation/final examples were sent.
The combined changes' effects have not been isolated in separate experiments.

An actual input and raw/post-harness output appear in
`docs/semantic-v1-example.json`. The source description calls the item a drinking
cup while supplier notes explicitly call it a separate-cup holder. Baseline
asserted drinking-cup labels; candidate returned unknown for both fields with
conflict review. The candidate also emitted citations for unasserted category
labels; the active review behavior removed those citations. Literal citation
matching alone would not have exposed the baseline's semantic conflict.

| Actual accounting | Calls | Input tokens | Output tokens | Total tokens |
| --- | ---: | ---: | ---: | ---: |
| Baseline, 36 listing inferences | 9 | 14,358 | 3,817 | 18,175 |
| One policy proposal | 1 | 1,649 | 204 | 1,853 |
| Candidate, 36 listing inferences | 9 | 16,068 | 3,951 | 20,019 |
| **Whole experiment** | **19/20** | **32,075** | **7,972** | **40,047** |

All calls completed with known usage and one model snapshot. Average inference
cost was 504.86 tokens/listing for baseline and 556.08 for candidate (about 10.1%
more), excluding proposal overhead. Resource caps remained identical.

Atlas persisted all 72 enrichment outputs across four runs. Each run re-read its
outputs/evaluation and verified raw digests; a **separate process** then re-read
all four runs and the experiment, reproduced all scores, and verified both
semantic raw datasets and the existing 300-record v1 development digest. Repeated
semantic seeding retained exactly 24 and 12 source records with identical digests.
Full metrics, run IDs, preserved blocked attempts, and independent verification
are in `docs/semantic-v1-results.json`; raw model responses remain in ignored
artifacts for audit.

The call ledger now exists. **Do not rerun/reset the live experiment.** For a demo,
inspect the existing report without network or model calls:

```bash
.venv/bin/python -m enrichment.experiment report
```

The remaining submission work is to inspect the saved example/validation result,
push the follow-up commit, and prepare the hackathon explanation/demo. No final
holdout evaluation or further optimization is part of this milestone.

## Git and publishing

Local history uses the existing author identity and real commit timestamps on
`main`. Secrets, virtual environments, caches, generated data, and local artifacts
are ignored. No global Git configuration is changed.

The existing `origin` is
`https://github.com/Harshvardhan-Garude/catalog-harness.git`. Publish subsequent
commits with `git push origin main` using existing local authentication.
Never put a token in a URL or command argument.
