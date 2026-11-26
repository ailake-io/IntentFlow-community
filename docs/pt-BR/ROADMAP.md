# Roteiro e critérios de decisão

[English](../ROADMAP.md)

## Feito e verificado localmente

Linhagem e impacto (UI e API), `run_results.json` do dbt, desempenho (cache, 1.000 modelos em 24 s a frio), diff de dados na UI,
SLA/frescor/checks por UI, 13 novos checks, Airflow/Kestra/Prefect/Dagster reais, OpenLineage real do Airflow, dialetos de nuvem
(fixture), concorrência do armazém, empacotamento, acessibilidade básica.

CI em `.github/workflows/ci.yml` (unit em Python 3.11/3.13/3.14 + integração com PostgreSQL e Mailpit), **verde** em `ailake-io/IntentFlow-community`. Escala sintética: 5.000 modelos em 11 s (4 processos), 0,6 s com cache.

Rodada de projetos reais (ver [BENCH_REAL_PROJECT](BENCH_REAL_PROJECT.md)): 2 projetos dbt públicos (ra_data_warehouse, dbt-project-evaluator), 7 bugs de linhagem/checks corrigidos, oráculo dinâmico (recall de impacto 100 % no nível de modelo), linhagem 19× mais rápida e paralela, `data diff` com amostragem, `--sample-rows` no oráculo, linhagem incremental por PR, IA assíncrona na UI com sugestão do dono (DBT003). Ver [CHECKS](CHECKS.md) e [INTEGRATIONS](INTEGRATIONS.md).

## O que falta e precisa de você

Como ligar cada serviço de nuvem: [CLOUD_SETUP](CLOUD_SETUP.md).

| Item | Bloqueio |
|---|---|
| Medir precisão da linhagem (hoje só limite inferior ~40 %) e utilidade dos achados num pipeline **de produção** | Projeto real de alguém com `catalog.json` |
| Snowflake, Databricks (SQL e Jobs), Redshift, Fivetran reais | Contas e credenciais do usuário (hoje só simulados) |
| BigQuery real (`INFORMATION_SCHEMA.JOBS`, custo) | Projeto GCP; emulador cobre leitura/plano/diff |
| Airbyte Cloud, Slack, GitHub, DataHub/OpenMetadata reais | Contas/tokens; Airbyte OSS, Marquez, Mattermost e SMTP já verificados |
| dbt Cloud, PagerDuty, Opsgenie, Jira, Teams, Bitbucket, Azure DevOps | Conta/token de teste (implementados; só servidor simulado) |
| AWS (Glue, Step Functions, Athena, catálogo) e Azure Data Factory | Conta e papel somente leitura (implementados; só emulador/simulado) |
| Correção assistida por LLM | Modelo e dados autorizados; só vale se superar a regra em casos reais |

GitLab foi exercitado em instância local autohospedada numa rodada anterior; ambientes hospedados de clientes continuam não verificados.

Trabalho técnico prioritário: tratar e reavaliar os CVEs Debian das imagens. O CI agora audita as dependências Python travadas (`pip-audit`) e reprova a imagem em achados CRITICAL com correção (trivy); os HIGH da base ainda pedem decisão. Veja [VALIDATION_STATUS](VALIDATION_STATUS.md).

DBTR003 agora cobre descendentes pulados após falha de teste, em uma correção Unreleased na develop.

## O que falta e é decisão de produto

Já resolvido: usuários, papéis e MFA; SSO/OIDC e papéis por projeto; HTTPS próprio (`--tls-cert`) ou atrás de proxy
([DEPLOYMENT](DEPLOYMENT.md)); pacote único Apache-2.0 (o Enterprise foi incorporado na 0.4.0); inglês como idioma
principal, com português.

Em aberto: onde roda por padrão (notebook × servidor da equipe × nuvem hospedada); manter releases e distribuição para clientes (o repositório público do núcleo já existe).

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

## Validação preventiva e regras de negócio (Unreleased)

Implementado na develop: evidência de build completo, conclusão/erros dos checks obrigatórios, revisão nos metadados nativos dbt, contratos obrigatórios estritos, catálogo/UI/histórico de regras e diff lógico entre ambientes. Guias: [PREFLIGHT](PREFLIGHT.md), [BUSINESS_RULES](BUSINESS_RULES.md).

Próximos, em ordem: vincular qualidade externa a snapshots dos dados; ampliar fixtures de incremental/backfill/eventos atrasados; impor deploy e ensaiar recuperação no warehouse real. Sem promessa de prever qualquer falha.

Frescor de fontes dbt e resultados GX/Soda atuais/mapeados também podem ser obrigatórios; veja PREFLIGHT.

Unreleased: a [revisão de mudanças](CHANGE_REVIEW.md) compara testes, schema/interfaces, contratos, chaves incrementais e políticas de frescor. O preflight exige revisão exata de controles alterados mesmo quando os testes restantes passam. Comportamento dinâmico e consumidores não registrados precisam de validação complementar.
