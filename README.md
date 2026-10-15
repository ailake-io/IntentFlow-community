# IntentFlow

[Português](README.pt-BR.md)

**One place for the problems of your data pipelines — and a way to catch them before they reach production.**

IntentFlow is **not an execution engine** and has **no YAML of its own**: settings and policies live in a local store and are
managed through the **CLI** or the **UI**. It sits next to what you already run (dbt, SQLMesh, Airflow, Kestra, Spark, OpenLineage,
warehouses) and:

- reads metadata and code (read-only by default);
- runs preventive *checks* on the pull request, before the job and continuously;
- traces column lineage, including Spark/Python/SQL jobs outside the project, and shows what breaks if a column changes;
- groups findings into incidents by root cause and routes them by owner (Slack, webhook, e-mail, PR comment);
- shows in one screen what is running, failing, at risk and being fixed.

The core is open source (Apache-2.0). SSO, per-project governance, governed fixes, AI and corporate integrations are part of
**IntentFlow Enterprise**: [EDITIONS](docs/EDITIONS.md).

## Problems covered

| # | Pain | How it is prevented | Verified with |
|---|---|---|---|
| 1 | Upstream changes break consumers (removed/renamed column, type) | catalog diff + column lineage → impact | real dbt (DuckDB and PostgreSQL) |
| 2 | "What breaks if I change this?" | column lineage (SQLGlot), external jobs (OpenLineage, declared Spark/Python/SQL) | answer key: 10 hard models, precision and recall 100%; real Spark |
| 3 | Badly written, expensive SQL that runs out of memory | 12 AST rules + cost through `EXPLAIN` + rewrite **checked by execution** | each rule fires on a planted model |
| 4 | Duplicated keys, abnormal volume | warehouse probes | real DuckDB and PostgreSQL |
| 5 | Unwanted data change in a PR | **data diff** (rows, keys, changed columns, sample) in the warehouse | real DuckDB and PostgreSQL |
| 6 | Predictable orchestration failure (no retry/timeout, backfill, secret in the flow YAML) | lint of Airflow DAGs and Kestra flows | sample files |
| 7 | Cluster memory: OOM, exhaustion, spill, saturated pool | host/Docker/Airflow samples + forecast + warehouse spill | simulated Docker, real Airflow |
| 8 | Recurring failures, duration regression, SLA and lateness | run history + policies | **real** Airflow and Kestra |
| 9 | Unmasked PII downstream | propagation through the lineage, also into external jobs | real dbt |
| 10 | Alert noise | root-cause incidents, anti-spam, `ack` with expiry, false positive rate per check | tested |
| 11 | "What can break in the whole process?" | `intentflow risks`: visibility gaps, fragile columns, external jobs, process gaps | [RISKS](docs/RISKS.md) |

Full catalog: [docs/CHECKS.md](docs/CHECKS.md).

## Usage (CLI)

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev,secrets]'

# one-time setup (in the store; connection credentials through environment variables)
intentflow config set dbt.target project/target
intentflow config set airflow.url http://airflow:8080
intentflow policy pii-allow customers            # policies
intentflow policy sla airflow:etl 30             # SLA in minutes
intentflow notify add-sink data --type slack --url-env SLACK_URL
intentflow notify route data --min-severity high

# day to day
intentflow check --format github --fail-on high   # CI
intentflow gate --stage pre-run                    # gate before dbt (Airflow/Kestra)
intentflow diff --base prod.orders --head dev.orders --key id --sample-pct 5   # huge table: ~5 % of the keys (hash), with an estimate of the total
intentflow lineage --column orders.status --impact # what this column affects (direct, indirect and external jobs)
intentflow risks --base main/target                # what can break in the whole process
intentflow watch                                   # scheduler: collect, check, notify
intentflow ui                                      # http://127.0.0.1:8787 (local mode; see docs/DEPLOYMENT.md for a team)
intentflow user add maria --role admin             # team mode: token login, roles, MFA, per-person audit trail

