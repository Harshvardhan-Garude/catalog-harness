# Catalog Harness

Catalog Harness normalizes inconsistent supplier product catalogs and quarantines
records whose meaning cannot be established safely. The intended agent loop is:
inspect → propose bounded transformations → execute on copies → evaluate → propose
a harness-policy change → retain or reject after independent evaluation.

Harness policies control agent instructions and review behavior, never the
evaluator, safety constraints, or model weights. A separate semantic enrichment
experiment follows deterministic normalization and tests at most one revision.
The demo compares saved results and stages deterministic store placements.

## Open the saved-results demo

From `/home/harsh/Desktop/MongoDB_Hackathon`:

```bash
.venv/bin/python -m demo_app
```

Open **http://127.0.0.1:8080**. The server reads the completed experiment from the
existing Atlas database at startup and caches its verified snapshot. Opening,
searching, filtering, switching stores, and refreshing never make model requests.
Choose one of 36 saved development/validation products to compare supplier fields,
baseline/candidate factual category, primary/secondary uses, citations, review
reasons, and raw model responses. The exact tested instruction addendum, complete
instructions, review behavior, and aggregate metrics are visible.

If Atlas is unavailable, the interface explicitly identifies a **local saved-report
fallback** using the complete ignored `artifacts/enrichment-v1` files. Placement
acceptance is disabled in fallback mode. A fresh clone without Atlas access and
those artifacts displays a useful error; it never manufactures example AI results.
Restart to retry Atlas access. Final products and answer keys are excluded from
the inspector; final results are aggregate only. The final summary prefers its
matching Atlas report and explicitly labels a local saved-report fallback.

Edit `demo_app/profiles/homeware.json` and `gift.json` to change allowed display
paths and explicit category/use mapping rules. Preview a selection or all saved
products, inspect category counts and records needing review, then accept only
ready rows into `catalog_harness_demo_placements` in the demo Atlas database.
Unmapped, uncertain, or conflicting facts require review. `store_category` is a
separate placement value: switching stores changes neither factual category/uses
nor raw records. Acceptance checks profile/policy freshness and verifies readback.
There is no external shop integration. **Store placement is a new product feature,
not part of the already-measured semantic-v1 improvement.**

The measured change is in the **harness, including evidence-handling behavior**.
The frozen validation result remains 33/36 → 34/36 correct fields, unsupported
assertions 4 → 0, and unnecessary abstentions 1 → 2. Development correct fields
stay at 68/72. These are saved real model outputs on synthetic fixtures, separate
from hand-authored normalization fixture results; they are not freshly generated
when the interface opens. The preserved experiment at `c3ee55a` is not rerun.

## Locked final holdout — completed once

The untouched 24 final records were evaluated once for each exact frozen policy,
at four records/request, using `gpt-4.1-mini-2025-04-14`, the existing taxonomy,
scoring, and evidence processing. No answer keys were sent to the model, no new
policy was proposed, and no tuning followed the result.

| Final metric | Baseline | Candidate |
| --- | ---: | ---: |
| Correct field decisions | 68/72 (94.44%) | 69/72 (95.83%) |
| Unsupported assertions | 2 | 1 |
| Unnecessary abstentions | 2 | 2 |
| Evidence issues | 6 | 0 |
| Missing / duplicate / unexpected outputs | 0 / 0 / 0 | 0 / 0 / 0 |
| New model requests | 6 | 6 |

This is one additional correct field decision on a small synthetic holdout. One
unsupported assertion remains; semantic correctness does not follow from literal
citation checks alone. The comparison does not establish production generalization
or statistical significance. Aggregate results and provenance are committed in
`docs/semantic-v1-final-results.json`. Atlas persisted and re-read both runs,
original model rows, post-harness outputs, and evaluations; existing and final raw
snapshots remained unchanged, and seeding was repeatable.

