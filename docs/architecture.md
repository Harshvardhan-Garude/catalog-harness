# Catalog Harness architecture

Catalog Harness separates supplier data, normalization, semantic predictions,
evaluation, and placement. The public application inspects saved results.

## Data flow and ownership

1. Supplier payloads are stored with stable source identities.
2. A validated transformation plan normalizes copies or quarantines ambiguity.
3. A separate semantic provider predicts category and uses with literal citations.
4. Independent evaluation scores the resulting harness outputs against hidden keys.
5. Store profiles map supported facts to display categories without changing facts.

Every output retains its source identity and run or dataset links. Raw payloads
remain evidence, rather than becoming a mutable working buffer. Persistence
checks compare saved results after readback and fingerprint raw records before
and after processing. Repeat seeding uses insert-only upserts to avoid duplicates.

## Deterministic normalization

`catalog_harness/transforms.py` interprets a small JSON plan. It never executes
model-provided Python, shell commands, expressions, or arbitrary imports.
Validation permits supplier field mappings, two named string cleanup operations,
decimal-dot numeric parsing, supported unit conversion, and quarantine behavior.

Weights use explicit record units or suffixes, with documented supplier defaults
only when explicit evidence is absent. Conflicting units, ambiguous number
formats, nonpositive values, unsupported suppliers, and missing evidence produce
quarantine reasons. Arithmetic uses a controlled decimal context. Each record
returns a new normalized or quarantined result; input payloads are not rewritten.

The hand-authored normalization fixture is labelled separately from real AI results.

## Semantic enrichment and evidence

The semantic-v1 provider is separate from the normalizer. Its public input is a
whitelist: source identity, supplier name/description/notes, and normalized
product identity/name/weight. Fixture answer keys and evaluator context are not
part of the serialized model request. Taxonomy labels are fixed and allowlisted.

Each output contains category, primary use, secondary uses, evidence, and review
reasons. The provider uses strict structured JSON, at most four products per
request, bounded input and output, and the shared durable request budget.
The recorded model snapshot is `gpt-4.1-mini-2025-04-14`.

`enrichment/evidence.py` checks taxonomy membership, output identity, exact
nonempty substring excerpts in the stated supplier field, and citation coverage
for asserted labels. Unknown category/primary decisions require review; secondary
uses cannot contain unknown, duplicates, or the primary use.

These checks establish literal evidence form and membership, not semantic truth.
A real excerpt can still be interpreted incorrectly. Semantic correctness is
evaluated independently against evaluator-only answers, including permitted
ambiguity and required or optional secondary uses.

The tested baseline policy uses `report_only`: citation problems are recorded
without changing its output. The exact tested candidate uses
`abstain_invalid_evidence`: an affected category/primary field becomes unknown,
an affected secondary field becomes an empty list, and a review reason is added.
Evidence for that field is removed while unaffected decisions are preserved.
The policy's version string alone does not select behavior.
Storage retains original model rows, original evidence checks, post-harness
outputs, and final evidence checks. Evaluation measures the outputs actually
returned by the harness, so the observed change includes evidence handling.

## Frozen experiment boundary

The intended loop permits one development-informed policy proposal and then an
independent comparison. The proposer sees bounded development inputs, outputs,
and sanitized failure summaries; it cannot change taxonomy, scoring, tools, or
budget. Validation answers and final records are excluded from that proposal.

The completed experiment and its exact policy snapshots are frozen. The final
holdout was evaluated once for each frozen policy, without another proposal or
tuning afterward. Saved reports, model responses, request hashes, and provenance
support inspection without rerunning inference. Small synthetic results do not
establish production generalization or statistical significance.

Request ledgers reserve capacity before transmission. Completed saved responses
support recovery of persistence; uncertain or failed requests stop further calls.
There are no automatic model retries. Final limits preserve the original ledger.

## Store placement

Two editable JSON profiles define allowed display paths and explicit factual
category/primary-use mappings. Deterministic previews require supported,
nonunknown facts and valid citations; uncertain or unmapped records need review.

`store_category` is a separate value. Switching Homeware and Gift profiles does
not alter raw data or semantic predictions. Acceptance checks preview freshness,
policy identity, and profile hashes, then writes only ready selections to
`catalog_harness_demo_placements` and verifies readback. Local fallback mode
cannot accept placements. There is no external shop integration, and placement
is not part of the measured semantic-v1 improvement.

## Isolated database benchmark

The fixed-seed 1,000, 10,000, and 50,000 record datasets use dedicated raw,
output, and report collections under `catalog_harness_benchmark_*`.
Generation, cursor processing, and bulk writes are bounded. Existing validated
normalization and quarantine rules process these synthetic records without AI.

Initial insertion, repeat seeding, and processing are measured separately.
Raw fingerprints and exact deterministic outputs reconcile after processing.
Completed measurements are preserved locally and in Atlas; restoring an existing
report does not constitute a fresh integrity check or replace initial timings.

The output compound index is dataset, supplier, status, and source-record ID.
Indexed and forced `$natural` queries use identical filters, sorting, and a
25-record limit; returned page fingerprints must match. Two warm-ups precede
20 measured requests per plan. Median and nearest-rank p95 include network time;
separate `executionStats` record documents and keys examined. Supplier/status
counts come from aggregation. The datasets share the isolated output collection,
so the final forced scan examines 61,000 documents, not only its 50,000 dataset.

## Saved-results web application

The Python standard-library HTTP server caches a verified saved Atlas snapshot.
If unavailable, complete local saved artifacts provide an explicitly labelled
fallback; missing both produces a useful error. Final products and answer keys
are excluded from the product inspector; final results are aggregate only.
The optional benchmark browser fetches filtered pages of at most 25 records.

Render binds the server to its supplied port. Hosted startup requires explicit
`CATALOG_MONGODB_URI` and `CATALOG_MONGODB_DATABASE`; local setup uses ignored
`.env` configuration. The web server needs no OpenAI key. Health/startup errors
expose sanitized categories, codes, or missing variable names, never credentials.
Refresh, search, filtering, and profile previews do not call a model or send telemetry.

## Raindrop visibility and safety limits

The 31 imported model interactions are historical saved calls. Six original
evaluation signals and 22 deterministic monitoring flags link to six run anchors.
Flags describe whole variant/split aggregates, not individual failing calls.
Fresh visibility checks distinguish the current event count and six anchor checks
from the earlier exact readback proof covering all 31 event identities.

Signal names/links are verified; aggregate properties are HTTP-acknowledged,
not readback-verified. Usage remains supported by local model-call ledgers.
Raindrop Costs is not verified: event properties do not populate that feature,
and historical usage is not backfilled. No latency or cost traces are fabricated.
Telemetry outboxes are immutable, reserve delivery durably, and fence uncertain
completion rather than resend. Query checks are bounded GETs with no retries.

Allowlisting and withheld model context are application boundaries, not OS
isolation: processes under the same OS user can read that user's accessible
files. The demo lacks production identity/authorization and multi-instance
preview storage. Secrets stay server-side, `.env` and artifacts stay ignored,
TLS remains verified, and public static routes serve only explicit assets.
