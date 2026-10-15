# Integrações e como foram verificadas

[English](../INTEGRATIONS.md)

| Integração | Uso | Verificação |
|---|---|---|
| **dbt** (core 1.12) + **DuckDB** | manifest/catalog, validação `parse`/`build` em sandbox | Projetos reais (`dbt_shop`, `dbt_lab`) em teste automatizado |
| **dbt** + **PostgreSQL 18** | outro dialeto: linhagem, sondas, `EXPLAIN` JSON, diff de dados | Contêiner local, teste automatizado (`INTENTFLOW_PG_URL`) |
| **Airflow 2.10** (REST v1, basic auth) | execuções, pools, saúde; lint de DAGs | **Instância real** em contêiner (`INTENTFLOW_AIRFLOW_URL`) |
| **Airflow 3.1** (REST v2, JWT via `POST /auth/token`) | execuções, pools, saúde; lint e **migração 2 → 3** (`intentflow airflow3`) | **Instância real** em contêiner `apache/airflow:3.1.0` (`INTENTFLOW_AIRFLOW3_URL`); DAGs reescritos pelo migrador rodaram nela |
| **Airflow 3.1 + OpenLineage** (provider 2.7.1) | estados, duração e nomes de job chegam ao receptor | **Real.** Limite verificado: o emissor **não envia `inputs/outputs`** de `Asset` declarado à mão (nem `postgres://` nem `s3://`); linhagem de dataset no Airflow 3 exige operadores com extrator (ex.: SQL) |
| **Airflow + OpenLineage** (provider 2.0 do Airflow 2.10) | eventos reais de execução e de entradas/saídas (`Table` em `inlets/outlets`) para o receptor | **Instância real** emitindo para o receptor (`tests/test_openlineage_airflow_real.py`) |
| **SQLMesh** 0.236 | projeto SQLMesh como alvo (`--target`): modelos, colunas, dependências, linhagem de coluna, impacto, drift, checks | **Real:** exportação via interpretador do SQLMesh (`INTENTFLOW_SQLMESH_PYTHON`) sobre `examples/sqlmesh_shop`; snapshot JSON (`sqlmesh-export`) para CI |
| **Great Expectations** 1.24 | resultados de validação → `DQ001`, ligados ao grafo | **Real:** saída gravada pelo GX 1.24 (fixture); não executa o GX |
| **Soda Core** 3.5 | `soda scan -srf` → `DQ001` | **Real:** saída gravada pelo Soda 3.5 sobre DuckDB (fixture) |
| **Airbyte** OSS 1.15 (API pública v1) | execuções de sync por conexão; auth por token, `AIRBYTE_CLIENT_ID`/`AIRBYTE_CLIENT_SECRET` ou Basic | **Real:** `abctl` (kind em Docker), fonte faker → destino End-to-End, sync concluído e cancelado. **Airbyte Cloud não testado** (base e token diferentes) |
| **Fivetran** (REST v1) | último sync por conector, pausados ignorados | Servidor HTTP local; **nunca contra Fivetran real** |
| **Apache Spark** 3.5 (UI/History REST) | aplicações → execuções; memória de executor, spill, OOM → checks de cluster | **Real:** `apache/spark:3.5.3` em contêiner |
| **Databricks Jobs** (API 2.1) | estados de runs de jobs | Servidor HTTP local; **nunca contra workspace real** |
| **Kafka + Schema Registry** (Redpanda 24.2) | lag de grupos (`KAF001`), quebra de schema Avro entre versões (`KAF002`) | **Real:** Redpanda em contêiner (broker + registry) |
| **Data contracts** (Data Contract Specification, ODCS v3) | contrato × catálogo/testes → `CTR001` | Arquivos de exemplo em `examples/contracts`; parser testado, sem ferramenta externa |
| **OpenLineage emissor** | linhagem de coluna → DataHub, OpenMetadata, Marquez (`openlineage emit`) | **Real no Marquez** (jobs, grafo de datasets e `column-lineage`); DataHub/OpenMetadata/Atlan **não testados** |
| **Trino** 483 | sondas, `EXPLAIN` distribuído (JSON), diff de dados, consultas mortas por memória | **Real:** `trinodb/trino` em contêiner (catálogo `memory`) |
| **ClickHouse** 24.8 | sondas, `EXPLAIN json`/`ESTIMATE`, diff de dados, spill/`MEMORY_LIMIT_EXCEEDED` via `query_log` | **Real:** `clickhouse-server:24.8` em contêiner; sessão `readonly=2` (escrita recusada pelo servidor) |
| **Redshift** | protocolo Postgres; plano por texto, spill via `svl_query_summary` | **Só conexão falsa** (sem emulador); SQL e plano conforme a documentação |
| **Argo Workflows** 3.4 | workflows → execuções (por template/cron) | **Real:** k3d (Kubernetes em Docker) + Argo 3.4.17, `argo-server --auth-mode=server` |
| **Mage** (OSS) | `pipeline_runs` → execuções; login por sessão (`MAGE_USER`/`MAGE_PASSWORD`) | **Real:** `mageai/mageai` em contêiner com `tests/mage_project` |
| **dbt Cloud** (API v2) | runs de jobs; artefatos do último run bem-sucedido viram `dbt.target` (`dbt-cloud pull`, ou `dbt_cloud.job_id` na verificação) | Servidor HTTP local com o formato documentado; **nunca contra o dbt Cloud real** |
| **SQLFluff** 4.4 | `LINT001`: regras de correção/desempenho sobre o SQL compilado (`pip install intentflow[lint]`) | **Real:** biblioteca instalada; cache por hash do SQL |
| **Kubernetes** 1.30 | pods `OOMKilled` e memória vs limite (`kubectl top`, precisa do metrics-server) → checks de cluster | **Real:** kind 1.30 com pod que estoura memória (em k8s <1.28 + swap no host o OOM não reproduz) |
| **Prometheus** (fonte) | séries de memória (cAdvisor, node_exporter, qualquer consulta PromQL) → checks de cluster | **Real:** `prom/prometheus` |
| **Prometheus** (saída) | `GET /metrics` autenticado: achados abertos, execuções, última verificação | Endpoint testado (formato de exposição); scrape pelo Prometheus não testado |
| **OpenTelemetry** (saída) | `otel-push`: mesmas métricas por OTLP/HTTP | **Real:** OpenTelemetry Collector 0.110 |
| **GitLab** 17.5 | nota em merge request (sink `gitlab`; projeto por id ou `grupo/projeto`) | **Real:** `gitlab/gitlab-ce` em contêiner (projeto, branch, MR, nota lida de volta) |
| **Bitbucket Cloud / Azure DevOps** | comentário em PR (sinks `bitbucket`, `azure_devops`) | Servidor HTTP local com o formato documentado |
| **PagerDuty** (Events v2) / **Opsgenie** / **Jira** / **Teams** | sinks de incidente (`dedup_key`/`alias` = id do incidente) | Servidor HTTP local com os formatos documentados; **nunca contra os serviços reais** |
| **Elementary** | `ELM001` lê `<schema>.elementary_test_results` do warehouse | Esquema da documentação criado em DuckDB real; não roda o pacote dbt-elementary |
| **Delta Lake / Apache Iceberg** | `LAKE001`: arquivos pequenos e schema incompatível entre versões | **Real:** tabelas gravadas com `deltalake` e `pyiceberg` |
| **dbt semantic layer** (MetricFlow) | modelos semânticos, métricas e saved queries entram no grafo; o impacto chega às métricas | **Real:** `dbt parse` do dbt 1.12 com time spine |
| **Kafka Connect / Debezium** | conector ou task `FAILED` = falha de CDC | Servidor HTTP local; Connect real não testado |
| **Apache Flink** 1.19 | jobs (FINISHED/FAILED/RUNNING) → execuções | **Real:** jobmanager + taskmanager em contêiner |
| **Luigi** / **Rundeck** / **MLflow** | tarefas, execuções e runs de experimento → execuções | Servidor HTTP local com o formato documentado |
| **MySQL** 8.4 | sondas, `EXPLAIN FORMAT=JSON`, diff (`<=>`), sessão `READ ONLY` | **Real:** `mysql:8.4` em contêiner |
| **SQL Server / Azure SQL** | `describe`, diff (`INTERSECT`), colchetes | **Só conexão falsa** |
| **AWS Glue / Step Functions / Athena** (boto3) | job runs, execuções, consultas (por workgroup); `AthenaWarehouse` (describe via Glue, EXPLAIN Trino) | **Emulado:** `moto` (API real do boto3, servidor emulado); AWS real não testado. Athena cai para `GetQueryExecution` sem `BatchGet…` |
| **Azure Data Factory** | `queryPipelineRuns` (últimas 24 h, com paginação) | Servidor HTTP local; **nunca contra o Azure** |
| **Glue Data Catalog / Unity Catalog** | retrato de schema por coleta; `CAT001` compara com o anterior (coluna removida/retipada, tabela sumida) + impacto | Glue: `moto`; Unity: servidor HTTP local. Hive Metastore (Thrift) **não implementado** |
| **LLM: Anthropic / OpenAI / Gemini** | sugerir edições de correção (`fix-ai`, botão "Sugerir com IA") | Formato verificado em servidor local; **nunca com chave real** |
| **LLM local (Ollama)** | idem, sem sair da máquina | **Real:** Ollama + `qwen2.5:3b` (listar modelos, chat, pipeline completo) |
| **Jev (TypeSafe)** | triagem de achados e porta de risco (decisões tipadas) | Servidor simulado no formato documentado (OpenRouter `/api/alpha/decisions`; SDK `typesafe-sdk`) |
| **Prefect** (REST) | execuções e estado | **Servidor real** local |
| **Dagster** (GraphQL) | execuções e estado | **Servidor real** local |
| **Kestra** | execuções, flows; lint de flows | **Servidor real** em contêiner (`INTENTFLOW_KESTRA_URL`) |
| **OpenLineage** (RunEvent 1.x) | execuções, entradas/saídas, linhagem de coluna; receptor HTTP `/api/v1/lineage` | Eventos conforme a especificação; **Airflow e Spark 3.5.3 reais** (`openlineage-spark` 1.24.2, aresta de coluna recebida) apontados para o receptor |
| **Jobs sem SQL** (Spark, Python, notebooks) | leituras de coluna/dataset entram no impacto (`lineage --impact`, UI) e nos checks `OL001` (coluna alterada), `OL002` (nome ambíguo, com sugestão de alias; `external_warn_unmatched` na política inclui os ausentes), `OL003` (PII lida; `pii_allowed_jobs` libera) e `OL004` (`appName` padrão do Spark). Fontes: OpenLineage ingerido ou `intentflow.lineage.json` (`{"aliases": {...}, "jobs":[{"name","owner","reads":[{"dataset","columns","feeds":[{"dataset","columns"}]}],"writes"}]}`; sem `columns` = dataset inteiro). **Job que é SQL** traz `"sql"`/`"sql_file"` (+ `dialect`, `writes`, `params`): leituras e `feeds` saem da linhagem de coluna do SQLGlot, incluindo colunas só de JOIN/WHERE/GROUP BY (indiretas, alimentam a saída inteira); aceita scripts com vários comandos (`INSERT ... SELECT`, `CREATE TABLE AS`) e resolve `${v}`, `$v`, `{v}`, `{{ v }}`, `{{ ref() }}`, `{{ source() }}` (sem valor vira `__param_v__` e ainda casa pelo sufixo); `select *` sem esquema cai para leitura do dataset inteiro. **Job Python** traz `"py_file"`: análise estática (AST, sem executar) de PySpark/pandas: segue variáveis, encadeamentos, funções do arquivo e de módulos irmãos importados (dentro do diretório da declaração), SQL montado por concatenação/f-string/`format`, `select/withColumn/withColumnRenamed/drop/filter/join/groupBy.agg` e pandas (`df["c"] = ...`, `merge`, `rename`); filtro e junção viram dependência indireta; o que não entende (`mapInPandas`, `apply`, UDF opaca) marca a saída como conservadora, nunca some Eventos do dbt e do próprio emissor do IntentFlow não viram jobs externos (o projeto já os tem; `openlineage.keep_project_lineage` desliga). Ações do Spark são agrupadas pela aplicação (`parent` facet; outros motores: `openlineage.group_by_parent`). A mudança propaga pelas saídas dos jobs **por coluna** (com `columnLineage`, `feeds` ou SQL; saída que é nó do projeto expõe só os consumidores daquela coluna) e de forma conservadora sem isso; `lineage --coverage` lista os jobs nesse caso | **Real**: Spark (`tests/test_spark_real.py`, job `spark` no CI em `local-cluster`) e `dbt-ol` (`tests/test_dbt_ol_real.py`, job `dbt-ol`). Databricks/EMR/Kubernetes, Flink e Dagster **não testados**. Casamento pelo sufixo mais específico (3/2/1 últimos segmentos de um caminho); `lineage --unmatched` lista o que não casou e sugere; `lineage --alias DATASET=MODELO` grava o alias. PySpark 3.5 não serializa objetos Python no 3.14 |
| **Docker / host** | memória, `OOMKilled` | Host real; Docker com saída simulada |
| **Webhook estilo Slack** | saídas e rotas por dono | **Real no Mattermost** (payload aceito e renderizado); Slack de verdade não testado |
| **E-mail (SMTP)** | STARTTLS oportunista, `ssl`, login `SMTP_USER`/`SMTP_PASSWORD`, verificação de certificado | **Real no Mailpit** (texto puro, STARTTLS com certificado autoassinado, login) |
| **Comentário de PR (GitHub)** | saída por PR | Servidor HTTP local; GitHub real não testado |
| **dbt + dialetos Snowflake/BigQuery/Databricks** (linhagem) | `QUALIFY`, `:`/`::`, `UNNEST`, `SAFE_CAST`, `explode`, nomes com crase | SQL compilado de fixture; **nunca contra conta real** |
| **Snowflake** | `describe`, plano (`EXPLAIN USING JSON`), diff, spill via `account_usage.query_history` | Conexão falsa; **nunca conta real** |
| **BigQuery** | `describe`, plano (dry run), diff de dados; falhas `resourcesExceeded` | **Emulador real** (`goccy/bigquery-emulator`) para `describe`/plano/diff; `INFORMATION_SCHEMA.JOBS` só simulado; **nunca conta real** |
| **Databricks** (SQL) | `describe`, plano (`EXPLAIN COST`), diff, spill via `system.query.history` | Conexão falsa; **nunca conta real** |

