# Catalog Harness

Catalog Harness normalizes inconsistent supplier product catalogs and quarantines
records whose meaning cannot be established safely. The intended agent loop is:
inspect → propose bounded transformations → execute on copies → evaluate → propose
a harness-policy change → retain or reject after independent evaluation.

Harness policies will control sampling, context selection, and verification, never
the evaluator, safety constraints, or model weights. Model access and Raindrop
integration are reserved for the next milestone.

## Local foundation

Use the existing Python 3.8 virtual environment. Dependencies are pinned in
`requirements.txt`; no system Python or global Git settings are changed.

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python check_mongodb.py
```

Keep `MONGODB_URI` and `MONGODB_DATABASE` in the ignored local `.env`, using
`.env.example` as the placeholder template. Never commit credentials.

The initial foundation preserves the working MongoDB connection check. The first
implementation milestone adds synthetic datasets, bounded MongoDB operations,
validated deterministic transformations, and an independent evaluator.
