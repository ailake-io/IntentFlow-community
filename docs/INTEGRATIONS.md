# Integrations and how they were verified

[Português](pt-BR/INTEGRATIONS.md)

Items marked **(Enterprise)** need IntentFlow Enterprise ([EDITIONS](EDITIONS.md)).

| Integration | Use | Verification |
|---|---|---|
| **dbt** (core 1.12) + **DuckDB** | manifest/catalog, sandbox `parse`/`build` validation (Enterprise) | Real projects (`dbt_shop`, `dbt_lab`) in automated tests |
| **dbt** + **PostgreSQL 18** | another dialect: lineage, probes, JSON `EXPLAIN`, data diff | Local container, automated test (`INTENTFLOW_PG_URL`) |
| **Airflow 2.10** (REST v1, basic auth) | runs, pools, health; DAG lint | **Real instance** in a container (`INTENTFLOW_AIRFLOW_URL`) |
| **Airflow 3.1** (REST v2, JWT through `POST /auth/token`) | runs, pools, health; lint and **2 → 3 migration** (`intentflow airflow3`) | **Real instance** `apache/airflow:3.1.0` (`INTENTFLOW_AIRFLOW3_URL`); DAGs rewritten by the migrator ran on it |
| **Airflow 3.1 + OpenLineage** (provider 2.7.1) | states, duration and job names reach the receiver | **Real.** Verified limit: the emitter **does not send `inputs/outputs`** for hand-declared `Asset`s; dataset lineage on Airflow 3 needs operators with an extractor (e.g. SQL) |
| **Airflow + OpenLineage** (provider 2.0 on Airflow 2.10) | real run and input/output events (`Table` in `inlets/outlets`) to the receiver | **Real instance** emitting to the receiver (`tests/test_openlineage_airflow_real.py`) |
| **SQLMesh** 0.236 | SQLMesh project as the target: models, columns, dependencies, column lineage, impact, drift, checks | **Real:** export through SQLMesh's interpreter (`INTENTFLOW_SQLMESH_PYTHON`) over `examples/sqlmesh_shop`; JSON snapshot (`sqlmesh-export`) for CI |
| **Great Expectations** 1.24 | validation results → `DQ001`, linked to the graph | **Real:** output written by GX 1.24 (fixture); GX is never run |
| **Soda Core** 3.5 | `soda scan -srf` → `DQ001` | **Real:** output written by Soda 3.5 over DuckDB (fixture) |
| **Airbyte** OSS 1.15 (public API v1) | sync runs per connection; auth by token, client id/secret or Basic | **Real:** `abctl` (kind in Docker). **Airbyte Cloud not tested** |
| **Fivetran** (REST v1) | last sync per connector, paused ones ignored | Local HTTP server; **never against real Fivetran** |
| **Apache Spark** 3.5 (UI/History REST) | applications → runs; executor memory, spill, OOM → cluster checks | **Real:** `apache/spark:3.5.3` in a container |
| **Databricks Jobs** (API 2.1) | job run states | Local HTTP server; **never against a real workspace** |
| **Kafka + Schema Registry** (Redpanda 24.2) | group lag (`KAF001`), Avro schema break between versions (`KAF002`) | **Real:** Redpanda in a container |
| **Data contracts** (Data Contract Specification, ODCS v3) | contract × catalog/tests → `CTR001` | Sample files in `examples/contracts` |
| **OpenLineage emitter** | column lineage → DataHub, OpenMetadata, Marquez (`openlineage emit`) | **Real on Marquez**; DataHub/OpenMetadata/Atlan **not tested** |
| **Trino** 483 | probes, distributed `EXPLAIN` (JSON), data diff, queries killed by memory | **Real:** `trinodb/trino` in a container |
| **ClickHouse** 24.8 | probes, `EXPLAIN json`/`ESTIMATE`, data diff, spill/`MEMORY_LIMIT_EXCEEDED` through `query_log` | **Real:** `clickhouse-server:24.8`; `readonly=2` session |
| **Redshift** | Postgres protocol; text plan, spill through `svl_query_summary` | **Fake connection only** (no emulator) |
| **Argo Workflows** 3.4 | workflows → runs | **Real:** k3d + Argo 3.4.17 |
| **Mage** (OSS) | `pipeline_runs` → runs; session login | **Real:** `mageai/mageai` with `tests/mage_project` |
| **dbt Cloud** (API v2) | job runs; artifacts of the last successful run become `dbt.target` (`dbt-cloud pull`) | Local HTTP server with the documented format; **never against the real dbt Cloud** |
| **SQLFluff** 4.4 | `LINT001` over the compiled SQL (`pip install intentflow[lint]`) | **Real:** installed library; cache by SQL hash |
| **Kubernetes** 1.30 | `OOMKilled` pods and memory vs limit (`kubectl top`) → cluster checks | **Real:** kind 1.30 with a pod that runs out of memory |
| **Prometheus** (source) | memory series (cAdvisor, node_exporter, any PromQL query) → cluster checks | **Real:** `prom/prometheus` |
| **Prometheus** (output) | authenticated `GET /metrics`: open findings, runs, last check | Endpoint tested (exposition format) |
| **OpenTelemetry** (output) | `otel-push`: same metrics over OTLP/HTTP | **Real:** OpenTelemetry Collector 0.110 |
| **GitLab** 17.5 **(Enterprise)** | merge request note (`gitlab` output) | **Real:** `gitlab/gitlab-ce` in a container |
| **Bitbucket Cloud / Azure DevOps (Enterprise)** | PR comment | Local HTTP server with the documented format |
| **PagerDuty / Opsgenie / Jira / Teams (Enterprise)** | incident outputs (`dedup_key`/`alias` = incident id) | Local HTTP server; **never against the real services** |
| **Elementary** | `ELM001` reads `<schema>.elementary_test_results` | Documented schema created in real DuckDB |
| **Delta Lake / Apache Iceberg** | `LAKE001`: small files and incompatible schema between versions | **Real:** tables written with `deltalake` and `pyiceberg` |
| **dbt semantic layer** (MetricFlow) | semantic models, metrics and saved queries enter the graph; impact reaches metrics | **Real:** `dbt parse` on dbt 1.12 |
| **Kafka Connect / Debezium** | `FAILED` connector or task = CDC failure | Local HTTP server |
| **Apache Flink** 1.19 | jobs → runs | **Real:** jobmanager + taskmanager in containers |
| **Luigi** / **Rundeck** / **MLflow** | tasks, executions and experiment runs → runs | Local HTTP server |
| **MySQL** 8.4 | probes, `EXPLAIN FORMAT=JSON`, diff (`<=>`), `READ ONLY` session | **Real:** `mysql:8.4` |
| **SQL Server / Azure SQL** | `describe`, diff (`INTERSECT`) | **Fake connection only** |
| **AWS Glue / Step Functions / Athena** (boto3) | job runs, executions, queries; `AthenaWarehouse` | **Emulated:** `moto`; real AWS not tested |
| **Azure Data Factory** | `queryPipelineRuns` (last 24 h, paginated) | Local HTTP server; **never against Azure** |
| **Glue Data Catalog / Unity Catalog** | schema snapshot per collection; `CAT001` compares with the previous one | Glue: `moto`; Unity: local HTTP server. Hive Metastore **not implemented** |
| **LLM: Anthropic / OpenAI / Gemini (Enterprise)** | suggest fix edits (`fix-ai`, "Suggest fix") | Format verified on a local server; **never with a real key** |
| **Local LLM (Ollama) (Enterprise)** | same, without leaving the machine | **Real:** Ollama + `qwen2.5:3b` |
| **Jev (TypeSafe) (Enterprise)** | finding triage and risk gate | Simulated server with the documented format |
| **Prefect** (REST) / **Dagster** (GraphQL) | runs and state | **Real** local servers |
| **Kestra** | runs, flows; flow lint | **Real server** in a container (`INTENTFLOW_KESTRA_URL`) |
| **OpenLineage** (RunEvent 1.x) | runs, inputs/outputs, column lineage; HTTP receiver `/api/v1/lineage` | **Real Airflow and Spark 3.5.3** (`openlineage-spark` 1.24.2, column edge received) |
| **Jobs without SQL** (Spark, Python, notebooks) | see below | **Real:** Spark (`tests/test_spark_real.py`, `spark` CI job in `local-cluster`) and `dbt-ol` (`tests/test_dbt_ol_real.py`). Databricks/EMR/Kubernetes, Flink and Dagster **not tested** |
| **Docker / host** | memory, `OOMKilled` | Real host; Docker with simulated output |
| **Slack-style webhook** | outputs and routes per owner | **Real on Mattermost**; real Slack not tested |
| **E-mail (SMTP)** | opportunistic STARTTLS, `ssl`, `SMTP_USER`/`SMTP_PASSWORD` login, certificate checks | **Real on Mailpit** |
| **PR comment (GitHub)** | output per PR | Local HTTP server; real GitHub not tested |
| **dbt + Snowflake/BigQuery/Databricks dialects** (lineage) | `QUALIFY`, `:`/`::`, `UNNEST`, `SAFE_CAST`, `explode`, backtick names | Compiled SQL fixtures; **never against a real account** |
| **Snowflake** | `describe`, plan (`EXPLAIN USING JSON`), diff, spill through `account_usage.query_history` | Fake connection; **never a real account** |
| **BigQuery** | `describe`, plan (dry run), data diff; `resourcesExceeded` failures | **Real emulator** (`goccy/bigquery-emulator`); `INFORMATION_SCHEMA.JOBS` only simulated |
| **Databricks** (SQL) | `describe`, plan (`EXPLAIN COST`), diff, spill through `system.query.history` | Fake connection; **never a real account** |

