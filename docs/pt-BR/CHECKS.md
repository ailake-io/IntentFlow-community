# Catálogo de checks

[English](../CHECKS.md)

Estados: **testado** = há teste automatizado que exercita o check com um caso positivo (e, onde relevante, negativo).

## dbt e esquema

| Id | O que detecta | Gatilho | Custo | Estado |
|---|---|---|---|---|
| SCH001 | Coluna removida/renomeada com consumidores a jusante; sugere renomeação e gera correção | pre-merge | static | testado (dbt real) |
| OL001 | Job fora do SQL (Spark/Python/notebook, via OpenLineage ou `intentflow.lineage.json`) lê coluna removida/alterada, direta ou derivada | pre-merge | static | external |
| OL002 | Dataset lido por job externo casa com mais de um modelo (impacto some em silêncio) ou, com `external_warn_unmatched`, com nenhum; resolva com `aliases` | pre-merge | static | external |
| OL003 | Job externo lê coluna PII (ou derivada sem máscara); `pii_allowed_jobs` na política libera | pre-merge | static | external |
| OL004 | Job Spark com nome de aplicação padrão (`pyspark_shell`): jobs distintos se misturam | pre-merge | static | external |
| SCH002 | Mudança de tipo de coluna, com consumidores | pre-merge | static | testado |
| DBT001 | Modelo consumido sem teste `unique` | pre-merge | static | testado |
| DBT002 | Incremental sem `unique_key` (não idempotente). Correção por regra: a pessoa informa a chave (a UI sugere colunas `id`/`*_id`); valida formato e existência da coluna | pre-merge | static | testado |
| DBT003 | Modelo sem dono (`meta.owner`). Correção por regra: a pessoa informa o dono | continuous | static | testado |
| DBT004 | PII propagada sem hash/máscara a modelo não autorizado | pre-merge | static | testado |
| DBT005 | Modelo com ≥ 3 consumidores sem `contract.enforced` | pre-merge | static | testado |
| DBTR001 | Teste do dbt falhando/em aviso (do `run_results.json`) | continuous | statistical | testado (falha real do dbt) |
| DBTR002/003 | Modelo com erro de execução; modelos pulados por falha a montante | continuous | statistical | testado |
| DBTR004 | Modelo > 2× a mediana histórica de tempo | continuous | statistical | testado |

## SQL (AST do SQLGlot sobre o SQL compilado)

| Id | O que detecta | Estado |
|---|---|---|
| SQL001 | `SELECT *` | testado |
| SQL002 | JOIN sem condição (produto cartesiano, incl. vírgula) | testado |
| SQL003 | `UNION` sem `ALL` | testado |
| SQL004 | `ORDER BY` em subconsulta/CTE sem `LIMIT` | testado |
| SQL005 | Função aplicada à coluna em filtro/join (não sargável) | testado |
| SQL006 | `DISTINCT` combinado com `JOIN` | testado |
| SQL007 | Subconsulta correlacionada | testado |
| SQL008 | `LIKE '%…'` | testado |
| SQL009 | `NOT IN (subconsulta)` | testado |
| SQL010 | `IN` com mais de 100 literais | testado |
| SQL011 | `OR` na condição de JOIN | testado |
| SQL012 | Reescrita do otimizador; **equivalência verificada executando as duas versões** (descarta se diferir) | testado |
| SQL013 | Plano caro via `EXPLAIN` (produto cartesiano, nested loop, linhas estimadas acima da política) | testado (DuckDB); PostgreSQL via `plan()` |
| SQL014 | Plano piorou no PR (novo operador pesado ou custo > 2×) comparando base e nova | testado (DuckDB) |
| SQL015 | Chave de junção com tipos de famílias diferentes (número × texto etc.) | testado |

Todas as regras SQL001–SQL013 têm modelo plantado em `examples/dbt_lab` e teste que as dispara, mais verificação de ausência de
falso positivo em modelos limpos. SQL001 e SCH001 geram correção determinística (`expand_star`, `rename_column`).

## Orquestração (análise estática)

