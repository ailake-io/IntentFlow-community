# IntentFlow

[English](README.md)

**Um lugar só para os problemas de pipelines de dados — e para pegá-los antes de chegarem à produção.**

IntentFlow **não é um motor de execução** e **não usa arquivos YAML próprios**: configuração e políticas ficam no armazém
local e são geridas pela **CLI** ou pela **UI**. Ele fica ao lado do que você já usa (dbt, SQLMesh, Airflow, Kestra, Spark,
OpenLineage, warehouses) e:

- lê metadados e código (somente leitura por padrão);
- roda *checks* preventivos no PR, antes do job e continuamente;
- agrupa os achados em incidentes por causa raiz e roteia por dono (Slack, webhook, e-mail, comentário de PR);
- mostra numa tela o que está rodando, falhando, em risco e sendo corrigido;
- traça a linhagem de coluna, inclusive de jobs Spark/Python/SQL fora do projeto, e mostra o que quebra se uma coluna mudar.

O núcleo é open source (Apache-2.0). SSO, governança por projeto, correções governadas, IA e integrações corporativas fazem parte do
**IntentFlow Enterprise**: [EDITIONS](docs/pt-BR/EDITIONS.md).

## Problemas cobertos

| # | Dor | Como é prevenida | Verificação |
|---|---|---|---|
| 1 | Upstream muda e quebra consumidores (coluna removida/renomeada, tipo) | diff de catálogo + linhagem de coluna → impacto | dbt real (DuckDB e PostgreSQL) |
| 2 | "O que quebra se eu mudar isto?" | linhagem de coluna (SQLGlot) | gabarito: 10 modelos difíceis, precisão e recall 100% |
| 3 | SQL mal escrita, cara, que estoura memória | 12 regras de AST + custo via `EXPLAIN` + reescrita **verificada por execução** | cada regra dispara em modelo plantado |
| 4 | Duplicidade de chave, volume anormal | sondas no warehouse | DuckDB e PostgreSQL reais |
| 5 | Mudança de dados indesejada num PR | **diff de dados** (linhas, chaves, colunas alteradas, amostra) no warehouse | DuckDB e PostgreSQL reais |
| 6 | Falha de orquestração previsível (sem retry/timeout, backfill, segredo no YAML do flow) | lint de DAGs Airflow e flows Kestra | arquivos de exemplo |
| 7 | Memória do cluster: OOM, esgotamento, spill, pool saturado | amostras host/Docker/Airflow + previsão + spill do warehouse | Docker simulado, Airflow real; nuvem só com conexões falsas |
| 8 | Falhas recorrentes, regressão de duração, SLA e atraso | histórico de execuções + políticas | Airflow e Kestra **reais** |
| 9 | PII sem máscara a jusante | propagação pela linhagem | dbt real |
| 10 | Ruído de alertas | incidentes por causa raiz, anti-spam, `ack` com validade, falso positivo, taxa por check | testado |
| 11 | "O que pode quebrar no processo todo?" | `intentflow risks`: lacunas de visibilidade, colunas frágeis, jobs externos, processo | [RISKS](docs/pt-BR/RISKS.md) |

Catálogo completo: [docs/CHECKS.md](docs/pt-BR/CHECKS.md).

## Uso (CLI)

```bash
# a partir do wheel de uma release (https://github.com/ailake-io/IntentFlow-community/releases) ou do código-fonte:
python -m venv .venv && .venv/bin/pip install -e '.[dev,secrets]'

# configuração única (no armazém; credenciais de conexão por variável de ambiente)
intentflow config set dbt.target projeto/target
intentflow config set airflow.url http://airflow:8080
intentflow config set kestra.url  http://kestra:8080
intentflow policy pii-allow customers            # políticas
intentflow policy sla airflow:etl 30             # SLA em minutos
intentflow notify add-sink dados --type slack --url-env SLACK_URL
intentflow notify route dados --min-severity high

# dia a dia
intentflow check --format github --fail-on high   # CI
intentflow gate --stage pre-run                    # portão antes do dbt (Airflow/Kestra)
intentflow diff --model orders --base-schema prod --head-schema dev --key order_id --duckdb wh.duckdb
intentflow diff --base prod.orders --head dev.orders --key id --sample-pct 5   # tabela enorme: ~5 % das chaves (hash), com estimativa do total
intentflow lineage --column orders.status          # o que esta coluna afeta (direto e indireto)
intentflow watch                                   # agendador: coleta, verifica, notifica
intentflow ui                                      # http://127.0.0.1:8787 (modo local; ver docs/pt-BR/DEPLOYMENT.md para equipe)
intentflow risks --base main/target               # o que pode quebrar no processo todo
intentflow user add maria --role admin            # liga o modo equipe: login por token, papéis, MFA, trilha por pessoa

# entrada OpenLineage (Airflow, Spark, dbt, Flink, Dagster…)
intentflow openlineage serve --port 5000
```