Credenciais, sempre por ambiente: `AIRFLOW_USER`/`AIRFLOW_PASSWORD` ou `AIRFLOW_TOKEN`, `KESTRA_USER`/`KESTRA_PASSWORD`,
`INTENTFLOW_PG_URL`, `AIRBYTE_TOKEN`, `FIVETRAN_API_KEY`/`FIVETRAN_API_SECRET`, `DATABRICKS_TOKEN`, `KAFKA_USER`/`KAFKA_PASSWORD`, `SCHEMA_REGISTRY_USER`/`SCHEMA_REGISTRY_PASSWORD`, `OPENLINEAGE_API_KEY`, `TRINO_*`, `CLICKHOUSE_*`, `REDSHIFT_URL`, `ARGO_TOKEN`/`ARGO_NAMESPACE`, `MAGE_API_KEY`/`MAGE_USER`/`MAGE_PASSWORD`/`MAGE_TOKEN`, `DBT_CLOUD_TOKEN`, `GITLAB_TOKEN`, `BITBUCKET_TOKEN`, `AZURE_DEVOPS_PAT`, `PAGERDUTY_ROUTING_KEY`, `OPSGENIE_API_KEY`, `JIRA_USER`/`JIRA_TOKEN`, `TEAMS_URL`, `RUNDECK_TOKEN`, `MLFLOW_TRACKING_TOKEN`, `MYSQL_*`, `MSSQL_*`, `OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_HEADERS`, `SNOWFLAKE_*`, `BIGQUERY_PROJECT` + credenciais do Google, `DATABRICKS_*`, `GITHUB_TOKEN`, `SMTP_HOST`/`SMTP_PORT`,
`INTENTFLOW_UI_TOKEN`. Nada é gravado em disco; o armazém guarda só **nomes** de variáveis.

