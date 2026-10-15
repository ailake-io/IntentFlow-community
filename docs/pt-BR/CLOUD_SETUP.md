# Ligando serviços de nuvem (o que eu não consegui rodar em Docker)

[English](../CLOUD_SETUP.md)

Estes serviços só existem como SaaS ou exigem conta paga. O IntentFlow tem o código para eles, **testado só com respostas simuladas**.
Este guia diz, para cada um: o que criar, com que permissão mínima, quais variáveis definir, como ligar no IntentFlow e como confirmar que
funciona. Depois de rodar, **me diga o resultado** (ou abra o teste indicado): é assim que cada item sai de "simulado" para "verificado".

Regras gerais:

- **Credenciais só por variável de ambiente** do processo (`intentflow ui`, `watch`, CI). O armazém guarda URL e *nomes* de variável, nunca o segredo.
- **Somente leitura.** Crie um usuário/papel dedicado só com leitura; o IntentFlow não escreve em nenhum desses serviços.
- **Teste primeiro por CLI** (`intentflow runs`, `intentflow diff`, `intentflow check`), depois pela UI.
- Nenhum teste automatizado de nuvem roda sem as variáveis; sem elas ele é pulado.

## Resumo

| Serviço | Para quê | Estado hoje | Seção |
|---|---|---|---|
| Snowflake | sondas, plano, diff, spill | simulado | [1](#1-snowflake) |
| BigQuery | sondas, diff, custo (dry run), `resourcesExceeded` | emulador real (leitura/diff); `INFORMATION_SCHEMA.JOBS` simulado | [2](#2-bigquery) |
| Databricks (SQL warehouse) | sondas, plano, diff, spill | simulado | [3](#3-databricks-sql-e-jobs) |
| Databricks Jobs | execuções de jobs | simulado | [3](#3-databricks-sql-e-jobs) |
| Redshift | sondas, plano, spill | simulado | [4](#4-redshift) |
| Fivetran | último sync por conector | simulado | [5](#5-fivetran) |
| Airbyte Cloud | execuções de sync | **OSS (abctl) real**; Cloud só difere em URL/token, não testado | [6](#6-airbyte-cloud) |
| dbt Cloud | execuções e artefatos | implementado; **só servidor simulado** | [7](#7-dbt-cloud) |
| PagerDuty / Teams / Jira / Opsgenie / GitLab / Bitbucket / Azure DevOps | alertas e comentário em PR | implementado; **só servidor simulado** | [8](#8-alertas-pagerduty-teams-jira-gitlab) |
| DataHub / OpenMetadata / Atlan | catálogo | emissor OpenLineage verificado no Marquez | [9](#9-catálogos) |
| AWS (Glue, Step Functions, Athena) / Azure Data Factory | orquestração, consulta, catálogo | implementado; **AWS só em emulador**, Azure simulado | [10](#10-aws-e-azure) |

---

## 1. Snowflake

**Criar** (como `ACCOUNTADMIN` ou equivalente):

```sql
create role if not exists INTENTFLOW_RO;
grant usage on warehouse <SEU_WAREHOUSE> to role INTENTFLOW_RO;               -- warehouse pequeno (XS) basta
grant usage on database <DB> to role INTENTFLOW_RO;
grant usage on all schemas in database <DB> to role INTENTFLOW_RO;
grant select on all tables in database <DB> to role INTENTFLOW_RO;
grant select on future tables in database <DB> to role INTENTFLOW_RO;
grant imported privileges on database SNOWFLAKE to role INTENTFLOW_RO;        -- ACCOUNT_USAGE.QUERY_HISTORY (spill)
create user if not exists INTENTFLOW_SVC password = '<senha forte>' default_role = INTENTFLOW_RO must_change_password = false;
grant role INTENTFLOW_RO to user INTENTFLOW_SVC;
```

**Variáveis:** `SNOWFLAKE_ACCOUNT` (formato `orgname-accountname`), `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, opcionais `SNOWFLAKE_WAREHOUSE`,
`SNOWFLAKE_ROLE`. `pip install intentflow[snowflake]`.

**Ligar:** `intentflow config set warehouse.type snowflake`.

**Confirmar:**
```bash
intentflow diff --base DB.SCHEMA.TABELA_A --head DB.SCHEMA.TABELA_B --key ID     # exercita describe + consultas
intentflow check                                                                  # plano (EXPLAIN USING JSON) e spill
```

**Limites conhecidos:** só autenticação por senha (contas com MFA obrigatório ou política de rede exigem key-pair/PAT: **não suportado ainda**);
`ACCOUNT_USAGE.QUERY_HISTORY` tem latência de até ~45 min; o plano do Snowflake não estima linhas (só bytes).

## 2. BigQuery

**Criar:** uma service account com os papéis `BigQuery Data Viewer`, `BigQuery Job User` e `BigQuery Resource Viewer`
(este último dá `bigquery.jobs.listAll`, necessário para `INFORMATION_SCHEMA.JOBS_BY_PROJECT`).

```bash
gcloud iam service-accounts create intentflow-ro
for r in roles/bigquery.dataViewer roles/bigquery.jobUser roles/bigquery.resourceViewer; do
  gcloud projects add-iam-policy-binding SEU_PROJETO --member=serviceAccount:intentflow-ro@SEU_PROJETO.iam.gserviceaccount.com --role=$r
done
gcloud iam service-accounts keys create key.json --iam-account=intentflow-ro@SEU_PROJETO.iam.gserviceaccount.com
```
(Prefira Workload Identity ou `gcloud auth application-default login` a arquivo de chave, se puder.)

**Variáveis:** `GOOGLE_APPLICATION_CREDENTIALS` (caminho da chave), `BIGQUERY_PROJECT`, `BIGQUERY_REGION` (padrão `us`; use `eu`,
`southamerica-east1`…). `pip install intentflow[bigquery]`.

**Ligar:** `intentflow config set warehouse.type bigquery`.

**Confirmar:** relações com o nome completo, `` `projeto.dataset.tabela` ``:
```bash
intentflow diff --base '`proj.ds.a`' --head '`proj.ds.b`' --key id
```
O plano usa *dry run* (bytes que seriam lidos); o custo estimado aparece no check de custo.

**Já verificado com o emulador** (`goccy/bigquery-emulator`): `describe`, plano por dry run, diff de dados. **Não verificado:**
`INFORMATION_SCHEMA.JOBS_BY_PROJECT` com `resourcesExceeded` (o emulador não o implementa).

## 3. Databricks (SQL e Jobs)

**Criar:**
1. Um **SQL warehouse** (Serverless, menor tamanho) e um **service principal** (ou usuário) com *personal access token*.
2. No Unity Catalog: `GRANT USE CATALOG, USE SCHEMA, SELECT ON ...` ao principal.
3. Para spill: `GRANT SELECT ON system.query.history` e habilitar o schema `system.query` (admin do metastore).
4. Para Jobs: permissão `CAN VIEW` nos jobs desejados.

**Variáveis:** `DATABRICKS_HOST` (sem `https://`), `DATABRICKS_HTTP_PATH` (do SQL warehouse), `DATABRICKS_TOKEN`.
`pip install intentflow[databricks]`.

**Ligar:**
```bash
intentflow config set warehouse.type databricks              # sondas/plano/diff/spill
intentflow config set databricks.jobs_host adb-123.4.azuredatabricks.net    # execuções de jobs
```

**Confirmar:** `intentflow runs --databricks adb-123.4.azuredatabricks.net` (lista runs) e `intentflow diff ...` com `catalogo.schema.tabela`.

**Limites:** o plano usa `EXPLAIN COST` (estatísticas só existem se `ANALYZE TABLE ... COMPUTE STATISTICS` foi rodado); a API de runs
lista no máximo 25 por chamada (usamos a mais recente).

## 4. Redshift

**Criar:** usuário de leitura + visibilidade de logs de sistema.
```sql
create user intentflow_ro password '<senha forte>';
grant usage on schema <schema> to intentflow_ro;
grant select on all tables in schema <schema> to intentflow_ro;
alter user intentflow_ro syslog access unrestricted;     -- ver svl_query_summary de outros usuários
```
**Variável:** `REDSHIFT_URL=postgresql://intentflow_ro:SENHA@cluster.xxx.us-east-1.redshift.amazonaws.com:5439/dev?sslmode=require`.

**Ligar:** `intentflow config set warehouse.type redshift` (e, opcional, `warehouse.postgres_env` com **o nome** de outra variável de URL).

**Confirmar:** `intentflow diff` e `intentflow check` com `warehouse.type redshift`; veja se o spill (`svl_query_summary`) aparece em Cluster.

**Limites:** o spill usa `svl_query_summary` (*provisioned*). **Redshift Serverless** não tem `svl_*`: use `sys_query_history`
(**ainda não implementado**; hoje retorna vazio sem erro). O plano é lido do texto do `EXPLAIN` (sem JSON).

## 5. Fivetran

**Criar:** em *Settings → API Config* gere **API key + secret** com um usuário de papel somente leitura (ex.: `Account Reviewer`).

**Variáveis:** `FIVETRAN_API_KEY`, `FIVETRAN_API_SECRET`.

**Ligar:** `intentflow config set fivetran.url https://api.fivetran.com`.

**Confirmar:** `intentflow runs --fivetran https://api.fivetran.com` — deve listar um item por conector não pausado com o último sync
(sucesso/falha). Conectores pausados são ignorados de propósito.

**Limite:** a API de listagem só traz o **último** sync por conector, sem histórico de duração (a duração aparece vazia).

## 6. Airbyte Cloud

Contra o **Airbyte OSS** (`abctl`, v1.15) a integração foi verificada de verdade. O **Airbyte Cloud** usa a mesma API pública, com outra base:

- Base: `https://api.airbyte.com/v1` — **sem** o prefixo `/api/public` que o OSS usa. O cliente do IntentFlow monta `{url}/api/public/v1/...`,
  então no Cloud a listagem pode dar 404. Se der, é só isso: me envie a resposta de
  `curl -H "Authorization: Bearer $TOKEN" "https://api.airbyte.com/v1/jobs?limit=1"` que eu ajusto o caminho.
- Token: `POST https://api.airbyte.com/v1/applications/token` com `client_id`/`client_secret` criados em *Settings → Applications*.

**Variáveis (OSS e Cloud):** `AIRBYTE_CLIENT_ID` + `AIRBYTE_CLIENT_SECRET` (o cliente troca por token e renova a cada ~2 min), ou
`AIRBYTE_TOKEN` pronto, ou `AIRBYTE_USER`/`AIRBYTE_PASSWORD` (Basic, instalações antigas). No OSS: `abctl local credentials`.

**Ligar:** `intentflow config set airbyte.url http://airbyte:8000` (OSS) · `https://api.airbyte.com` (Cloud).

**Confirmar:** `intentflow runs --airbyte <url>` → um item `NOME_DA_CONEXÃO:sync` por job, com estado e duração (`PT1M48S` vira 108 s).

**Rodar o Airbyte OSS localmente para testar** (≈ 10 min, vários GB de imagens, ~4 GB de RAM):
```bash
abctl local install --port 8000 --low-resource-mode      # use HOME isolado se já houver ~/.airbyte de outra instalação
abctl local credentials                                   # client id/secret
```
Observação: o destino "dev-null" não está no enum da API pública; para testes crie o destino *End-to-End Testing* pela API de configuração.

## 7. dbt Cloud

Implementado (runs + artefatos); testado só contra um servidor local com o formato da API v2.

**Criar:** em *Account Settings → Service Tokens*, um token com permissão **Read-only** (ou *Job Viewer*) e anote o `account_id` (na URL) e o
`job_id` de um job que rode `dbt build` + `dbt docs generate` (para ter `manifest.json`, `catalog.json` e `run_results.json`).

**Variável:** `DBT_CLOUD_TOKEN`. **Ligar:**
```bash
intentflow config set dbt_cloud.account_id 12345
intentflow config set dbt_cloud.job_id 67890
intentflow config set dbt_cloud.host cloud.getdbt.com      # ou o host do seu tenant (ex.: emea.dbt.com, ab123.us1.dbt.com)
```
**Confirmar:**
```bash
intentflow dbt-cloud runs                               # lista runs com estado e duração
intentflow dbt-cloud pull --job 67890 --dest ./target  # baixa os artefatos do último run bem-sucedido
intentflow check                                        # sem dbt.target local, usa os artefatos do job
```
Me envie a saída de `dbt-cloud runs` e, se falhar, o código HTTP: os pontos que podem divergir são o host do tenant e o caminho de
artefatos (`/jobs/{id}/artifacts/manifest.json`).

## 8. Alertas (PagerDuty, Teams, Jira, GitLab)

Todos implementados como sinks (campos e variáveis em [DEPLOYMENT](DEPLOYMENT.md#alertas-e-prs-sinks)); testados com servidor local (GitLab, também com instância real).

| Destino | O que criar | Como ligar |
|---|---|---|
| PagerDuty | *Service → Integrations → Events API v2* → **Integration Key** | `PAGERDUTY_ROUTING_KEY`; sink `{"type":"pagerduty"}` (o incidente do IntentFlow vira `dedup_key`) |
| Opsgenie | *Settings → API key management* (chave de integração) | `OPSGENIE_API_KEY`; `{"type":"opsgenie"}`; `api` = `https://api.eu.opsgenie.com` na UE |
| Jira | token de API do usuário; projeto e tipo de issue existentes | `JIRA_USER` (e-mail), `JIRA_TOKEN`; `{"type":"jira","url":"https://x.atlassian.net","project":"DATA"}` |
| Teams | *Workflows → "Post to a channel when a webhook request is received"* → URL | `TEAMS_URL`; `{"type":"teams","url_env":"TEAMS_URL"}` (Adaptive Card) |
| GitLab | *Access Token* com escopo `api` (projeto) | `GITLAB_TOKEN`; `{"type":"gitlab","project":"grupo/proj","mr":12}`; `api` para self-managed. **Verificado** contra GitLab 17.5 real |
| Bitbucket Cloud | *Repository access token* com `pullrequest:write` | `BITBUCKET_TOKEN` |
| Azure DevOps | PAT com *Code (Read & Write)* | `AZURE_DEVOPS_PAT` |

**Confirmar:** `intentflow notify --dry-run` (mostra o que sairia) e depois sem `--dry-run` com um incidente real; verifique o alerta/comentário.

## 9. Catálogos

`intentflow openlineage emit --url <base> --target <projeto>` envia a linhagem de coluna como OpenLineage. **Verificado no Marquez** (jobs,
grafo de datasets e `column-lineage` corretos, atravessando dois saltos).

| Catálogo | Endpoint / como | Estado |
|---|---|---|
| Marquez | `http://marquez:5000` | **verificado** (container `marquezproject/marquez` + Postgres) |
| DataHub | endpoint OpenLineage do `datahub-gms` (`/openapi/openlineage/api/v1/lineage`; confira a doc da sua versão); token em `OPENLINEAGE_API_KEY` | **não testado**: não consegui baixar o `docker-compose` do quickstart neste ambiente |
| OpenMetadata | conector *OpenLineage* (consome de Kafka ou do serviço de ingestão) | não testado |
| Atlan, Collibra | conector OpenLineage deles | não testado |

**Para testar o DataHub localmente** (≈ 6–8 GB de RAM):
```bash
pip install acryl-datahub && datahub docker quickstart                      # sobe GMS (:8080) e frontend (:9002)
intentflow openlineage emit --url http://localhost:8080 --target <projeto> --dry-run     # veja os eventos
intentflow openlineage emit --url http://localhost:8080 --target <projeto>
```
Se o endpoint responder 404, me diga a versão do DataHub e o caminho do OpenLineage dela.

## 10. AWS e Azure

Implementados; **AWS verificado só contra o emulador `moto`** (API real do boto3, servidor emulado), Azure só com servidor local.

**AWS** — `pip install intentflow[aws]`. Credenciais: cadeia padrão do boto3 (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, perfil ou papel).
Política mínima de leitura:
```json
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":[
  "glue:GetJobs","glue:GetJobRuns","glue:GetDatabases","glue:GetTables","glue:GetTable",
  "states:ListStateMachines","states:ListExecutions",
  "athena:ListQueryExecutions","athena:GetQueryExecution","athena:BatchGetQueryExecution"],"Resource":"*"}]}
```
Para o **warehouse Athena** (sondas/diff) some `athena:StartQueryExecution`, `athena:GetQueryResults`, `athena:GetTableMetadata`, acesso ao bucket
de saída (`ATHENA_OUTPUT_LOCATION`) e leitura dos dados.

```bash
intentflow config set aws.region us-east-1
intentflow config set aws.glue true && intentflow config set aws.stepfunctions true && intentflow config set aws.athena true
intentflow runs           # confirma: sistemas glue / stepfunctions / athena
intentflow config set catalog.glue true     # schema do Glue Data Catalog (CAT001 a partir da 2ª coleta)
intentflow config set warehouse.type athena # sondas/diff/plano no Athena
```
**Azure Data Factory:**
```bash
az role assignment create --assignee <principal> --role Reader --scope /subscriptions/SUB/resourceGroups/RG/providers/Microsoft.DataFactory/factories/FABRICA
export AZURE_TOKEN=$(az account get-access-token --resource https://management.azure.com --query accessToken -o tsv)   # vale ~1 h
intentflow config set azure.data_factory SUB/RG/FABRICA
```
O token do Azure expira em cerca de 1 h: para o `watch` use uma identidade gerenciada/script que renove `AZURE_TOKEN` (**renovação automática ainda não implementada**).

**Unity Catalog:** `catalog.unity` com o host do workspace e `DATABRICKS_TOKEN` com `USE CATALOG`/`USE SCHEMA`/`SELECT` nas tabelas observadas.

**Ainda não implementado:** Hive Metastore (Thrift), Redshift Serverless (`sys_query_history`), Glue DataBrew/Crawlers, EMR.

---

## Como me devolver o resultado

Para cada serviço que você ligar, cole a saída de:

```bash
intentflow runs        # execuções coletadas
intentflow check       # achados
intentflow diff ...    # quando for warehouse
```

Se algo falhar, **o texto do erro** (sem segredos) basta: a maioria dos ajustes é de formato de resposta.