### Idioma

CLI, UI e achados saem em **inglês** ou **português**. A CLI segue `INTENTFLOW_LANG` (`en`/`pt`) ou o locale do sistema
(`LANG=pt_BR.UTF-8`). A UI segue o idioma do navegador e tem um seletor no rodapé da barra lateral. O id dos achados não depende do
idioma: trocar de idioma não reabre achados.

Na **UI** (`intentflow ui`, tema claro/escuro automático): visão geral com indicadores, incidentes, achados (filtros, detalhes com
recomendação, reconhecer, falso positivo), linhagem e impacto, diff de dados, pipelines, cluster/memória, integrações, configuração por
formulário, equipe, auditoria e tela de boas-vindas guiada. Com Enterprise: correções com passo a passo (proposta → aprovada → validada →
aplicada), sugestões por IA, SSO e papéis por projeto.

## Como medir a linhagem no SEU projeto

```bash
dbt build && dbt docs generate
intentflow lineage-verify --target target --duckdb <arquivo.duckdb> --package <seu_pacote>
```
Perturba os dados de origem, reconstrói os modelos e compara com a linhagem estática (precisão, recall, o que a dependência indireta explica).
Em base grande: `--sample-rows 1000` (reduz cada origem na **cópia** temporária, por hash da chave) e `--source NOME[.coluna]` (limita as origens).
Metodologia e números no projeto jaffle real: [BENCHMARKS](docs/pt-BR/BENCHMARKS.md).

## O que foi verificado contra sistemas reais (local)

dbt 1.12 com DuckDB **e** PostgreSQL 18 · SQLMesh 0.236 · Airflow 2.10 e **3.1** (APIs v1/v2, com emissor OpenLineage real) · Kestra · Prefect · Dagster · Spark 3.5 · Kafka/Schema Registry (Redpanda) · Trino · ClickHouse · MySQL · Flink · Kubernetes · Prometheus · OpenTelemetry · SQLFluff · Delta/Iceberg · Airbyte · BigQuery (emulador) · Argo Workflows · Mage · Marquez · Mailpit · Mattermost · saídas reais do Great Expectations 1.24 e do Soda 3.5 · Keycloak 26 · PostgreSQL 18 como armazém · Docker/host · servidores HTTP/SMTP locais para saídas.

## Limites que você precisa saber

- **Linhagem:** gabarito de 10 modelos difíceis (janelas, unnest, union, pivot, macro, self-join, `exclude`), dois projetos de exemplo e fixtures de
  Snowflake/BigQuery/Databricks. Medida com oráculo dinâmico em projetos dbt reais públicos (jaffle-shop, dbt-project-evaluator, ra_data_warehouse):
  **o impacto nunca subestimou** (recall 100 % no nível de modelo); a **precisão** é só limite inferior (~40 %). Nenhum pipeline de produção de terceiros
  foi medido ainda: [BENCH_REAL_PROJECT](docs/pt-BR/BENCH_REAL_PROJECT.md) e o roteiro [PILOT](docs/pt-BR/PILOT.md).
- **Escala (medido):** linhagem sintética de 5.000 modelos / 56 mil colunas em ~11 s a frio (4 processos) e 0,6 s com cache; em projeto real espere ~3× mais
  por modelo. Em processo com threads (servidor web) a 1ª execução é serial. `INTENTFLOW_WORKERS` ajusta os processos.