### Jobs without SQL (external jobs)

Column/dataset reads enter the impact (`lineage --impact`, UI) and the checks `OL001` (changed column), `OL002` (ambiguous name, with an alias
suggestion; `external_warn_unmatched` in the policy also includes missing ones), `OL003` (PII read; `pii_allowed_jobs` allows) and `OL004`
(default Spark `appName`). Sources: ingested OpenLineage or `intentflow.lineage.json`:

```json
{"aliases": {"file:/lake/raw/customers": "stg_customers"},
 "jobs": [{"name": "ltv_spark", "owner": "team-ds",
           "reads": [{"dataset": "customers", "columns": ["email"], "feeds": [{"dataset": "wh.ltv", "columns": ["score"]}]}],
           "writes": ["wh.ltv"]},
          {"name": "ltv_sql", "sql_file": "jobs/ltv.sql", "dialect": "spark", "params": {"db": "prod"}, "writes": ["wh.ltv"]},
          {"name": "ltv_py", "py_file": "jobs/ltv.py"}]}
```

- Without `columns` = the whole dataset.
- **SQL job** (`sql`/`sql_file`): reads and `feeds` come from SQLGlot column lineage, including columns used only in JOIN/WHERE/GROUP BY
  (indirect, feeding the whole output); multi-statement scripts (`INSERT ... SELECT`, `CREATE TABLE AS`); `${v}`, `$v`, `{v}`, `{{ v }}`,
  `{{ ref() }}`, `{{ source() }}` are resolved (without a value they become `__param_v__` and still match by suffix).
