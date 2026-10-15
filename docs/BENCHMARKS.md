# Column lineage performance

[Português](pt-BR/BENCHMARKS.md)

Measured on 2026-10-11, Intel i7-6820HQ (8 threads), 31 GB, Python 3.13, SQLGlot 30, **synthetic** project (`tests/synth_project.py`):
each model `JOIN`s two sources with CTEs, `SUM`, `COALESCE` and 10 output columns.

| Models | Columns | Cold | Cached (unchanged) | Peak RSS |
|---|---:|---:|---:|---:|
| 300 | 3,000 | 7.0 s (2.35 ms/column) | 0.02 s | — |
| 1,000 | 10,000 | 24.2 s (2.42 ms/column) | 0.09 s | 58 MB |

Linear scale. Reference before the optimization: 9.4 ms/column (28 s for 3,000 columns).

What changed: (1) the schema is a `MappingSchema` built once — passing the dict made SQLGlot renormalize everything for every column (≈78% of the
time); (2) `qualify` and the scope once per model; (3) a per-model cache, keyed by the hash of the compiled SQL, the dialect and the parents'
schema, kept in the store (only changed models are recomputed).

**Limits:** synthetic, regular SQL; real projects with wide `SELECT *`, hundreds of columns per model and heavy macros may cost more per column.
Measure on a real project before claiming capacity for it.

**Update (second round):** the remaining dominant cost was the `deepcopy` of the tree for every column inside `sqlglot.lineage()`;
`trim_selects=False, copy=False` made lineage ~19× faster with identical edges. Parallelism (spawn, 4 processes) and a synthetic scale of 5,000
models: see [BENCH_REAL_PROJECT](BENCH_REAL_PROJECT.md), sections "Performance" and "Scale".

## Column lineage measured on a real dbt project (dynamic oracle)

**Project:** `dbt-labs/jaffle-shop` (13 own models, 7 seeds, 150 thousand rows) with `dbt-labs/dbt-project-evaluator` installed as a package
(≈ 48 third-party models with macro-heavy SQL) running on DuckDB; clean `dbt build` (`PASS=119 ERROR=0`). Total: 61 models, 6 sources, 23 metrics,
6 semantic models. IntentFlow resolved **100%** of the columns without errors.

**How it is measured without a hand-made answer key** (`intentflow lineage-verify`): for each source column the oracle (1) nulls the values,
(2) shifts them (+constant, +1 day, suffix), (3) reverses the order (negative, mirrored date, `reverse`), rebuilds every project model from the
compiled SQL inside a DuckDB transaction (`ROLLBACK` between perturbations) and sees **which output columns changed** (value paired with the
model's unique key, to see `ROW_NUMBER`/`RANK`). It compares with the closure of the static lineage.

| Measure (jaffle project models, 30 source columns perturbed) | Result |
|---|---|
| **Direct** lineage, column by column: hits / suspected false positives | **88 / 0** (precision ≥ 100%) |
| Changes missed by direct lineage and **not** explained by indirect dependency | **0** |
| Columns that changed only through JOIN/filter/grouping/ordering (indirect dependency) | 117 |
| With indirect dependency, per model: hits / suspected FP / missed | **74 / 5 / 0** (precision ≥ 93.7%, recall 100%) |
| Time (13 models, 56 columns × up to 3 perturbations) | ≈ 3 min |

**What the oracle found (and was fixed):**
1. **Without indirect lineage** value recall was only **62.6%**: 52 changes came from JOIN keys and filters. Now there is *indirect* dependency
   (JOIN, filter, grouping, ordering, window, CASE) at model level, used in the impact and in `SCH001`/`SCH002` — removing a join key that is never
   projected becomes **critical**.
2. **Seed and source for the same table:** edges linked to the wrong node. Now they prefer the parent declared in `depends_on`.
3. **The oracle itself had biases**, fixed and tested (including that it *detects* a sabotaged lineage).

**Limits of this measure** (it is not an answer key):
- "Suspected false positive" is an **upper bound**: it can be a real dependency the perturbations do not reveal. Precision ≥ X% is a floor.
- It only holds for DuckDB and small real data; `incremental`/`snapshot` models are rebuilt as full tables.
- A single project of 13 business models. For **your** project: `intentflow lineage-verify --target ... --duckdb ...`.

## 2026-10-13 review: queries and SQL analysis

Local microbenchmarks with synthetic data, Python 3.13.5 and SQLGlot 30.22.0:

| Operation | Before | After |
|---|---:|---:|
| Listing 2,000 findings: SQLite queries | 2,001 | 1 |
| Listing 2,000 findings: time, one run | 0.09185 s | 0.02011 s |
| 11 SQL checks × 300 models: parser calls per run | 3,300 | 300 |
| SQL checks: median of 3 runs | 1.26363 s | 0.20841 s |

Reproduce with `.venv/bin/python scripts/benchmark_review.py` (synthetic data and in-memory SQLite only).
Report and limits (Portuguese): [REVIEW_2026-10-13](pt-BR/REVIEW_2026-10-13.md).
