# Connecting cloud services (the ones that could not run in Docker)

[Português](pt-BR/CLOUD_SETUP.md)

These services only exist as SaaS or need a paid account. IntentFlow has the code for them, **tested only with simulated responses**.
For each one this guide says what to create, with which minimum permission, which variables to set, how to turn it on in IntentFlow and how
to confirm it works. Once you run it, **report the result** (or open the test mentioned): that is how each item moves from "simulated" to "verified".

General rules:

- **Credentials only through environment variables** of the process (`intentflow ui`, `watch`, CI). The store keeps URLs and variable *names*, never the secret.
- **Read-only.** Create a dedicated user/role with read access only; IntentFlow writes to none of these services.
- **Test with the CLI first** (`intentflow runs`, `intentflow diff`, `intentflow check`), then in the UI.
- No automated cloud test runs without the variables; without them it is skipped.

## Summary

| Service | For | State today | Section |
|---|---|---|---|
| Snowflake | probes, plan, diff, spill | simulated | [1](#1-snowflake) |
| BigQuery | probes, diff, cost (dry run), `resourcesExceeded` | real emulator (read/diff); `INFORMATION_SCHEMA.JOBS` simulated | [2](#2-bigquery) |
| Databricks (SQL warehouse) | probes, plan, diff, spill | simulated | [3](#3-databricks-sql-and-jobs) |
| Databricks Jobs | job runs | simulated | [3](#3-databricks-sql-and-jobs) |
| Redshift | probes, plan, spill | simulated | [4](#4-redshift) |
| Fivetran | last sync per connector | simulated | [5](#5-fivetran) |
| Airbyte Cloud | sync runs | **real OSS (abctl)**; Cloud only differs in URL/token, not tested | [6](#6-airbyte-cloud) |
| dbt Cloud | runs and artifacts | implemented; **simulated server only** | [7](#7-dbt-cloud) |
| PagerDuty / Teams / Jira / Opsgenie / GitLab / Bitbucket / Azure DevOps (Enterprise) | alerts and PR comments | implemented; **simulated server only** (GitLab also real) | [8](#8-alerts-enterprise) |
| DataHub / OpenMetadata / Atlan | catalog | OpenLineage emitter verified on Marquez | [9](#9-catalogs) |
| AWS (Glue, Step Functions, Athena) / Azure Data Factory | orchestration, queries, catalog | implemented; **AWS on the emulator only**, Azure simulated | [10](#10-aws-and-azure) |

---

## 1. Snowflake

**Create** (as `ACCOUNTADMIN` or equivalent):

```sql
create role if not exists INTENTFLOW_RO;
grant usage on warehouse <YOUR_WAREHOUSE> to role INTENTFLOW_RO;              -- a small (XS) warehouse is enough
grant usage on database <DB> to role INTENTFLOW_RO;
grant usage on all schemas in database <DB> to role INTENTFLOW_RO;
grant select on all tables in database <DB> to role INTENTFLOW_RO;
grant select on future tables in database <DB> to role INTENTFLOW_RO;
grant imported privileges on database SNOWFLAKE to role INTENTFLOW_RO;        -- ACCOUNT_USAGE.QUERY_HISTORY (spill)
create user if not exists INTENTFLOW_SVC password = '<strong password>' default_role = INTENTFLOW_RO must_change_password = false;
grant role INTENTFLOW_RO to user INTENTFLOW_SVC;
```

**Variables:** `SNOWFLAKE_ACCOUNT` (`orgname-accountname`), `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, optional `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE`.
`pip install intentflow[snowflake]`.

**Turn on:** `intentflow config set warehouse.type snowflake`.

**Confirm:**
```bash
intentflow diff --base DB.SCHEMA.TABLE_A --head DB.SCHEMA.TABLE_B --key ID     # exercises describe + queries
intentflow check                                                               # plan (EXPLAIN USING JSON) and spill
```

**Known limits:** password authentication only (accounts with mandatory MFA or a network policy need key pair/PAT: **not supported yet**);
`ACCOUNT_USAGE.QUERY_HISTORY` lags up to ~45 min; the Snowflake plan does not estimate rows (only bytes).

## 2. BigQuery

**Create:** a service account with the roles `BigQuery Data Viewer`, `BigQuery Job User` and `BigQuery Resource Viewer`
(the last one grants `bigquery.jobs.listAll`, needed for `INFORMATION_SCHEMA.JOBS_BY_PROJECT`).

```bash
gcloud iam service-accounts create intentflow-ro
for r in roles/bigquery.dataViewer roles/bigquery.jobUser roles/bigquery.resourceViewer; do
  gcloud projects add-iam-policy-binding YOUR_PROJECT --member=serviceAccount:intentflow-ro@YOUR_PROJECT.iam.gserviceaccount.com --role=$r
done
gcloud iam service-accounts keys create key.json --iam-account=intentflow-ro@YOUR_PROJECT.iam.gserviceaccount.com
```
(Prefer Workload Identity or `gcloud auth application-default login` to a key file when you can.)

**Variables:** `GOOGLE_APPLICATION_CREDENTIALS` (key path), `BIGQUERY_PROJECT`, `BIGQUERY_REGION` (default `us`). `pip install intentflow[bigquery]`.

**Turn on:** `intentflow config set warehouse.type bigquery`.

**Confirm:** relations with the full name, `` `project.dataset.table` ``:
```bash
intentflow diff --base '`proj.ds.a`' --head '`proj.ds.b`' --key id
```
The plan uses a *dry run* (bytes that would be read); the estimated cost shows up in the cost check.

**Already verified with the emulator** (`goccy/bigquery-emulator`): `describe`, dry-run plan, data diff. **Not verified:**
`INFORMATION_SCHEMA.JOBS_BY_PROJECT` with `resourcesExceeded` (the emulator does not implement it).

## 3. Databricks (SQL and Jobs)

**Create:**
1. A **SQL warehouse** (Serverless, smallest size) and a **service principal** (or user) with a personal access token.
2. In Unity Catalog: `GRANT USE CATALOG, USE SCHEMA, SELECT ON ...` to the principal.
3. For spill: `GRANT SELECT ON system.query.history` and enable the `system.query` schema (metastore admin).
4. For Jobs: `CAN VIEW` on the desired jobs.

**Variables:** `DATABRICKS_HOST` (without `https://`), `DATABRICKS_HTTP_PATH` (of the SQL warehouse), `DATABRICKS_TOKEN`. `pip install intentflow[databricks]`
(note: this extra pulls a vulnerable `oauthlib`; see [DEPLOYMENT](DEPLOYMENT.md#container)).

**Turn on:**
```bash
intentflow config set warehouse.type databricks                            # probes/plan/diff/spill
intentflow config set databricks.jobs_host adb-123.4.azuredatabricks.net   # job runs
```

**Confirm:** `intentflow runs --databricks adb-123.4.azuredatabricks.net` (lists runs) and `intentflow diff ...` with `catalog.schema.table`.

**Limits:** the plan uses `EXPLAIN COST` (statistics only exist if `ANALYZE TABLE ... COMPUTE STATISTICS` ran); the runs API lists at most 25 per call.

## 4. Redshift

**Create:** a read user + visibility of system logs.
```sql
create user intentflow_ro password '<strong password>';
grant usage on schema <schema> to intentflow_ro;
grant select on all tables in schema <schema> to intentflow_ro;
alter user intentflow_ro syslog access unrestricted;     -- see svl_query_summary of other users
```
**Variable:** `REDSHIFT_URL=postgresql://intentflow_ro:PASSWORD@cluster.xxx.us-east-1.redshift.amazonaws.com:5439/dev?sslmode=require`.

**Turn on:** `intentflow config set warehouse.type redshift`.

**Confirm:** `intentflow diff` and `intentflow check`; check whether spill (`svl_query_summary`) shows up under Cluster.

**Limits:** spill uses `svl_query_summary` (provisioned). **Redshift Serverless** has no `svl_*`: `sys_query_history` is **not implemented yet**
(today it returns empty without an error). The plan is read from the `EXPLAIN` text (no JSON).

## 5. Fivetran

**Create:** under *Settings → API Config* generate an **API key + secret** for a read-only user (e.g. `Account Reviewer`).

**Variables:** `FIVETRAN_API_KEY`, `FIVETRAN_API_SECRET`.

**Turn on:** `intentflow config set fivetran.url https://api.fivetran.com`.

**Confirm:** `intentflow runs --fivetran https://api.fivetran.com` — one item per non-paused connector with its last sync. Paused connectors are skipped on purpose.

**Limit:** the listing API only returns the **last** sync per connector, with no duration history.

## 6. Airbyte Cloud

Against **Airbyte OSS** (`abctl`, v1.15) the integration was really verified. **Airbyte Cloud** uses the same public API with another base:

- Base: `https://api.airbyte.com/v1` — **without** the `/api/public` prefix OSS uses. The client builds `{url}/api/public/v1/...`, so the Cloud
  listing may return 404. If it does, send the response of `curl -H "Authorization: Bearer $TOKEN" "https://api.airbyte.com/v1/jobs?limit=1"`.
- Token: `POST https://api.airbyte.com/v1/applications/token` with a `client_id`/`client_secret` created in *Settings → Applications*.

**Variables (OSS and Cloud):** `AIRBYTE_CLIENT_ID` + `AIRBYTE_CLIENT_SECRET` (exchanged for a token, renewed every ~2 min), or a ready `AIRBYTE_TOKEN`,
or `AIRBYTE_USER`/`AIRBYTE_PASSWORD` (Basic, older installs). On OSS: `abctl local credentials`.

**Turn on:** `intentflow config set airbyte.url http://airbyte:8000` (OSS) · `https://api.airbyte.com` (Cloud).

**Confirm:** `intentflow runs --airbyte <url>` → one `CONNECTION_NAME:sync` item per job, with state and duration.

## 7. dbt Cloud

Implemented (runs + artifacts); tested only against a local server with the API v2 format.

**Create:** under *Account Settings → Service Tokens*, a **Read-only** (or *Job Viewer*) token, and note the `account_id` (in the URL) and the
`job_id` of a job that runs `dbt build` + `dbt docs generate`.

**Variable:** `DBT_CLOUD_TOKEN`. **Turn on:**
```bash
intentflow config set dbt_cloud.account_id 12345
intentflow config set dbt_cloud.job_id 67890
intentflow config set dbt_cloud.host cloud.getdbt.com      # or your tenant's host (e.g. emea.dbt.com, ab123.us1.dbt.com)
```
**Confirm:**
```bash
intentflow dbt-cloud runs                              # runs with state and duration
intentflow dbt-cloud pull --job 67890 --dest ./target  # artifacts of the last successful run
intentflow check                                       # without a local dbt.target it uses the job artifacts
```

## 8. Alerts (Enterprise)

All implemented as outputs (fields and variables in [DEPLOYMENT](DEPLOYMENT.md#alerts-and-prs-outputs)); tested with a local server (GitLab also with a real instance).

| Destination | What to create | How to turn on |
|---|---|---|
| PagerDuty | *Service → Integrations → Events API v2* → **Integration Key** | `PAGERDUTY_ROUTING_KEY`; output `{"type":"pagerduty"}` |
| Opsgenie | *Settings → API key management* | `OPSGENIE_API_KEY`; `{"type":"opsgenie"}`; `api` = `https://api.eu.opsgenie.com` in the EU |
| Jira | user API token; existing project and issue type | `JIRA_USER` (e-mail), `JIRA_TOKEN`; `{"type":"jira","url":"https://x.atlassian.net","project":"DATA"}` |
| Teams | *Workflows → "Post to a channel when a webhook request is received"* → URL | `TEAMS_URL`; `{"type":"teams","url_env":"TEAMS_URL"}` |
| GitLab | *Access Token* with `api` scope | `GITLAB_TOKEN`; `{"type":"gitlab","project":"group/proj","mr":12}`. **Verified** against a real GitLab 17.5 |
| Bitbucket Cloud | *Repository access token* with `pullrequest:write` | `BITBUCKET_TOKEN` |
| Azure DevOps | PAT with *Code (Read & Write)* | `AZURE_DEVOPS_PAT` |

**Confirm:** `intentflow notify send --dry-run` (shows what would go out), then without `--dry-run`.

## 9. Catalogs

`intentflow openlineage emit --url <base> --target <project>` sends column lineage as OpenLineage. **Verified on Marquez** (jobs, dataset graph and
`column-lineage`, across two hops). DataHub (`/openapi/openlineage/api/v1/lineage`, token in `OPENLINEAGE_API_KEY`), OpenMetadata, Atlan and
Collibra: **not tested**.

```bash
pip install acryl-datahub && datahub docker quickstart                                 # GMS (:8080) and frontend (:9002)
intentflow openlineage emit --url http://localhost:8080 --target <project> --dry-run
```

## 10. AWS and Azure

Implemented; **AWS verified only against the `moto` emulator**, Azure only with a local server.

**AWS** — `pip install intentflow[aws]`. Credentials: the default boto3 chain. Minimum read policy:
```json
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":[
  "glue:GetJobs","glue:GetJobRuns","glue:GetDatabases","glue:GetTables","glue:GetTable",
  "states:ListStateMachines","states:ListExecutions",
  "athena:ListQueryExecutions","athena:GetQueryExecution","athena:BatchGetQueryExecution"],"Resource":"*"}]}
```
For the **Athena warehouse** add `athena:StartQueryExecution`, `athena:GetQueryResults`, `athena:GetTableMetadata`, the output bucket
(`ATHENA_OUTPUT_LOCATION`) and read access to the data.

```bash
intentflow config set aws.region us-east-1
intentflow config set aws.glue true && intentflow config set aws.stepfunctions true && intentflow config set aws.athena true
intentflow runs                               # glue / stepfunctions / athena systems
intentflow config set catalog.glue true       # Glue Data Catalog schema (CAT001 from the 2nd collection)
intentflow config set warehouse.type athena   # probes/diff/plan on Athena
```
**Azure Data Factory:**
```bash
az role assignment create --assignee <principal> --role Reader --scope /subscriptions/SUB/resourceGroups/RG/providers/Microsoft.DataFactory/factories/FACTORY
export AZURE_TOKEN=$(az account get-access-token --resource https://management.azure.com --query accessToken -o tsv)   # lasts ~1 h
intentflow config set azure.data_factory SUB/RG/FACTORY
```
The Azure token expires in about 1 h: for `watch` use a managed identity/script that renews `AZURE_TOKEN` (**automatic renewal not implemented yet**).

**Unity Catalog:** `catalog.unity` with the workspace host and `DATABRICKS_TOKEN` with `USE CATALOG`/`USE SCHEMA`/`SELECT` on the watched tables.

**Not implemented yet:** Hive Metastore (Thrift), Redshift Serverless (`sys_query_history`), Glue DataBrew/Crawlers, EMR.

---

## Reporting the result

For each service you turn on, paste the output of `intentflow runs`, `intentflow check` and, for warehouses, `intentflow diff ...`. If something
fails, **the error text** (without secrets) is enough: most fixes are about response formats.
