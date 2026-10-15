# Test on a large real project (ra_data_warehouse)

[Português](pt-BR/BENCH_REAL_PROJECT.md)

Project: `rittmananalytics/ra_data_warehouse` (GitHub; multi-source dbt for BigQuery/Snowflake/Redshift, 284 SQL files, embedded Fivetran
packages). Compiled with dbt 1.12 + DuckDB, without a real warehouse (no `catalog.json`, no data).

## What it took for the project to compile at all (not IntentFlow)
Old dbt (0.17): `source-paths`/`data-paths`, removed `dbt_utils.surrogate_key` and `type_*`, `target.type` without DuckDB, a duplicated YAML test,
a `ref()` to a missing model, missing per-source variables. Result: 101 enabled models, 142 sources, 1 model removed (broken ref).

## IntentFlow bugs found and fixed
| # | Symptom | Fix |
|---|---|---|
| 1 | Lineage only computed **declared** columns (163 of ~1,200): impact reached 31 of 655 relations | output columns inferred from the compiled SELECT (`named_selects`) |
| 2 | A parent without column edges (`select *` from a source without a schema, generated SQL) vanished from the graph → underestimated impact | `opaque` model dependency; `indirect_of` considers `*` |
| 3 | Check SQL012 crashed (`ParseError` re-reading the optimizer SQL) | error isolated per model |
| 4 | SQL001: 98 warnings, half on pass-through `select * from source` without a known schema | ignored when there is nothing to expand (67 left) |

## Numbers (after the fixes)
- Full `check`: ~23 s, 306 findings (DBT003 101, SQL001 67, DBT001 62, DBT005 53); lineage 1,228 columns, 90 % resolved.
- Impact per model = dbt (655/655, nothing underestimated).
- Impact per **column**: on average 0.7 models hit against 11.5 descendants in dbt; 187 of 411 columns hit nothing. **Not validated by the
  oracle** (no data in this project): treat it as a lower bound of precision.

## Honest limits
- Without `catalog.json` precision depends on the SQL declaring columns; `select *` from an opaque source falls back to model level.
- BigQuery SQL was read with the DuckDB adapter dialect; 27 columns with `SqlglotError`.
- Findings were not classified one by one by hand (a sample of 12 looked coherent; SQL001/DBT001 are noisy).

## Second project: dbt-project-evaluator (runs on DuckDB)
`ra_data_warehouse` does not run (invalid SQL in the repository), so it cannot feed the oracle. `dbt-project-evaluator` (dbt-labs, integration)
runs on DuckDB: 54 models + 28 seeds, 37 verifiable models, 196 source columns perturbed (null / shift / reverse), ~16 min.

### Bugs found by the oracle (fixed)
| # | Symptom | Cause |
|---|---|---|
| 5 | 34 real changes "missed" (`stg_nodes.is_enabled` → `fct_*_coverage`) | in sqlglot 30 `Scope.union_scopes` no longer exists (now `set_operation_scopes`); `UNION ALL` provenance was silently empty |
| 6 | the oracle recreated empty root models (`graph` outside dbt) and wiped the data | existing roots are kept as sources |
| 7 | `UPDATE` on a view failed silently and became a "false positive" | roots become tables; a perturbation that does not apply is reported |
| 8 | `now()` in `measured_at` became "missed" | two builds without perturbation: unstable columns are excluded |
| 9 | a model whose row count changes (filter/join) did not count as affected | it now counts at model level |
| 10 | 100 % NULL columns (perturbation is a no-op) became "false positives" | marked as not testable |

### Final result
| | |
|---|---|
| Recall at model level (including indirect dependency) | **100 %** (0 missed out of 554 affected models) |
| Recall of direct lineage, column by column | 37.5 % of what changed; the rest (722) only through join/filter/grouping |
| Precision (lower bound) | 43 % per model, 38 % per column |

Precision is a **lower bound**: "suspected" is a dependency the lineage points to but the perturbation did not show. With 13× more data
(≈2.4 thousand rows) precision barely moved (44–46 % per model, 37–39 % per column), so the suspects **are not a lack of data**: the dependency is
structural (a filter over all UNION rows), not observable with this data. **Real precision is between the lower bound (≈40 %) and 100 %.**

### Faster oracle
Incremental rebuild: only models that depend on the perturbed source are rebuilt; `--source name[.column]` limits the sources. 75 columns in
11 min against >1 h 25 min for 196 columns before.

## Performance (measured)
| Step | Before | After | How |
|---|---|---|---|
| Lineage, 101 models / 1,228 columns | 34 s | **1.8 s** | `trim_selects=False, copy=False`: `sqlglot.lineage()` deep-copied the tree for every column (92 % of the time); identical edges |
| Full `check` (ra_data_warehouse) | 23 s | **7 s** | same |
| `data diff`, 3 M rows × 31 columns | ~100 queries/scans | **10 queries, 1.4 s** | count+statistics in one scan, differences in batches of 40 columns |
| Lineage oracle | rebuilds every model | only the affected ones | `--source` limits the sources |

### Scale (second round)
| Item | What it does | Known limit |
|---|---|---|
| Parallel lineage | models out of the cache go to N processes (`spawn`, default 4; `INTENTFLOW_WORKERS`). ra_data_warehouse: 1.75 s → 0.73 s, identical edges | only in single-threaded processes (CLI/CI); the web server stays serial on the 1st run and uses the cache afterwards |
| `data diff --sample-pct P` | compares ~P % of the keys by key hash (same bucket on both relations), returns sample counts + an estimate of the total | hash per engine (10 engines); only DuckDB was really run. A sample does not prove absence of differences outside it |
| `lineage-verify --sample-rows N` | shrinks each source to ~N rows (key hash, keeps joins) in the temporary copy | a column only present in rare rows may become "untestable" |
| Incremental lineage per PR | base and head share the per-model cache | the key includes the parents' schema: changing a column recomputes whoever reads that parent |

### Synthetic scale (`.bench/scale.py`)
Layered models (join, aggregation, window, UNION; ~11 columns), identical edges between serial and parallel.

| Models | Columns | Serial | 4 processes | Warm cache | 1 model changed |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 11,250 | 4.4 s | 2.3 s | 0.09 s | 0.09 s (999/1000 from the cache) |
| 5,000 | 56,250 | 26.3 s | 10.9 s | 0.6 s | 0.6 s (4,999/5,000 from the cache) |

Parallel ≈ 2.4×; 8 processes were **worse** (13.3 s) than 4, so the default is 4. Linear in the number of models (~5 ms per model). Synthetic SQL
is simpler than real SQL (ra_data_warehouse: ~18 ms/model); expect ~3× slower on a real project.