| Id | Plataforma | Detecta | Estado |
|---|---|---|---|
| AF001/AF002 | Airflow | sem `retries`; sem timeout | testado |
| AF003/AF004 | Airflow | `catchup` ativo com `start_date` antigo; sem `max_active_runs` | testado |
| AF005 | Airflow | sem notificação de falha | testado |
| AF006 | Airflow | `schedule_interval`, imports descontinuados | testado |
| AF007 | Airflow | `Variable.get` no nível do módulo | testado |
| LINT001 | SQLFluff | regras de correção/desempenho (`SELECT *`, `UNION` sem `ALL`, comparação com NULL, coluna ambígua, CTE sem uso…) sobre o SQL compilado; opcional | testado |
| CAT001 | Catálogos (Glue/Unity) | tabela sumiu, coluna removida/retipada entre duas coletas; severidade sobe com consumidores no projeto | testado (Glue em emulador, Unity simulado) |
| ELM001 | Elementary | teste ou anomalia (volume, frescor, distribuição) falhando, com impacto a jusante | testado com tabela no esquema documentado |
| LAKE001 | Delta/Iceberg | muitos arquivos pequenos; schema que remove/muda tipo entre versões | testado com tabelas reais |
| DQ001 | GX / Soda | verificação de qualidade falhou/avisou, com impacto a jusante e severidade da ferramenta | testado com saídas reais |
| CTR001 | Contratos | modelo/coluna do contrato inexistente, tipo divergente, unicidade/obrigatoriedade sem teste | testado |
| KAF001 | Kafka | lag de consumidor alto **e** crescendo | testado com Redpanda real |
| KAF002 | Kafka | versão nova do schema remove/muda tipo de campo; crítico se o tópico alimenta modelos | testado com Redpanda real |
| AF301–AF311 | Airflow 2 → 3 | imports movidos (`airflow.sdk`, provider `standard`), `schedule_interval`, `execution_date`, `provide_context`, `SubDagOperator`, `days_ago`, `catchup` implícito, acesso ao banco de metadados, `Dataset`/`Table` → `Asset`, REST `/api/v1`. **Opt-in:** `intentflow policy airflow-target 3`. Comando: `intentflow airflow3 PASTA [--write]` (reescreve só imports exatos, guarda `.af2.bak`) | testado; DAGs reescritos idênticos aos que rodaram no Airflow 3.1 real |
| KS001–KS004 | Kestra | sem `errors`; I/O sem retry; sem timeout; agendado sem `concurrency` | testados |
| KS005 | Kestra | segredo em texto claro | testado |
| KS006 | Kestra | backfill desde data antiga | testado |

## Sondas no warehouse

| Id | Detecta | Estado |
|---|---|---|
| PRB001 | Duplicidade em coluna com teste `unique` | testado (positivo e negativo; DuckDB, PostgreSQL) |
| PRB002 | Volume fora de 0,5×–2× da mediana histórica | testado |
| PRB003 | Taxa de nulos subiu ≥ 20 p.p. e ≥ 2× o habitual | testado (DuckDB) |
| PRB004 | Fan-out: junção por chave não única multiplica linhas | testado (DuckDB, projeto laboratório) |

## Cluster e execuções

| Id | Detecta | Estado |
|---|---|---|
| CLU001 | Memória ≥ 70% do limite | testado |
| CLU002 | Esgotamento previsto em < 24 h (regressão linear) | testado |
| CLU003 | Processo/container morto por OOM | testado |
| CLU004 | Consultas com spill em disco | testado |
| CLU005 | Pool/worker saturado de forma sustentada | testado |
| RUN001 | Três falhas seguidas | testado |
| RUN002 | Duração > 2× a mediana | testado |
| RUN003 | SLA em risco (p90 > 80% do prazo) ou violado (`policy sla`) | testado |
| RUN004 | Sem sucesso há mais que `max_age_hours` | testado |

## Falsos positivos

Cada achado pode ser marcado como falso positivo (não reabre). `intentflow stats`/UI mostram a taxa por check e sugerem desligar
quando ≥ 50% com ≥ 5 decisões (`policy disable-check`). Novos checks devem rodar sem bloquear até ter taxa conhecida.
