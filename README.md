# Catalog Harness

[Live demo](https://catalog-harness.onrender.com/) ·
[GitHub repository](https://github.com/Harshvardhan-Garude/catalog-harness)

Catalog Harness uses MongoDB Atlas to normalize inconsistent supplier catalogs,
compare evidence-backed product enrichment, and stage categories for different
stores. It preserves supplier records and sends uncertain or contradictory facts
for review.

The web demo displays **saved experiment results**. Opening, searching, refreshing,
or switching stores makes **no model calls**.

## Try the demo

1. Choose a saved product and inspect its original supplier fields.
2. Compare baseline and candidate categories, intended uses, evidence, and review
   reasons. Inspect the exact tested policy change and original model response.
3. Switch between Homeware Store and Gift Store, then preview category counts and
   products requiring review. Accepted placements stay in the demo database.
4. Open the database benchmark to see saved measurements and browse a bounded
   page of supplier records.

The inspector contains 36 development/validation products. Final-holdout products
and answer keys are excluded; final results are shown only as aggregates.
Store placement is a separate product feature, not part of the measured semantic
improvement. `store_category` never replaces factual category or intended uses.

## Run locally

For the existing checkout, use its prepared environment:

```bash
.venv/bin/python -m demo_app
```

Open **http://127.0.0.1:8080**.

For a fresh checkout, create a local environment and install the pinned packages:

```bash
git clone https://github.com/Harshvardhan-Garude/catalog-harness.git
cd catalog-harness
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n .env.example .env
```

Enter the existing event Atlas settings privately in `.env`, then start the demo:

```bash
.venv/bin/python -m demo_app
```

Local verification used Python 3.8.10; the Render configuration selects Python
3.11. No framework or provider SDK is required.

| Configuration | Used by |
| --- | --- |
| `MONGODB_URI`, `MONGODB_DATABASE` | Local Atlas access |
| `CATALOG_MONGODB_URI`, `CATALOG_MONGODB_DATABASE` | Hosted demo; explicitly reuse the event Atlas database |
| `OPENAI_API_KEY`, `OPENAI_MODEL=gpt-4.1-mini` | Explicit model-running tools; unnecessary for viewing saved results |
| `RAINDROP_WRITE_KEY`, `RAINDROP_PROJECT_ID` | Server-side historical telemetry upload |
| `RAINDROP_QUERY_API_KEY` | Separate read key for Raindrop verification |
| `PORT` | Web server port; defaults to 8080 locally |

Keep `.env` and `.venv/` private and ignored. Never put credentials in browser code,
commits, screenshots, or chat. Existing process variables can take priority over
local dotenv settings; the standalone OpenAI checker reads the project `.env`.
Codex editor access does not establish application model access.

The demo loads saved results from Atlas at startup. If Atlas is unavailable, it
explicitly labels a complete local saved-report fallback from ignored
`artifacts/enrichment-v1/`. Placement acceptance is disabled in fallback mode.
A fresh clone without Atlas access or those artifacts shows a useful error.
Restart the server after correcting startup configuration.

## Frozen experiment results

These are **real model outputs on hand-authored synthetic fixtures**. They are
separate from the hand-authored normalization plan used to verify the pipeline.
The measured change is in the **harness instructions and evidence-handling
behavior**, not model training.

| Split | Correct fields, baseline → candidate | Unsupported assertions | Unnecessary abstentions |
| --- | --- | --- | --- |
| Development: 24 products | 68/72 → 68/72 | 4 → 3 | 0 → 1 |
| Validation: 12 products | 33/36 → 34/36 | 4 → 0 | 1 → 2 |
| Final holdout: 24 products | 68/72 → 69/72 | 2 → 1 | 2 → 2 |

The candidate adds cross-source conflict guidance and changes review behavior
from `report_only` to `abstain_invalid_evidence`. The frozen validation rule
accepted it before the separate final holdout was evaluated once. Final evidence
issues fell from six to zero, but one unsupported assertion remains. No further
proposal or tuning followed the final result.

The gain is one correct field decision on validation and one on the small final
holdout. Development accuracy is unchanged, unnecessary abstentions increased on
validation, and the two policy changes were not tested separately. These results
do not establish production generalization or statistical significance.

| Recorded phase | Model calls | Input tokens | Output tokens |
| --- | ---: | ---: | ---: |
| Development/validation, including one proposal | 19 | 32,075 | 7,972 |
| Separate final evaluation | 12 | 20,370 | 4,805 |
| Total semantic experiments | 31 | 52,445 | 12,777 |

Both phases used `gpt-4.1-mini-2025-04-14`, four products per inference request,
bounded output, `store=false`, and no automatic retries. The original experiment
retains its 20-call cap; final evaluation had a separate 12-call budget. The
$0.50 new-phase and $1 cumulative application spending ceilings remain enforced.
Recorded cumulative application usage, including earlier checks and the original
normalization baseline, estimates **$0.0424452** at the rates recorded by the
spending ledger. This is an estimate, not an account balance or billing claim.

**Do not rerun the completed experiment or final evaluation, regenerate their
fixtures or candidate policy, or reset call/delivery ledgers.** Read existing
local reports without inference:

```bash
.venv/bin/python -m enrichment.experiment report
.venv/bin/python -m enrichment.final_evaluation report
.venv/bin/python -m catalog_harness.benchmark report
```

A fresh clone may not have these ignored local artifacts. Committed sanitized
results and provenance are linked in the [evidence index](docs/README.md).
Foundation `730331f`, milestone `82021e6`, and frozen experiment `c3ee55a` remain
in the published Git history.

## Measured 50k database benchmark

The benchmark uses three separate fixed-seed datasets of 1,000, 10,000, and
50,000 records: **61,000 total records in isolated benchmark collections**.
It performs deterministic normalization and quarantine with **zero model calls**.
It is not 50,000 AI evaluations or a production load test.

| Records | Processing time | Records/s | Normalized / quarantined |
| ---: | ---: | ---: | ---: |
| 1,000 | 5.75 s | 173.8 | 737 / 263 |
| 10,000 | 69.47 s | 143.9 | 7,445 / 2,555 |
| 50,000 | 206.38 s | 242.3 | 37,418 / 12,582 |

For the 50k dataset, the indexed query measured **44.42 ms median / 103.91 ms p95**,
compared with **465.90 ms / 2208.39 ms** for a forced collection scan. Both returned
the same 25 records; documents examined were **25 versus 61,000**, because the
scan traversed the collection shared by all three benchmark datasets.

Measurements include laptop-to-Atlas network time, with two warm-ups and 20 timed
samples per query plan. Repeat seeding inserted zero records; raw fingerprints
and every deterministic output reconciled. Existing catalog and frozen reports
remained unchanged. Network variation, caches, and shared-tier scheduling limit
these measurements. See [full results](docs/benchmark-results.json) and
[integrity checks](docs/benchmark-integrity.json).

The browser applies supplier/status filters and pagination on the server, with
at most 25 records per page. Opening the page never executes the benchmark.

## Raindrop monitoring

The configured Raindrop project contains **31 historical model interactions**,
six linked run evaluations, and **22 additional flags across five instrumented
signal types**. Exact saved inputs/outputs and scalar provenance were verified;
all 28 evaluation/flag links were read back on their six run anchors.

For the recording, select **Today** or **Last 7 days**, then:

- **Events:** filter user `catalog-harness-demo` and event
  `semantic_enrichment_import`.
- **Signals → Instrumented:** inspect unsupported assertions, unnecessary
  abstentions, evidence issues, incorrect fields, and clean mechanical evidence.

Flags describe whole evaluation runs, not individual failing model calls. A
clean evidence check does not prove semantic correctness. Uploads are explicitly
historical and made no new model calls or event retransmissions.

The following operator commands require this workstation's existing ignored
call artifacts, import outbox, and delivery receipts; a fresh clone alone does
not contain them.

```bash
# Fresh read-only check: eight bounded GETs, no telemetry uploads or inference.
.venv/bin/python -m enrichment.raindrop_visibility check --receipt
# Inspect delivery locally without sending again.
.venv/bin/python -m enrichment.raindrop_monitoring status
```

The fresh check distinguishes six current anchor checks from the prior complete
31-event proof. Nested usage and aggregate signal properties are not independently
read back; token totals come from completed local ledgers. Native Raindrop
Experiments, usage traces/Costs, and custom dashboards were not created by this
import. Dashboard authoring needs a signed-in browser or authorized MCP connection.
See the [Raindrop recording guide](docs/raindrop-demo.md) for exact signal names,
counts, coverage limits, and a short narration.

## Deployment and verification

The Python app is hosted on [Render](https://catalog-harness.onrender.com/).
`render.yaml` installs `requirements.txt` and runs `python -m demo_app` on
`0.0.0.0:$PORT`. Hosted startup requires the explicit `CATALOG_MONGODB_*` variables;
it ignores an automatically provisioned `MONGODB_URI` and preserves the event
Atlas database. The saved-results server needs no OpenAI key.

Public Atlas data loading, product inspection, store previews, benchmark report,
and bounded catalog pagination passed desktop/mobile browser checks. Verification
records describe their checked commits and timestamps rather than guaranteeing
current external availability. `/healthz` reports process/data status; `/api/state`
returns saved state or sanitized startup diagnostics. Neither retries model calls.

**201 local tests passed.** Run the offline regression suite with:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

For a bounded read-only Atlas diagnostic, use
`.venv/bin/python -m enrichment.atlas_diagnostics`. The separate
`check_mongodb.py` inserts, reads, and deletes only its uniquely identified setup
check document. `check_openai.py` makes a real, billable model request; it is not
needed to view or record this demo.

This is a hackathon demo. The public app has no authentication; accepted placements
are confined to the demo collection. Durable preview sessions across instances,
production hardening, and broader supplier/locale coverage remain future work.
Modelence's documented runtime did not support the existing
Python app, so Render was chosen without a framework rewrite. Atlas Search and
additional optimization are deferred.

## More detail

- [Architecture and data boundaries](docs/architecture.md)
- [Results, policy, and verification evidence](docs/README.md)
- [Raindrop recording guide](docs/raindrop-demo.md)

The repository excludes credentials, generated datasets, full request/response
artifacts, and the local virtual environment. Commit changes using the existing
Git identity and publish with `git push origin main`; never put tokens in a URL.
