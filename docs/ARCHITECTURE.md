# Architecture

[Português](pt-BR/ARCHITECTURE.md)

## Principle

A control layer **next to** the existing tools. It never runs a transformation of its own.
Read-oriented collectors; code changes only through an approved proposal (Enterprise).
Connection credentials come from the environment; MFA/SSO secrets and AI keys can be persisted encrypted (see `DEPLOYMENT.md`).
**No YAML configuration files:** sources, policies, routes and outputs live in the store (`settings` table) and are edited through the CLI or UI.

```
dbt/SQLMesh (manifest/catalog) · Airflow (REST, DAGs) · Kestra (REST, flows) · Spark/OpenLineage · warehouses · Docker/host
                │
        Collectors (read-only)
                │
      ┌─────────▼──────────┐
      │ SQLite/PG store    │  findings (lifecycle) · runs · samples · history · proposals · audit
      └─────────▲──────────┘
                │
   Pluggable checks  ──►  findings  ──►  CLI (CI/gate)  ·  UI  ·  (Enterprise) governed proposals
```

## Modules (`src/intentflow`)

| Module | Role |
|---|---|
| `dbt_loader` | Reads `manifest.json`/`catalog.json`. Tells real columns (catalog) from columns only declared in YAML. |
| `sqlmesh_loader` | A SQLMesh project (or its exported snapshot) becomes the same `Project`. |
| `checks/` | Check registry `@check(id, title, stage, cost)`. A broken check becomes an `info` finding and does not stop the others. |
| `warehouses/` | Reads: DuckDB, PostgreSQL, Trino, ClickHouse, MySQL, SQL Server, Athena, Redshift; Snowflake, BigQuery, Databricks (`cloud.py`). |
| `orchestrators/` | Airflow (REST v1/v2), Kestra, Prefect, Dagster, Airbyte, Fivetran, Spark, Databricks Jobs, Argo, Mage, AWS, ADF… → normalized `Run`. |
| `monitoring` | Memory samples (host, Docker, Airflow pools, Kubernetes, Prometheus, spill) and exhaustion forecast. |
| `lineage` | **Direct** lineage (column by column) and **indirect** (a column used in JOIN/filter/grouping/ordering/window/CASE affects the whole model). Measured on real projects by `lineage_oracle`. Output columns are **inferred from the compiled SELECT**; a parent without column edges falls back to a model dependency (`opaque`), never to "nothing". Per-model cache (hash of SQL, dialect and parents' schema): base and head of a PR share the cache. Models out of the cache go to N processes (`spawn`, default 4, `INTENTFLOW_WORKERS`). |
| `external` / `pyflow` | Reads of jobs outside the SQL (OpenLineage + `intentflow.lineage.json`), matched to models; static analysis (AST) of Python/PySpark/pandas jobs; they feed `impact` and `OL001`–`OL004`. |
| `lineage_api` | Lineage/impact queries for the UI and CLI (transitive impact, external jobs). |
| `risks` | Report of what can break in the whole process (visibility, fragility, external jobs, process); CLI `risks`. |
| `store` | SQLite (file) or PostgreSQL (`postgresql://`, `env:VAR`); same API, `copy_store` migrates between them. Findings: open → acknowledged (with expiry) → resolved automatically. |
| `overview` / `web` | Single view and UI (loopback by default, anti-CSRF, Host check). |
| `runner` | Runs a check (CLI, UI and scheduler): sources and policies come from the store. |
| `incidents` / `notify` | Groups findings by root cause; routes by owner/severity to Slack, webhook, e-mail, GitHub PR; 24 h anti-spam. |
| `datadiff` | Data diff between two relations in the warehouse: ~10 queries regardless of the number of columns and, with `sample_pct`, sampling by key hash. |
| `lineage_oracle` | Dynamic lineage validation (`lineage-verify`): perturbs sources in a DuckDB copy, rebuilds only the affected models and compares. |
| `openlineage` / `ol_emit` | OpenLineage input (file or HTTP receiver) and output (project lineage to a catalog). |
| `i18n` | Translations: English source strings, `locale/pt.json`; `tr()` in Python, `T()` in the UI. Finding ids use the English rendering. |
| `extensions` | Extension point (`intentflow.extensions` entry points): CLI commands, web routes, sessions, outputs, integration cards. |
| `cli` | `check`, `gate`, `diff`, `lineage`, `risks`, `runs`, `monitor`, `watch`, `notify`, `openlineage`, `overview`, `ui`, `user`, `config`, `policy`, `ack`, `fp`, `stats`, `prune`… |

**IntentFlow Enterprise** (separate package) adds `proposals`, `file_transaction`, `dbt_sandbox` (governed fixes), `llm`, `llm_fix`, `jev` (AI),
`oidc` (SSO), per-project roles/scopes, multiple projects and corporate outputs, through the extension hooks: [EDITIONS](EDITIONS.md).

## Checks: three triggers, three costs

- **Trigger:** `pre-merge` (PR/CI), `pre-run` (gate before the job), `continuous` (history).
- **Cost:** `static` (code/metadata only), `probe` (warehouse query), `statistical` (accumulated history).

A finding carries: a stable id, severity (by downstream impact), evidence, affected nodes, confidence and, when there is one, a fix.

## Security guarantees

- Loading a SQLMesh project runs its code: it requires `INTENTFLOW_SQLMESH_ALLOW_EXEC=1` or the JSON snapshot.
- Collectors use read queries. The effective restriction also depends on the credential's permissions in the warehouse.
- Declared lineage files (`intentflow.lineage.json`) may come from a PR: `sql_file`/`py_file` are confined to its directory, with size limits,
  linear-time parameter rendering and an evaluation budget for Python analysis; error messages never echo the content.
- The UI uses loopback by default. External exposure requires team mode and TLS or a declared proxy; `Host`, origin and the `X-IntentFlow` header
  are checked. Request bodies have a limit and a timeout.
- Enterprise fixes: applying requires an approval and a validation bound to the same content; validation runs in Docker without network.

Evidence and open items: [2026-10-13 review (Portuguese)](pt-BR/REVIEW_2026-10-13.md).

## How to extend

- **New check:** a function `(ctx) -> list[Finding]` with `@check("XXX001", "Title", stage, cost)` in `checks/`; user-facing text through `tr()`.
- **New warehouse:** a `Warehouse` subclass with `rows()` (and `spill_report()` if there is a memory signal).
- **New orchestrator:** a client that returns `Run` and, if there are any, pools/queues for `monitoring`.
- **New translation:** add `locale/<lang>.json` (English string → translation); `tests/test_i18n.py` checks coverage and placeholders.