## Ligar cada integração

| Integração | Como |
|---|---|
| SQLMesh | `--target caminho/do/projeto` (ou `sqlmesh_export.json`); `INTENTFLOW_SQLMESH_PYTHON` aponta o Python que tem `sqlmesh` |
| GX / Soda | `intentflow config set dq.results 'pasta,arquivo.json'` |
| Airbyte / Fivetran / Spark / Databricks | `config set airbyte.url`, `fivetran.url` (`https://api.fivetran.com`), `spark.url`, `databricks.jobs_host`, ou `runs --airbyte … --spark …` |
| Kafka | `config set kafka.bootstrap host:9092`, `kafka.schema_registry http://…`, opcional `kafka.groups`; `pip install intentflow[kafka]`; limite: `policy kafka_lag_max` |
| Contratos | `config set contracts.dir pasta` |
| Warehouse | `config set warehouse.type trino\|clickhouse\|redshift\|snowflake\|bigquery\|databricks\|postgres\|duckdb` + variáveis de ambiente do motor (sem `type`: vale `warehouse.duckdb`/`warehouse.postgres_env`) |
| Argo / Mage | `config set argo.url`, `argo.namespace`, `mage.url` |
| dbt Cloud | `config set dbt_cloud.account_id N`, `dbt_cloud.job_id N`, `dbt_cloud.host`; `DBT_CLOUD_TOKEN`; `intentflow dbt-cloud runs|pull` |
| Kubernetes / Prometheus | `config set kubernetes.enabled true` (+ `kubernetes.context`, `kubernetes.namespace`); `config set prometheus.url http://prom:9090` (`prometheus.queries` aceita consultas próprias em JSON) |
| Lint de SQL | `pip install intentflow[lint]`; `config set lint.enabled true` (desligado por padrão: é lento); regras por `policy.lint_rules` |
| Elementary | `config set elementary.schema analytics_elementary` (usa o warehouse configurado) |
| Delta / Iceberg | `pip install intentflow[lakehouse]`; `config set lakehouse.tables 'pasta_delta,metadata.json'`; limites `policy.small_file_count`/`small_file_bytes` |
| Mais execuções | `config set kafka_connect.url`, `flink.url`, `luigi.url`, `mlflow.url`, `rundeck.url` (+ `rundeck.project`) |
| Alertas | sinks `pagerduty`, `opsgenie`, `jira`, `teams`, `gitlab`, `bitbucket`, `azure_devops` em `notify.sinks` (ver [DEPLOYMENT](DEPLOYMENT.md)) |
| Métricas | `GET /metrics` (Prometheus, com `Authorization: Bearer`) · `intentflow otel-push --endpoint http://collector:4318` |
| Warehouses | `warehouse.type`: `mysql`, `sqlserver`, `athena`, além dos anteriores |
| AWS | `pip install intentflow[aws]`; `config set aws.region us-east-1`, `aws.glue true`, `aws.stepfunctions true`, `aws.athena true` (credenciais pela cadeia padrão do boto3) |
| Azure Data Factory | `config set azure.data_factory SUB/RG/FABRICA`; `AZURE_TOKEN` |
| Catálogos | `config set catalog.glue true` e/ou `catalog.unity HOST` (+ `catalog.include`); a segunda coleta em diante já compara |
| Catálogo | `intentflow openlineage emit --url http://catalogo --target …` (`--dry-run` mostra os eventos) |

