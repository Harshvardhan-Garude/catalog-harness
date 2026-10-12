# Evidence and documentation

These JSON files are sanitized, persisted reports or timestamped verification
snapshots. Reading them does not rerun experiments, benchmarks, model requests,
or telemetry uploads. They record evidence at the stated time, rather than a
current API health check. See the [project README](../README.md) for setup and
the [public demo](https://catalog-harness.onrender.com/) for saved results.

## Semantic experiment

Human-authored synthetic products were evaluated using real model predictions.

- [Development and validation results](semantic-v1-results.json): original
  experiment, candidate selection, usage and limitations. Its unused-holdout
  statement describes that phase; the separate final report below came later.
- [Selected candidate policy](semantic-v1-policy.json): instruction addendum and
  review behavior.
- [Saved prediction example](semantic-v1-example.json): actual input, original
  model output, evidence issues and post-harness output.
- [Final evaluation](semantic-v1-final-results.json): the subsequent frozen
  holdout comparison; final results did not select or retune the policy.

## Demo and deployment

- [Implementation and persistence verification](demo-verification.json):
  reproduced aggregate scores, Atlas records, raw-data integrity, budgets and
  preserved history.
- [Public deployment verification](public-deployment.json): hosted Atlas source,
  saved product workflow, browser checks and placement verification.

## Database benchmark

Separate 1,000-, 10,000- and 50,000-record synthetic datasets measure database
operations and deterministic processing. This benchmark added no model calls.

- [Measured results](benchmark-results.json): seeding, repeat seeding,
  processing throughput, indexed/scan timings and supplier/status aggregation.
- [Integrity verification](benchmark-integrity.json): reconciliation, unchanged
  raw records, Atlas/local report agreement and preserved prior experiments.
- [Browser verification](benchmark-demo-verification.json): local/public saved
  metrics, live filters, bounded pagination and mobile layout.
- [Deployment verification](benchmark-deployment.json): completed Atlas-backed
  benchmark displayed on the public host.

## Raindrop visibility and recording

- [Original import proof](raindrop-verification.json): 31 historical event cores
  and six evaluation signal links.
- [Monitoring visibility proof](raindrop-monitoring-verification.json): 22 added
  deterministic flag links, six freshly checked anchor events, and explicit
  readback limitations. Aggregate signal properties remain unverified remotely.
- [Recording guide](raindrop-demo.md): dashboard navigation, flag meanings,
  coverage limits and a short narration. Flags describe entire run aggregates;
  an anchor event does not identify the individual failing model call.