# OpenLineage input (Airflow, Spark, dbt, Flink, Dagster…)
intentflow openlineage serve --port 5000
```

### Language

The CLI, the UI and the findings come in **English** or **Portuguese**. The CLI follows `INTENTFLOW_LANG` (`en`/`pt`) or the
system locale (`LANG=pt_BR.UTF-8`). The UI follows the browser language and has a selector at the bottom of the sidebar.
Finding ids do not depend on the language, so switching never reopens findings.

## UI

`intentflow ui` (light/dark theme): overview with indicators, incidents, findings (filters, details with recommendation, acknowledge,
false positive), lineage and impact graph, data diff, pipelines, cluster/memory, integrations, settings by form, team and audit, and a
guided welcome screen the first time. With Enterprise: fixes with step by step (proposed → approved → validated → applied),
AI suggestions, SSO and per-project roles.

## How to measure the lineage on YOUR project

```bash
dbt build && dbt docs generate
intentflow lineage-verify --target target --duckdb <file.duckdb> --package <your_package>
```
It perturbs the source data, rebuilds the models and compares with the static lineage (precision, recall, what indirect dependency explains).
On a large database: `--sample-rows 1000` (shrinks each source in the temporary **copy**, by key hash) and `--source NAME[.column]` (limits the sources).
Method and numbers on the real jaffle project: [BENCHMARKS](docs/BENCHMARKS.md).

## Verified against real systems (local)

dbt 1.12 with DuckDB **and** PostgreSQL 18 · SQLMesh 0.236 · Airflow 2.10 and **3.1** (APIs v1/v2, with a real OpenLineage emitter) · Kestra ·
Prefect · Dagster · Spark 3.5 (with the OpenLineage listener) · Kafka/Schema Registry (Redpanda) · Trino · ClickHouse · MySQL · Flink · Kubernetes ·
Prometheus · OpenTelemetry · SQLFluff · Delta/Iceberg · Airbyte · BigQuery (emulator) · Argo Workflows · Mage · Marquez · Mailpit · Mattermost ·
real outputs of Great Expectations 1.24 and Soda 3.5 · Keycloak 26 · PostgreSQL 18 as the store · Docker/host.

## Limits you need to know

- **Lineage:** answer key of 10 hard models (windows, unnest, union, pivot, macro, self-join, `exclude`), two sample projects and
  Snowflake/BigQuery/Databricks fixtures. Measured with the dynamic oracle on real public dbt projects (jaffle-shop, dbt-project-evaluator,
  ra_data_warehouse): **the impact never underestimated** (100 % recall at model level); **precision** is only a lower bound (~40 %). No
  third-party production pipeline has been measured yet: [BENCH_REAL_PROJECT](docs/BENCH_REAL_PROJECT.md) and the [PILOT](docs/PILOT.md) guide.
- **External jobs:** Python analysis is heuristic (it never runs the code). Dynamic code (`exec`, `eval`, `getattr`), UDFs and `mapInPandas`
  make the output conservative; `if/else` adds both branches. Without `columnLineage`/`feeds`/`sql_file`/`py_file` propagation is conservative.
- **Scale (measured):** synthetic lineage of 5,000 models / 56 thousand columns in ~11 s cold (4 processes) and 0.6 s cached; on a real
  project expect ~3× more per model. `INTENTFLOW_WORKERS` sets the processes.
- **Load safety:** reading a SQLMesh project runs its code; it requires `INTENTFLOW_SQLMESH_ALLOW_EXEC=1` or the JSON from `sqlmesh-export`.
- **Cloud:** Snowflake, Databricks (SQL and Jobs), Redshift and Fivetran were tested **only with simulated responses**; BigQuery, with the
  emulator. AWS (Glue, Step Functions, Athena, Glue Catalog) only on the `moto` emulator; Azure Data Factory and Unity Catalog only simulated.
  How to connect each one: [CLOUD_SETUP](docs/CLOUD_SETUP.md).
- **False positives:** the rate per check is measured from your feedback; there is no real usage data yet.
- **Translations:** findings are stored in the language of the process that ran the check.

## Documentation

[PILOT](docs/PILOT.md) (guide to try it on a real pipeline) · [RISKS](docs/RISKS.md) · [DEPLOYMENT](docs/DEPLOYMENT.md) (team, roles, TLS) ·
[ARCHITECTURE](docs/ARCHITECTURE.md) · [CHECKS](docs/CHECKS.md) · [INTEGRATIONS](docs/INTEGRATIONS.md) · [CLOUD_SETUP](docs/CLOUD_SETUP.md) ·
[BENCHMARKS](docs/BENCHMARKS.md) · [BENCH_REAL_PROJECT](docs/BENCH_REAL_PROJECT.md) · [ROADMAP](docs/ROADMAP.md) · [DECISIONS](docs/DECISIONS.md) ·
[EDITIONS](docs/EDITIONS.md) · Portuguese: [docs/pt-BR](docs/pt-BR/)

## License

Open source core under [Apache-2.0](LICENSE). SSO, per-project governance, governed fixes, AI and corporate integrations form the commercial
edition: [EDITIONS](docs/EDITIONS.md). [CONTRIBUTING](CONTRIBUTING.md) · [SECURITY](SECURITY.md) · [CHANGELOG](CHANGELOG.md)