Para ligar os que só existem em nuvem, veja [CLOUD_SETUP.md](CLOUD_SETUP.md).

Não entraram: Evidently/whylogs/Pandera, Unity Catalog/Glue/Hive Metastore, Athena, Oracle, Dataform/Coalesce, BI nativo (Looker/Tableau/Power BI) — sem demanda clara ou sem ambiente para verificar.

Meltano não entrou: não há API de servidor para ler execuções (o estado fica no banco de sistema do projeto).

## Notas de segurança e operação das integrações novas

- **SQLMesh executa código do projeto** (`config.py`, modelos Python, macros) no interpretador de `INTENTFLOW_SQLMESH_PYTHON`. Por isso ler um projeto
  (diretório) **exige `INTENTFLOW_SQLMESH_ALLOW_EXEC=1`**; sem isso o erro indica `sqlmesh-export`. O snapshot JSON é só dado e carrega sem opt-in.
  Em CI de PR vindo de fork nunca habilite a variável: gere o JSON em ambiente isolado.
- **Credenciais** de todas as integrações vêm de variáveis de ambiente do processo; o armazém guarda apenas URLs e nomes de variáveis.
  O registro de projetos mascara senha de DSN na listagem, mas prefira `env:VAR`.
- **Conexões de warehouse** são abertas por verificação/coleta e fechadas ao fim (`Warehouse.close`). ClickHouse conecta com `readonly=2`
  (o servidor recusa escrita); Postgres/Redshift abrem a sessão `readonly`.
- **Airflow 3**: um `Asset` usado só como *inlet* fica inativo e a task falha (`AirflowInactiveAssetInInletOrOutletException`); alguém
  precisa produzi-lo (outlet). O token JWT da API v2 é pedido por `POST /auth/token` e renovado a cada 30 min.
- **Exposures do dbt** (dashboards) entram no grafo: o impacto de modelo/coluna lista quais dashboards são atingidos e o dono declarado.

## Fora do escopo atual

SQLMesh, Redshift, Fabric, Spark direto, e qualquer escrita em warehouse. Correção por LLM.

## Subir o Airflow com OpenLineage para o receptor (local)

```bash
intentflow openlineage serve --port 5055 &
docker run -d --network host \
  -e AIRFLOW__WEBSERVER__WEB_SERVER_PORT=58082 \
  -e AIRFLOW__OPENLINEAGE__TRANSPORT='{"type":"http","url":"http://127.0.0.1:5055","endpoint":"api/v1/lineage"}' \
  apache/airflow:2.10.5 standalone
```

Observação verificada: `BashOperator` com `inlets/outlets` do tipo `airflow.datasets.Dataset` **não** emite datasets; com
`airflow.lineage.entities.Table` emite (namespace do cluster, nome `banco.tabela`).