- **Segurança de carga:** ler um projeto SQLMesh executa o código dele; exige `INTENTFLOW_SQLMESH_ALLOW_EXEC=1` ou o JSON de `sqlmesh-export`.
  SSO: o IdP em rede privada exige `INTENTFLOW_SSO_ALLOW_INTERNAL=1`; destinos internos são bloqueados na conexão.
- **CI:** [`.github/workflows/ci.yml`](.github/workflows/ci.yml) roda a suíte (Python 3.11 e 3.13) e a integração com PostgreSQL e Mailpit. Os demais testes de integração
  (Airflow, Kestra, Trino, Keycloak…) ficam pulados sem os serviços.
- **Nuvem:** Snowflake, Databricks (SQL e Jobs), Redshift e Fivetran foram testados **só com respostas simuladas**; BigQuery, com o emulador (sem `INFORMATION_SCHEMA.JOBS`). Airflow 3.1 (API v2 com JWT) foi verificado em contêiner real, com um migrador 2 → 3 (`intentflow airflow3`). dbt Cloud, PagerDuty/Opsgenie/Jira/Teams, GitLab/Bitbucket/Azure DevOps, Airbyte Cloud: implementados, só com servidor simulado. AWS (Glue, Step Functions, Athena, Glue Catalog) foi verificado só no emulador `moto`; Azure Data Factory e Unity Catalog, só simulados. Como ligar cada um: [CLOUD_SETUP](docs/pt-BR/CLOUD_SETUP.md).
- **Correções (Enterprise):** regras para renomear colunas, expandir `SELECT *`, definir dono (DBT003) e `unique_key` (DBT002) e sugestões por LLM, sempre com aprovação humana e validação em contêiner sem rede.
- **Falsos positivos:** a taxa por check é medida a partir do seu feedback; ainda não há dado real de uso.
- **Jobs externos:** a análise de Python é heurística (não executa o código); código dinâmico, UDFs e `mapInPandas` deixam a saída conservadora.
- **Traduções:** os achados ficam gravados no idioma do processo que rodou a verificação.
- **Reescrita do otimizador:** verificada por execução apenas quando há warehouse; sem ele é só sugestão.

## Validação isolada e recuperação

Correções governadas e a validação isolada fazem parte do IntentFlow Enterprise; a documentação delas fica no pacote Enterprise.

## Revisão de segurança, confiabilidade e desempenho

Correções, regressões, medições reproduzíveis e limites restantes: [revisão de 13/10/2026](docs/pt-BR/REVIEW_2026-10-13.md).
Os resultados históricos de integrações acima não significam que todos os serviços foram exercitados nesta revisão.

## Documentação

[PILOT](docs/pt-BR/PILOT.md) (roteiro para testar num pipeline real) · [RISKS](docs/pt-BR/RISKS.md) (o que pode quebrar no processo todo; `intentflow risks`) · [DEPLOYMENT](docs/pt-BR/DEPLOYMENT.md) (equipe, papéis, TLS) · [ARCHITECTURE](docs/pt-BR/ARCHITECTURE.md) · [CHECKS](docs/pt-BR/CHECKS.md) · [INTEGRATIONS](docs/pt-BR/INTEGRATIONS.md) · [BENCHMARKS](docs/pt-BR/BENCHMARKS.md) · [BENCH_REAL_PROJECT](docs/pt-BR/BENCH_REAL_PROJECT.md) · [ROADMAP](docs/pt-BR/ROADMAP.md) · [DECISIONS](docs/pt-BR/DECISIONS.md)

## Licença

Núcleo open source sob [Apache-2.0](LICENSE). SSO, governança por projeto, correções governadas, IA e integrações corporativas
formam a edição comercial: [EDITIONS](docs/pt-BR/EDITIONS.md). Copyright (c) 2026 Thiago Egon Lange, veja [NOTICE](NOTICE). [CONTRIBUTING](CONTRIBUTING.md) · [SECURITY](SECURITY.md) · [CHANGELOG](CHANGELOG.md)
