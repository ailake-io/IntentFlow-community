# Roteiro e critérios de decisão

[English](../ROADMAP.md)

## Feito e verificado localmente

Linhagem e impacto (UI e API), `run_results.json` do dbt, desempenho (cache, 1.000 modelos em 24 s a frio), diff de dados na UI,
SLA/frescor/checks por UI, 13 novos checks, Airflow/Kestra/Prefect/Dagster reais, OpenLineage real do Airflow, dialetos de nuvem
(fixture), concorrência do armazém, empacotamento, acessibilidade básica.

CI em `.github/workflows/ci.yml` (unit em Python 3.11/3.13 + integração com PostgreSQL e Mailpit), **verde** em `ailake-io/IntentFlow-community`. Escala sintética: 5.000 modelos em 11 s (4 processos), 0,6 s com cache.

Rodada de projetos reais (ver [BENCH_REAL_PROJECT](BENCH_REAL_PROJECT.md)): 2 projetos dbt públicos (ra_data_warehouse, dbt-project-evaluator), 7 bugs de linhagem/checks corrigidos, oráculo dinâmico (recall de impacto 100 % no nível de modelo), linhagem 19× mais rápida e paralela, `data diff` com amostragem, `--sample-rows` no oráculo, linhagem incremental por PR, IA assíncrona na UI com sugestão do dono (DBT003). Ver [CHECKS](CHECKS.md) e [INTEGRATIONS](INTEGRATIONS.md).

## O que falta e precisa de você

Como ligar cada serviço de nuvem: [CLOUD_SETUP](CLOUD_SETUP.md).

| Item | Bloqueio |
|---|---|
| Medir precisão da linhagem (hoje só limite inferior ~40 %) e utilidade dos achados num pipeline **de produção** | Projeto real de alguém com `catalog.json` |
| Snowflake, Databricks (SQL e Jobs), Redshift, Fivetran reais | Contas e credenciais do usuário (hoje só simulados) |
| BigQuery real (`INFORMATION_SCHEMA.JOBS`, custo) | Projeto GCP; emulador cobre leitura/plano/diff |
| Airbyte Cloud, Slack, GitHub, DataHub/OpenMetadata reais | Contas/tokens; Airbyte OSS, Marquez, Mattermost e SMTP já verificados |
| dbt Cloud, PagerDuty, Opsgenie, Jira, Teams, GitLab, Bitbucket, Azure DevOps | Conta/token de teste (implementados; só servidor simulado) |
| AWS (Glue, Step Functions, Athena, catálogo) e Azure Data Factory | Conta e papel somente leitura (implementados; só emulador/simulado) |
| Correção assistida por LLM | Modelo e dados autorizados; só vale se superar a regra em casos reais |

## O que falta e é decisão de produto

Já resolvido: usuários, papéis e MFA; SSO/OIDC e papéis por projeto (Enterprise); HTTPS próprio (`--tls-cert`) ou atrás de proxy
([DEPLOYMENT](DEPLOYMENT.md)); licença open core com o pacote Enterprise separado ([EDITIONS](EDITIONS.md)); inglês como idioma
principal, com português.

Em aberto: onde roda por padrão (notebook × servidor da equipe × nuvem hospedada); publicar o núcleo a partir de um histórico novo.

## Ainda não implementado (sem bloqueio externo)

Meltano; Evidently/whylogs/Pandera; Hive Metastore; renovação automática do token do Azure; Oracle; Redshift Serverless (`sys_query_history`); chave-par/PAT do Snowflake; mais correções automáticas por regra (hoje: renomear coluna, expandir `SELECT *`, dono do modelo, `unique_key` de incremental; faltam YAML: descrição/teste);
revisão de acessibilidade com leitor de tela.

## Como saber se vale continuar

| Medida | Limiar |
|---|---|
| Colunas com linhagem correta em projetos reais | ≥ 85% |
| Incidentes/mês por mudança de upstream no contexto do usuário | ≥ 1 |
| Taxa de falso positivo por check (UI/`intentflow stats`) | < 50% para manter o portão de CI ligado |
| Tempo de diagnóstico economizado em incidente real | mensurável |
