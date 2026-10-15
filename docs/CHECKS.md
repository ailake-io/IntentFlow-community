# Check catalog

[Português](pt-BR/CHECKS.md)

States: **tested** = there is an automated test that exercises the check with a positive case (and, where relevant, a negative one).

## dbt and schema

| Id | What it detects | Trigger | Cost | State |
|---|---|---|---|---|
| SCH001 | Removed/renamed column with downstream consumers; suggests the rename, shows the transitive impact and produces a fix | pre-merge | static | tested (real dbt) |
| SCH002 | Column type change, with consumers | pre-merge | static | tested |
| OL001 | A job outside the SQL (Spark/Python/notebook, through OpenLineage or `intentflow.lineage.json`) reads a removed/changed column, directly or derived | pre-merge | static | tested (real Spark) |
| OL002 | Dataset read by an external job matches more than one model (the impact silently disappears) or, with `external_warn_unmatched`, none; fix with `aliases` | pre-merge | static | tested |
| OL003 | External job reads a PII column (or an unmasked derivative); `pii_allowed_jobs` in the policy allows it | pre-merge | static | tested |
| OL004 | Spark job with the default application name (`pyspark_shell`): different jobs get mixed up | pre-merge | static | tested |
| DBT001 | Consumed model without a `unique` test | pre-merge | static | tested |
| DBT002 | Incremental without `unique_key` (not idempotent). Rule-based fix (Enterprise): the person enters the key | pre-merge | static | tested |
| DBT003 | Model without an owner (`meta.owner`). Rule-based fix (Enterprise): the person enters the owner | continuous | static | tested |
| DBT004 | PII propagated without hash/mask to a non-authorized model | pre-merge | static | tested |
| DBT005 | Model with ≥ 3 consumers without `contract.enforced` | pre-merge | static | tested |
| DBTR001 | dbt test failing/warning (from `run_results.json`) | continuous | statistical | tested (real dbt failure) |
| DBTR002/003 | Model with a run error; models skipped because of an upstream failure | continuous | statistical | tested |
| DBTR004 | Model > 2× its historical median time | continuous | statistical | tested |

## SQL (SQLGlot AST over the compiled SQL)

| Id | What it detects | State |
|---|---|---|
| SQL001 | `SELECT *` | tested |
| SQL002 | JOIN without a condition (cartesian product, including comma joins) | tested |
| SQL003 | `UNION` without `ALL` | tested |
| SQL004 | `ORDER BY` in a subquery/CTE without `LIMIT` | tested |
| SQL005 | Function applied to a column in a filter/join (non-sargable) | tested |
| SQL006 | `DISTINCT` combined with `JOIN` | tested |
| SQL007 | Correlated subquery | tested |
| SQL008 | `LIKE '%…'` | tested |
| SQL009 | `NOT IN (subquery)` | tested |
| SQL010 | `IN` with more than 100 literals | tested |
| SQL011 | `OR` in a JOIN condition | tested |
| SQL012 | Optimizer rewrite; **equivalence checked by running both versions** (discarded if they differ) | tested |
| SQL013 | Expensive plan through `EXPLAIN` (cartesian product, nested loop, estimated rows above the policy) | tested (DuckDB); PostgreSQL through `plan()` |
| SQL014 | Plan got worse in the PR (new heavy operator or cost > 2×) comparing base and new | tested (DuckDB) |
| SQL015 | Join key with types of different families (number × text etc.) | tested |

Every rule SQL001–SQL013 has a planted model in `examples/dbt_lab` and a test that fires it, plus a check that clean models raise no false
positive. SQL001 and SCH001 produce deterministic fixes (`expand_star`, `rename_column`) applied through Enterprise.

## Orchestration and other sources

| Id | Platform | Detects | State |
|---|---|---|---|
| AF001/AF002 | Airflow | no `retries`; no timeout | tested |
| AF003/AF004 | Airflow | `catchup` on with an old `start_date`; no `max_active_runs` | tested |
| AF005 | Airflow | no failure notification | tested |
| AF006 | Airflow | `schedule_interval`, deprecated imports | tested |
| AF007 | Airflow | `Variable.get` at module level | tested |
| AF301–AF311 | Airflow 2 → 3 | moved imports (`airflow.sdk`, `standard` provider), `schedule_interval`, `execution_date`, `provide_context`, `SubDagOperator`, `days_ago`, implicit `catchup`, metadata DB access, `Dataset`/`Table` → `Asset`, REST `/api/v1`. **Opt-in:** `intentflow policy airflow-target 3`. Command: `intentflow airflow3 FOLDER [--write]` | tested; rewritten DAGs identical to the ones run on a real Airflow 3.1 |
| KS001–KS004 | Kestra | no `errors`; I/O without retry; no timeout; scheduled without `concurrency` | tested |
| KS005 | Kestra | plain-text secret | tested |
| KS006 | Kestra | backfill from an old date | tested |
| LINT001 | SQLFluff | correctness/performance rules over the compiled SQL; optional | tested |
| CAT001 | Catalogs (Glue/Unity) | table gone, column removed/retyped between two collections | tested (Glue on emulator, Unity simulated) |
| ELM001 | Elementary | failing test or anomaly (volume, freshness, distribution), with downstream impact | tested with the documented table |
| LAKE001 | Delta/Iceberg | many small files; schema removing/changing types between versions | tested with real tables |
| DQ001 | GX / Soda | quality check failed/warned, with downstream impact and the tool's severity | tested with real outputs |
| CTR001 | Contracts | missing contract model/column, type mismatch, uniqueness/required without a test | tested |
| KAF001 | Kafka | high **and** growing consumer lag | tested with real Redpanda |
| KAF002 | Kafka | a new schema version removes/retypes a field; critical if the topic feeds models | tested with real Redpanda |

## Warehouse probes

| Id | Detects | State |
|---|---|---|
| PRB001 | Duplicates in a column with a `unique` test | tested (positive and negative; DuckDB, PostgreSQL) |
| PRB002 | Volume outside 0.5×–2× the historical median | tested |
| PRB003 | Null rate went up ≥ 20 p.p. and ≥ 2× the usual | tested (DuckDB) |
| PRB004 | Fan-out: a join on a non-unique key multiplies rows | tested (DuckDB, lab project) |

## Cluster and runs

| Id | Detects | State |
|---|---|---|
| CLU001 | Memory ≥ 70% of the limit | tested |
| CLU002 | Exhaustion forecast in < 24 h (linear regression) | tested |
| CLU003 | Process/container killed by OOM | tested |
| CLU004 | Queries spilling to disk | tested |
| CLU005 | Pool/worker saturated over time | tested |
| RUN001 | Three failures in a row | tested |
| RUN002 | Duration > 2× the median | tested |
| RUN003 | SLA at risk (p90 > 80% of the deadline) or broken (`policy sla`) | tested |
| RUN004 | No success for more than `max_age_hours` | tested |

## False positives

Each finding can be marked as a false positive (it does not reopen). `intentflow stats`/the UI show the rate per check and suggest turning a
check off when it is ≥ 50% with ≥ 5 decisions (`policy disable-check`). New checks should run without blocking until their rate is known.
