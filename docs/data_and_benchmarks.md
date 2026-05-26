# Data and Benchmark Retention

This repository should stay usable as product engineering code. Keep source,
tests, reproducible configuration, and concise documentation. Do not keep raw
experiment dumps in Git.

## Keep in Git

Keep files that are small, reproducible, and useful for future engineering
decisions.

| Path | Why it stays |
| --- | --- |
| `configs/*.yaml` | parser service profiles, repair profiles, quality thresholds |
| `benchmarks/*.yaml` | benchmark manifests that describe how to reproduce a run |
| `tests/` | regression coverage and executable behavior examples |
| `docs/` | product, API, operations, and curated screenshots |
| `scripts/` | repeatable developer and benchmark entrypoints |

Benchmark manifests are valuable because they encode intent: which inputs,
engines, page ranges, and thresholds to compare. They should be kept even when
the generated benchmark output is removed.

## Keep Local Only

These paths are runtime output or local caches and should not be committed:

| Path | Contents |
| --- | --- |
| `data/jobs/` | uploaded PDFs, job manifests, parser outputs, run artifacts |
| `data/benchmarks/` | benchmark result folders |
| `reports/` | ad-hoc experiment output |
| `tmp/` | smoke-test downloads and screenshots |
| `.runtime_cache/` | Docker/model/parser caches |
| `.runtime_logs/` | local runtime logs |
| `data/run_history.jsonl` | local append-only run audit log |

These are already covered by `.gitignore`.

## When an Experiment Is Worth Keeping

Keep only the smallest artifact that helps someone reproduce or understand the
result.

Keep:

- a benchmark manifest
- a config file that changed parser behavior
- a short note describing the conclusion
- a tiny fixture PDF only when it is redistributable and necessary for a test

Do not keep:

- full parser output directories
- generated PDFs from parser internals
- screenshots from one-off smoke tests
- large customer or research PDFs
- duplicate benchmark result folders

## How to Promote an Experiment

1. Move the reusable settings into `configs/` or `benchmarks/`.
2. Add a short note under `docs/` only if the result affects product decisions.
3. Add or update a regression test if the experiment exposed a bug.
4. Leave generated output in ignored local folders.
5. Run `git status --ignored -s` if you need to confirm data is ignored.

## Current Decision

Existing local `data/` and `reports/` directories are treated as developer-local
history. They are useful for manual comparison on this machine but are not part
of the repository handoff.

The tracked benchmark YAML files remain because they are lightweight,
reproducible descriptions of experiments rather than experimental output.