- **Python job** (`py_file`): static analysis (AST, never run) of PySpark/pandas: follows variables, chained calls, functions of the file and
  sibling modules (inside the declaration directory), SQL built by concatenation/f-string/`format`, `select/withColumn/withColumnRenamed/drop/
  filter/join/groupBy.agg` and pandas (`df["c"] = ...`, `merge`, `rename`); filters and joins become indirect dependencies; what it does not
  understand (`mapInPandas`, `apply`, opaque UDF) makes the output conservative — it never disappears.
- dbt events and IntentFlow's own emitter do not become external jobs (`openlineage.keep_project_lineage` turns that off). Spark actions are
  grouped by application (`parent` facet; other engines: `openlineage.group_by_parent`).
- A change propagates through job outputs **per column** (with `columnLineage`, `feeds` or SQL) and conservatively without them;
  `lineage --coverage` lists those jobs. Matching uses the most specific suffix (last 3/2/1 path segments); `lineage --unmatched` lists what
  did not match and suggests; `lineage --alias DATASET=MODEL` writes the alias.

Credentials, always through the environment: `AIRFLOW_USER`/`AIRFLOW_PASSWORD` or `AIRFLOW_TOKEN`, `KESTRA_USER`/`KESTRA_PASSWORD`,
`INTENTFLOW_PG_URL`, `AIRBYTE_TOKEN`, `FIVETRAN_API_KEY`/`FIVETRAN_API_SECRET`, `DATABRICKS_TOKEN`, `KAFKA_USER`/`KAFKA_PASSWORD`,
`SCHEMA_REGISTRY_USER`/`SCHEMA_REGISTRY_PASSWORD`, `OPENLINEAGE_API_KEY`, `TRINO_*`, `CLICKHOUSE_*`, `REDSHIFT_URL`, `ARGO_TOKEN`/`ARGO_NAMESPACE`,
`MAGE_API_KEY`/`MAGE_USER`/`MAGE_PASSWORD`/`MAGE_TOKEN`, `DBT_CLOUD_TOKEN`, `RUNDECK_TOKEN`, `MLFLOW_TRACKING_TOKEN`, `MYSQL_*`, `MSSQL_*`,
`OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_HEADERS`, `SNOWFLAKE_*`, `BIGQUERY_PROJECT` + Google credentials, `DATABRICKS_*`,
`GITHUB_TOKEN`, `SMTP_HOST`/`SMTP_PORT`, `INTENTFLOW_UI_TOKEN`; Enterprise outputs: `GITLAB_TOKEN`, `BITBUCKET_TOKEN`, `AZURE_DEVOPS_PAT`,
`PAGERDUTY_ROUTING_KEY`, `OPSGENIE_API_KEY`, `JIRA_USER`/`JIRA_TOKEN`, `TEAMS_URL`. Nothing is written to disk; the store keeps only variable **names**.

## Turning each integration on

