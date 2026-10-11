# Raindrop recording guide

The verified import contains **31 historical model interactions and six linked
run-aggregate evaluation signals**, plus **22 verified monitoring flag links
across five additional instrumented signal types**. These describe completed,
frozen synthetic experiments. Opening the demo or this guide does not run
inference or upload events. Keep the existing import; do not reset delivery
records or resend it.

## Find the saved interactions

1. Open the configured project in Raindrop, then **Events**.
2. Choose **Today** or **Last 7 days**, covering the October 11 import.
3. Filter user to `catalog-harness-demo` and event to
   `semantic_enrichment_import`.
4. Open an event to show its saved model input, original model output, and scalar
   properties such as `phase`, `variant`, `split`, and `stage`.

There are 19 development/validation interactions and 12 final interactions:
15 baseline, 15 candidate, and one development policy-proposal call. They are
grouped into seven conversation IDs. An interaction can contain a four-product
batch; 31 events does not mean 31 individual product evaluations.

## Show evaluation signals

Open **Signals → Instrumented** and select **Synthetic enrichment evaluation**.
Its six occurrences correspond to baseline/candidate development, validation,
and final runs. Open an occurrence's linked event and inspect the run/split
properties. Each signal is anchored to one representative event for its run;
its counts describe that entire run, not a failure by the anchor event or each
product in the batch.

The frozen artifact reports contain these results:

| Split | Correct field decisions, baseline → candidate | Unsupported assertions | Unnecessary abstentions |
| --- | --- | --- | --- |
| Development, 24 products | 68/72 → 68/72 | 4 → 3 | 0 → 1 |
| Validation, 12 products | 33/36 → 34/36 | 4 → 0 | 1 → 2 |
| Final, 24 products | 68/72 → 69/72 | 2 → 1 | 2 → 2 |

The validation improvement selected the candidate before the separate final
evaluation. Final results were reported once; they did not drive further tuning.
The modest gains and abstention tradeoff belong in the narration.

## Interpret monitoring coverage

- **Verified:** the original readback proved all 31 event cores, exact saved
  inputs/outputs and scalar provenance. The latest fresh check reverified six
  anchor event cores, the current total of 31 events, all six instrumented signal
  definitions, and all 28 expected event/name links: six original evaluations
  plus 22 monitoring flags. It does not freshly reverify the other 25 event cores.
- **HTTP-acknowledged:** the six aggregate metric payloads and the separate
  22-flag payload were accepted with HTTP 204. Signal aggregate properties have
  not been independently read back.
- **Local usage:** completed call ledgers record **52,445 input + 12,777 output =
  65,222 tokens across 31 calls**. The six run signals cover 30 calls and 63,369
  tokens; they exclude the one 1,853-token policy-proposal call.
- **Metadata limitation:** Query API responses omit nested `model_usage` and
  `request_configuration` properties. Exact model input independently proves
  the request configuration. Missing usage metadata does not establish lost
  ingestion, and local usage is not proof of Raindrop Costs-page totals.

These are saved harness runs imported as Events. The application has not created
native Raindrop **Experiments** resources, sent OpenTelemetry execution spans for
**Traces**, or verified native **Costs** accounting. Those panels may therefore
be blank. There is no continuous stream of new model requests from the saved
demo. A blank panel is not a reason to rerun the frozen experiments.

Instrumented flags are deterministic labels calculated from saved aggregate
counts. They are not Raindrop-discovered Issues, a diagnosis of individual calls,
or new AI evaluations. Do not claim Issues were discovered unless the platform
actually returns them.

The following five flag types are **uploaded and their 22 event/name links are
verified**, as of October 11 at 23:30:48 UTC. These are the display names under
**Signals → Instrumented**. Occurrences come from the six saved run aggregates,
with several flags able to reference the same anchor event:

| Display name | Verified run occurrences | Meaning |
| --- | --- | --- |
| Aggregate unsupported assertions present | 5 | At least one unsupported assertion in the run |
| Aggregate unnecessary abstentions present | 5 | At least one unnecessary abstention in the run |
| Aggregate evidence issues present | 2 | At least one mechanical evidence-check issue |
| Aggregate incorrect fields present | 6 | At least one incorrect field decision in the run |
| Aggregate evidence checks clean | 4 | Zero mechanical evidence-check issues |

These 22 flag occurrences do not mean 22 failing model calls. A clean
mechanical evidence check does not establish semantic correctness or an
error-free run; positive and negative flags may coexist on one run.

Run a fresh read-only check from the project root:

```bash
.venv/bin/python -m enrichment.raindrop_visibility check --receipt
```

It makes eight bounded GETs: signal definitions, the filtered event count, and
six anchor events. It compares saved content and expected signal names, saves a
sanitized receipt in ignored artifacts, and never posts telemetry, changes
delivery records, or makes model calls. Original 31-event proofs remain separate
from the six freshly checked anchors.

For immediate visibility, use Events and Instrumented Signals with the filters
above. MCP dashboard creation/editing requires OAuth authorization with
`write:dashboards`; compatible Raindrop MCP tools are unavailable in this session
(plugin discovery returned none). A custom dashboard has not been created here.
No browser code should receive either Raindrop key.

## Thirty-second narration

“These are 31 real model interactions from our completed synthetic experiments,
imported historically into Raindrop. Six evaluation anchors now carry 22
deterministic monitoring flags, making unsupported assertions and review
tradeoffs visible. Validation improved from 33 to 34 correct field decisions out
of 36, with unsupported assertions falling from four to zero; the final holdout
gained one correct decision. Token totals come from local ledgers. These saved
results add no new model calls.”

Evidence: [verification summary](raindrop-verification.json),
[development/validation results](semantic-v1-results.json), and
[final results](semantic-v1-final-results.json). See the
[Raindrop Events guide](https://www.raindrop.ai/docs/platform/events/) for event
search and filtering.