Actual additional usage: **12 calls, 20,370 input + 4,805 output = 25,175 tokens**.
At uncached [published GPT-4.1 mini prices](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
($0.40/M input, $1.60/M output), the final phase is **$0.015836**. All 21 recorded
previous application calls add $0.0266092, giving **$0.0424452 cumulative** at those
rates. These are usage-based estimates, not an account billing/balance assertion.
The $0.50 phase and $1 cumulative application ceilings remain enforced. Unknown
historical usage is conservatively reserved; current recorded calls all have usage.

`enrichment/spending.py` reserves cost durably before transmission using the full
serialized request (including schema/instructions), a UTF-8 byte upper bound plus
8,192 framing tokens, and maximum output of 2,048 tokens. It uses uncached prices,
pins the returned model snapshot, caps this separate final phase at 12 requests,
and never resets the completed experiment's 20-call ledger. Completed responses
can repair interrupted persistence without retransmission. An uncertain or failed
request stops further calls rather than automatically retrying.

Read saved reports without requests:

```bash
.venv/bin/python -m enrichment.experiment report
.venv/bin/python -m enrichment.final_evaluation report
```

Do not regenerate fixtures or candidate policy, rerun the completed experiment,
or tune against final results. The final runner's completed path is read-only;
its reserved phase/run IDs and batch cache support interruption recovery, never
a second final experiment.

## Python hosting and remaining plan

The requested [Modelence skill](https://modelence.com/skill.md) and
[deployment instructions](https://docs.modelence.com/deploy/setup.md) explicitly
exclude Python runtimes, including Python behind a Node.js frontend. Per the
user's choice, preserve Python and prepare a compatible host. No framework rewrite
or Modelence database provisioning is performed.

`render.yaml` prepares a Python Render web service: `pip install -r requirements.txt`
then `python -m demo_app`, bound to `0.0.0.0:$PORT`. `.python-version` selects the
latest Python 3.11 patch on Render; local checks continue in existing Python 3.8.
See [Render web services](https://render.com/docs/web-services) and
[Python versions](https://render.com/docs/python-version). The public demo is live
at **https://catalog-harness.onrender.com/** and independently verified at commit
`75a026f`: page, health, and saved-state endpoints return HTTP 200. Both the 36
product inspector and final aggregate read matching saved reports from Atlas.
Browser checks pass for saved metrics, search/filter/selection, comparisons,
policy instructions, both store previews, and desktop/mobile layouts with zero
JavaScript errors. One supported public placement was accepted, read back from
the existing event demo database, and all four raw snapshots remained unchanged.
`docs/public-deployment.json` preserves the initial failed data load and the
successful subsequent verification. No deployment verification called a model.

In the hosting dashboard, set **CATALOG_MONGODB_URI** (secret) and
**CATALOG_MONGODB_DATABASE** to the existing event Atlas database. Hosted startup
requires those explicit names and ignores any automatically provisioned
`MONGODB_URI`; it cannot silently switch databases. Local startup continues using
the ignored `.env`. No OpenAI key is needed by the saved-results web server.
`/healthz` reports process health and whether saved data loaded; `/api/state`
reports a useful error if neither Atlas nor a complete fallback is available.
Startup failures are cached as credential-free typed category/code diagnostics
in `/healthz` and `/api/state`; missing variables expose names only. Fetching
those endpoints does not retry Atlas connections or make model requests.
Credentials are never served as static files. Store writes are limited to demo
placements, with same-origin checks and no external shop access.

Atlas reachability from Render is **verified**. If it becomes blocked, inspect
the host's outbound IP addresses and the event's Atlas networking policy with the
event owner. Do not weaken TLS, change Atlas settings silently, or replace the
event database. The lightweight server is intended for the hackathon demo; public
identity/authorization, multi-instance preview storage, and production hardening
remain limitations.

The first Python-host deployment and historical Raindrop event/signal links are
verified. Next: **the 50k database benchmark**.
Any imports of saved runs must be labelled historical and must not rerun inference.
Atlas Search and further optimization remain outside this milestone.

## Raindrop HTTP integration — historical events and signal links verified

The small standard-library client follows the
[Raindrop HTTP API](https://www.raindrop.ai/docs/sdk/http-api/): array POSTs to
`/v1/events/track`, followed by linked `/v1/signals/track`. The HTTP guide shows
204; successful 2xx acknowledgements are accepted, matching the
[official Python SDK](https://pypi.org/project/raindrop-ai/). An acknowledgement
does not establish readback of the stored content.
It uses server-side `RAINDROP_WRITE_KEY` and explicit `RAINDROP_PROJECT_ID` from
the ignored `.env` or hosting environment. The separate `RAINDROP_QUERY_API_KEY`
is used only for readback in that same project. On October 11, read-only requests
verified **all 31 historical interactions** against their original event IDs,
event/user/run identities, exact model inputs and outputs, returned model, and
scalar provenance. The six linked aggregate evaluation signals were then sent
once, acknowledged with **HTTP 204**, and each signal name was read back on its
correct event. No events were resent. See `docs/raindrop-verification.json`.

Readback has explicit limits: the Query API did not expose the two nested
`model_usage` and `request_configuration` properties on any event. Request
configuration was independently verified from its exact duplicate in the saved
model input. Token usage remains verified against the original **local completed
call ledgers**, not against Raindrop event metadata or its Costs dashboard. Event
detail exposes signal names/IDs, not the uploaded aggregate properties; all six
links are verified, while those properties are only HTTP-acknowledged.

The local outbox has been verified: **31 actual saved model interactions and six
linked aggregate evaluation signals**, covering the original 19 calls and final
12 calls. Every import is marked **historical**, with phase/run/stage identities,
actual returned model and locally recorded usage, original instructions/input,
and original model output. Signals are explicitly scoped to a variant aggregate rather than falsely
attributed to one product. Evaluator answer keys and individual final failures
are excluded. Exporting and uploading add **zero model calls** and do not affect
the API spending caps. The demo interface never uploads telemetry automatically.

Inspect the existing completed import without uploading again:

```bash
.venv/bin/python -m enrichment.raindrop_query verify
.venv/bin/python -m enrichment.raindrop status
```

Verification resumes the saved core-content proofs; with the complete receipt it
adds zero network GETs. Keys are entered privately using
[Query API authentication](https://auth.raindrop.ai/). The
[Query API](https://www.raindrop.ai/docs/query-api/sdk/) is independent of the
ingestion API. Missing variables are reported by name only; all errors are
sanitized categories/codes. Neither key belongs in browser code.

The original event attempt was marked uncertain by an older status checker.
Its error is retained, and only positive readback of all 31 events reconciled
delivery. Original event and signal payloads remain immutable. The signals-only
recovery command derives a separate payload with scalar metrics and canonical
JSON strings for counts/usage; it cannot send events:

```bash
.venv/bin/python -m enrichment.raindrop send-signals
```

This command is already completed and now skips sending. It requires the bound
event-verification receipt, fences the legacy sender before preparing the derived
payload, verifies all hashes, and reserves delivery durably before one POST.
Timeouts are bounded at 30 seconds with verified TLS and no automatic retries.
Uncertain completion blocks retransmission. Do not reset the delivery ledger or
export a replacement outbox to bypass those protections. Ignored artifacts retain
per-event proofs and the six signal-link receipts. Regression checks cover exact
provenance, coverage limits, scalar encoding, headers, bounds, sanitized errors,
crash recovery, and duplicate-send prevention.

The [Raindrop setup skill](https://raw.githubusercontent.com/raindrop-ai/skills/main/raindrop-setup/SKILL.md)
was applied to the existing, authorized HTTP import. No extra SDK dependency,
runtime change or new model interaction is needed. Synthetic fixtures are the
only exported inputs; the HTTP client does not provide SDK PII redaction or
automatic buffering/retries. There are no end-user accounts, live chat threads,
feedback controls or tool calls to instrument in this saved-results interface.
Optional investigation tools are the
[Raindrop MCP server](https://mcp.raindrop.ai/mcp),
`npx skills add raindrop-ai/skills --skill raindrop-investigate`, and Raindrop's
Slack alert connection. These are not prerequisites for the demo or permission
to rerun the completed evaluations.

## Current verification

The complete **171-test** suite passes in the existing Python 3.8 environment.
Headless Chrome verifies saved metrics, product search/filter/selection, original
fields and both policy outputs, exact instructions, profile switching and staged
preview, safe text rendering, and desktop/mobile layouts. A live Atlas check
accepted one ready placement per store, verified readback, and confirmed every
raw snapshot stayed unchanged. Hosted startup passed locally at `0.0.0.0:3000`
with `PORT=3000` and explicit event Atlas settings.

`docs/demo-verification.json` records 33 independent checks of the final Atlas
report/results, original outputs, normalized copies, unchanged raw snapshots,
request/cache hashes, frozen contexts, usage, and the preserved original result.
Credential scans found no secrets in publishable files; `.env` and `.venv/`
remain ignored. Public-host Atlas access and interactions are verified.
Raindrop readback verifies all 31 historical event cores and all six evaluation
signal links; nested usage and signal aggregate properties have the limits above.

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
- The final holdout was subsequently evaluated once under the locked policies;
  results are above. It was never used for policy selection or tuning.
- Foundation `730331f`, milestone `82021e6`, and frozen experiment `c3ee55a` are
  preserved in the published history at
  [Harshvardhan-Garude/catalog-harness](https://github.com/Harshvardhan-Garude/catalog-harness).
  The demo milestone `ee7b655` was pushed and independently confirmed at origin.

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
AI baseline is a distinct run. That original v1 milestone had no telemetry or UI;
the later saved-results demo and Raindrop client are documented above. There is
no provider SDK, vector search, or new agent framework. Codex editor access does not establish
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
the later final comparison is recorded above, without additional policy tuning.

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

This historical semantic-v1 result is preserved. The later demo, separate final
evaluation, and hosting work are documented above; there is no further optimization.

## Git and publishing

Local history uses the existing author identity and real commit timestamps on
`main`. Secrets, virtual environments, caches, generated data, and local artifacts
are ignored. No global Git configuration is changed.

The existing `origin` is
`https://github.com/Harshvardhan-Garude/catalog-harness.git`. Publish subsequent
commits with `git push origin main` using existing local authentication.
Never put a token in a URL or command argument.
