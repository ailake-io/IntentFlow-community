# Roadmap and decision criteria

[Português](pt-BR/ROADMAP.md)

## Done and verified locally

Lineage and impact (UI and API), dbt `run_results.json`, performance (cache, 1,000 models in 24 s cold), data diff in the UI,
SLA/freshness/checks in the UI, Airflow/Kestra/Prefect/Dagster for real, real OpenLineage from Airflow and Spark, cloud dialects (fixtures),
store concurrency, packaging, basic accessibility, external jobs (Spark, Python, SQL) in the impact, `intentflow risks`,
open core split (Apache-2.0 core + Enterprise extension with an offline license), English and Portuguese in CLI, UI and docs.

CI in `.github/workflows/ci.yml` (unit on Python 3.11/3.13, integration with PostgreSQL and Mailpit, real Spark, real `dbt-ol`).
Synthetic scale: 5,000 models in 11 s (4 processes), 0.6 s cached.

Round of real projects (see [BENCH_REAL_PROJECT](BENCH_REAL_PROJECT.md)): 2 public dbt projects (ra_data_warehouse, dbt-project-evaluator),
7 lineage/check bugs fixed, dynamic oracle (100 % impact recall at model level), lineage 19× faster and parallel, sampled `data diff`,
`--sample-rows` in the oracle, incremental lineage per PR.

## What is missing and needs you

How to connect each cloud service: [CLOUD_SETUP](CLOUD_SETUP.md).

| Item | Blocker |
|---|---|
| Measure lineage precision (today only a ~40 % lower bound) and the usefulness of findings on a **production** pipeline | Someone's real project with `catalog.json` ([PILOT](PILOT.md)) |
| Real Snowflake, Databricks (SQL and Jobs), Redshift, Fivetran | User accounts and credentials (today only simulated) |
| Real BigQuery (`INFORMATION_SCHEMA.JOBS`, cost) | GCP project; the emulator covers reads/plan/diff |
| Real Airbyte Cloud, Slack, GitHub, DataHub/OpenMetadata | Accounts/tokens; Airbyte OSS, Marquez, Mattermost and SMTP already verified |
| dbt Cloud, PagerDuty, Opsgenie, Jira, Teams, GitLab, Bitbucket, Azure DevOps | Test account/token (implemented; simulated server only) |
| AWS (Glue, Step Functions, Athena, catalog) and Azure Data Factory | Account and read-only role (implemented; emulator/simulated only) |
| LLM-assisted fixes (Enterprise) | Authorized model and data; only worth it if it beats the rules on real cases |

## Product decisions still open

Already settled: team users, roles, MFA; SSO/OIDC and per-project roles (Enterprise); built-in HTTPS (`--tls-cert`) or behind a proxy
([DEPLOYMENT](DEPLOYMENT.md)); open core license ([EDITIONS](EDITIONS.md)); English first with Portuguese.

Open: where it runs by default (notebook × team server × hosted cloud); publishing the core from a fresh history.

## Not implemented yet (no external blocker)

Meltano; Evidently/whylogs/Pandera; Hive Metastore; automatic Azure token refresh; Oracle; Redshift Serverless (`sys_query_history`);
Snowflake key pair/PAT; more rule-based fixes (today: rename column, expand `SELECT *`, model owner, incremental `unique_key`; missing YAML
ones: description/test); screen-reader accessibility review; UI view of `risks`.

## How to know whether it is worth continuing

| Measure | Threshold |
|---|---|
| Columns with correct lineage on real projects | ≥ 85% |
| Incidents/month from upstream changes in the user's context | ≥ 1 |
| False positive rate per check (UI/`intentflow stats`) | < 50% to keep the CI gate on |
| Diagnosis time saved on a real incident | measurable |