| Integration | How |
|---|---|
| SQLMesh | `--target path/to/project` (or `sqlmesh_export.json`); `INTENTFLOW_SQLMESH_PYTHON` points to the Python that has `sqlmesh` |
| GX / Soda | `intentflow config set dq.results 'folder,file.json'` |
| Airbyte / Fivetran / Spark / Databricks | `config set airbyte.url`, `fivetran.url` (`https://api.fivetran.com`), `spark.url`, `databricks.jobs_host`, or `runs --airbyte … --spark …` |
| Kafka | `config set kafka.bootstrap host:9092`, `kafka.schema_registry http://…`, optional `kafka.groups`; `pip install intentflow[kafka]`; limit: `policy kafka_lag_max` |
| Contracts | `config set contracts.dir folder` |
| Warehouse | `config set warehouse.type trino\|clickhouse\|redshift\|snowflake\|bigquery\|databricks\|postgres\|duckdb\|mysql\|sqlserver\|athena` + the engine's environment variables |
| Argo / Mage | `config set argo.url`, `argo.namespace`, `mage.url` |
| dbt Cloud | `config set dbt_cloud.account_id N`, `dbt_cloud.job_id N`, `dbt_cloud.host`; `DBT_CLOUD_TOKEN`; `intentflow dbt-cloud runs\|pull` |
| Kubernetes / Prometheus | `config set kubernetes.enabled true` (+ `kubernetes.context`, `kubernetes.namespace`); `config set prometheus.url http://prom:9090` |
| SQL lint | `pip install intentflow[lint]`; `config set lint.enabled true` (off by default: it is slow); rules in `policy.lint_rules` |
| Elementary | `config set elementary.schema analytics_elementary` (uses the configured warehouse) |
| Delta / Iceberg | `pip install intentflow[lakehouse]`; `config set lakehouse.tables 'delta_folder,metadata.json'`; limits `policy.small_file_count`/`small_file_bytes` |
| More runs | `config set kafka_connect.url`, `flink.url`, `luigi.url`, `mlflow.url`, `rundeck.url` (+ `rundeck.project`) |
| Alerts | `notify add-sink` with `slack`, `webhook`, `email`, `github`; Enterprise adds `pagerduty`, `opsgenie`, `jira`, `teams`, `gitlab`, `bitbucket`, `azure_devops` |
| Metrics | `GET /metrics` (Prometheus, with `Authorization: Bearer`) · `intentflow otel-push --endpoint http://collector:4318` |
| AWS | `pip install intentflow[aws]`; `config set aws.region us-east-1`, `aws.glue true`, `aws.stepfunctions true`, `aws.athena true` |
| Azure Data Factory | `config set azure.data_factory SUB/RG/FACTORY`; `AZURE_TOKEN` |
| Catalogs | `config set catalog.glue true` and/or `catalog.unity HOST` (+ `catalog.include`); comparing starts on the second collection |
| Lineage catalog | `intentflow openlineage emit --url http://catalog --target …` (`--dry-run` shows the events) |

To connect the cloud-only ones, see [CLOUD_SETUP.md](CLOUD_SETUP.md).

Not included: Evidently/whylogs/Pandera, Hive Metastore, Oracle, Dataform/Coalesce, native BI (Looker/Tableau/Power BI) — no clear demand or
no environment to verify. Meltano: there is no server API to read runs.

## Security and operation notes

- **SQLMesh runs the project's code** (`config.py`, Python models, macros). Reading a project directory **requires
  `INTENTFLOW_SQLMESH_ALLOW_EXEC=1`**; without it the error points to `sqlmesh-export`. The JSON snapshot is data only. In CI for fork PRs
  never set the variable: build the JSON in an isolated environment.
- **Credentials** of every integration come from process environment variables; the store keeps only URLs and variable names.
- **Warehouse connections** are opened per check/collection and closed at the end. ClickHouse connects with `readonly=2`; Postgres/Redshift open
  a `readonly` session.
- **Airflow 3**: an `Asset` used only as an *inlet* stays inactive and the task fails; something has to produce it (outlet). The API v2 JWT is
  requested at `POST /auth/token` and renewed every 30 min.
- **dbt exposures** (dashboards) enter the graph: model/column impact lists which dashboards are hit and their declared owner.

## Running Airflow with OpenLineage pointed at the receiver (local)

```bash
intentflow openlineage serve --port 5055 &
docker run -d --network host \
  -e AIRFLOW__WEBSERVER__WEB_SERVER_PORT=58082 \
  -e AIRFLOW__OPENLINEAGE__TRANSPORT='{"type":"http","url":"http://127.0.0.1:5055","endpoint":"api/v1/lineage"}' \
  apache/airflow:2.10.5 standalone
```

Verified: `BashOperator` with `inlets/outlets` of type `airflow.datasets.Dataset` does **not** emit datasets; with
`airflow.lineage.entities.Table` it does (cluster namespace, name `database.table`).
