# Teste em projeto real grande (ra_data_warehouse)

[English](../BENCH_REAL_PROJECT.md)

Projeto: `rittmananalytics/ra_data_warehouse` (GitHub; dbt multi-fonte para BigQuery/Snowflake/Redshift, 284 arquivos
SQL, pacotes Fivetran embutidos). Compilado com dbt 1.12 + DuckDB, sem warehouse real (sem `catalog.json`, sem dados).

## O que foi preciso para o projeto sequer compilar (não é o IntentFlow)
dbt antigo (0.17): `source-paths`/`data-paths`, `dbt_utils.surrogate_key` e `type_*` removidos, `target.type` sem DuckDB,
teste duplicado no YAML, `ref()` para modelo inexistente (`wh_contacts_audiences_xa`), variáveis por fonte ausentes.
Resultado: 101 modelos habilitados, 142 fontes, 1 modelo removido (ref quebrada).

## Bugs do IntentFlow achados e corrigidos
| # | Sintoma | Correção |
|---|---|---|
| 1 | Linhagem só calculava colunas **declaradas** (163 de ~1.200): impacto atingia 31 de 655 relações | colunas de saída inferidas do SELECT compilado (`named_selects`) |
| 2 | Pai sem aresta de coluna (`select *` de fonte sem esquema, SQL gerado) sumia do grafo → impacto subestimado | dependência `opaque` de modelo; `indirect_of` considera `*` |
| 3 | Check SQL012 quebrava (`ParseError` ao reler SQL do otimizador) | erro isolado por modelo |
| 4 | SQL001: 98 avisos, metade em `select * from source` de passagem (tmp/import) sem esquema conhecido | ignora quando não há o que expandir; mantém quando o fix governado é possível (67 restam) |

## Números (depois das correções)
- `check` completo: ~23 s, 306 achados (DBT003 101, SQL001 67, DBT001 62, DBT005 53); linhagem 1.228 colunas, 90 % resolvidas.
- Impacto por modelo = dbt (655/655, nada subestimado).
- Impacto por **coluna**: média de 0,7 modelos atingidos contra 11,5 descendentes no dbt; 187 de 411 colunas não
  atingem nada. **Ainda não validado pelo oráculo** (sem dados neste projeto): tratar como limite inferior de precisão.

## Limites honestos
- Sem `catalog.json` a precisão depende do SQL declarar colunas; `select *` de fonte opaca cai para nível de modelo.
- SQL BigQuery foi lido com o dialeto do adaptador DuckDB; 27 colunas com `SqlglotError`.
- Os achados não foram classificados manualmente um a um (amostra de 12 pareceu coerente; SQL001/DBT001 são ruidosos).

## Segundo projeto: dbt-project-evaluator (executável em DuckDB)
O `ra_data_warehouse` não executa (SQL inválido no repositório), então não serve para o oráculo. O
`dbt-project-evaluator` (dbt-labs, integração) roda em DuckDB: 54 modelos + 28 seeds, 37 modelos verificáveis,
196 colunas de origem perturbadas (null / deslocar / inverter), ~16 min.

### Bugs achados pelo oráculo (corrigidos)
| # | Sintoma | Causa |
|---|---|---|
| 5 | 34 mudanças reais "perdidas" (`stg_nodes.is_enabled` → `fct_*_coverage`) | no sqlglot 30 `Scope.union_scopes` não existe (agora `set_operation_scopes`); a proveniência por `UNION ALL` ficava vazia, sem erro |
| 6 | oráculo recriava modelos-raiz vazios (`graph` fora do dbt) e apagava os dados | raízes existentes são preservadas como origem |
| 7 | `UPDATE` em view falhava calado e virava "falso positivo" | raízes viram tabela; perturbação que não se aplica é reportada |
| 8 | `now()` em `measured_at` virava "perdido" | duas construções sem perturbação: coluna instável é excluída |
| 9 | modelo cujo nº de linhas muda (filtro/join) não contava como afetado | passa a contar no nível de modelo |
| 10 | colunas 100 % NULL (perturbação é no-op) viravam "falso positivo" | marcadas como não testáveis |

### Resultado final
| | |
|---|---|
| Recall no nível de modelo (inclui dependência indireta) | **100 %** (0 perdidos de 554 modelos afetados) |
| Recall da linhagem direta, coluna a coluna | 37,5 % do que mudou; o resto (722) só por join/filtro/agrupamento |
| Precisão (limite inferior) | 43 % no modelo, 38 % por coluna |

A precisão é um **limite inferior**: "suspeito" é dependência que a linhagem aponta mas a perturbação não
exibiu. Muitos suspeitos vêm de tabelas de 1–2 linhas (`stg_exposures`, `stg_metrics`), onde a perturbação não
altera nenhuma agregação; dependência condicional (ex.: valor só importa sob certo filtro) também aparece assim.
Não dá para separar "falso positivo" de "dado insuficiente" sem mais dados.

### Mais dados (13×, ≈2,4 mil linhas em `stg_nodes`) e perturbação "trocar por valor real"
| | 184 linhas | ≈2,4 mil linhas |
|---|---|---|
| Recall por modelo | 100 % | 100 % |
| Precisão (limite inferior) por modelo | 43 % | 44–46 % |
| Precisão (limite inferior) por coluna | 38 % | 37–39 % |

Mais linhas e mais tipos de perturbação quase não mudaram a precisão, então os "suspeitos" **não são falta de dado**.
Verificação manual de `stg_metrics.resource_type` → `fct_chained_views_dependencies`: trocar o valor para `'model'`
muda `int_all_graph_resources` (465 → 491 modelos), mas a saída de `fct_chained_views_dependencies` não muda, porque
as linhas perturbadas não têm parceiros no join. A dependência é estrutural (filtro sobre `resource_type` de todas as
linhas da UNION), não observável com estes dados. Ou seja: a linhagem é conservadora de forma legítima, e o oráculo
por perturbação só confirma efeitos observáveis; **a precisão real fica entre o limite inferior (≈40 %) e 100 %**.

### Oráculo mais rápido
Reconstrução incremental: só os modelos que dependem da origem perturbada (declarado em `depends_on` ou com a
relação no SQL) são refeitos; `--source nome[.coluna]` limita as origens. 75 colunas em 11 min contra >1 h 25 min
para 196 colunas antes.

## Desempenho (medido)
| Etapa | Antes | Depois | Como |
|---|---|---|---|
| Linhagem, 101 modelos / 1.228 colunas | 34 s | **1,8 s** | `trim_selects=False, copy=False`: o `sqlglot.lineage()` fazia `deepcopy` da árvore a cada coluna (92 % do tempo); arestas idênticas |
| `check` completo (ra_data_warehouse) | 23 s | **7 s** | idem |
| `data diff`, 3 M linhas × 31 colunas | ~100 consultas/varreduras | **10 consultas, 1,4 s** | contagem+estatísticas numa varredura, diferenças por colunas em lotes de 40 |
| Oráculo de linhagem | reconstrói todos os modelos | só os afetados | `--source` limita as origens |
Com cache por modelo (hash do SQL+esquema dos pais) a segunda execução só refaz o que mudou.
O oráculo reconstrói modelos: é para validar em amostra/CI, não para rodar sobre produção.

### Escala (segunda rodada)
| Item | O que faz | Limite conhecido |
|---|---|---|
| Linhagem em paralelo | modelos fora do cache vão para N processos (`spawn`, padrão 4; `INTENTFLOW_WORKERS` ou `workers=`). ra_data_warehouse: 1,75 s → 0,73 s (4 proc.), arestas idênticas ao serial | só em processo de thread única (CLI/CI); o servidor web fica serial na 1ª execução e usa o cache depois. Script sem `if __name__ == "__main__"` é reexecutado pelo spawn (o filho nunca abre outro pool). Ganho cresce com nº de modelos (o pool custa ~0,3 s fixos) |
| `data diff --sample-pct P` | compara ~P % das chaves por hash da chave (mesmo balde nas duas relações), devolve contagens da amostra + estimativa do total | hash por motor (DuckDB, Postgres, Redshift, Snowflake, BigQuery, Databricks, Trino/Athena, ClickHouse, MySQL, SQL Server); só DuckDB foi executado de verdade. Amostra não prova ausência de diferença em linhas fora dela |
| `lineage-verify --sample-rows N` | reduz cada origem a ~N linhas (hash da chave, preserva junções) na cópia temporária | coluna que só aparece em linhas raras pode virar "intestável"; precisão medida é limite inferior |
| Linhagem incremental por PR | base e head compartilham o cache por modelo; `lineage` (CLI) e a UI agora também usam. Só os modelos com SQL/esquema alterado são recalculados | a chave inclui o esquema dos pais: mudar uma coluna recalcula quem lê esse pai |

### Escala sintética (`.bench/scale.py`)
Modelos em camadas (join, agregação, janela, UNION; ~11 colunas), arestas idênticas entre serial e paralelo.

| Modelos | Colunas | Serial | 4 processos | Cache quente | 1 modelo alterado |
|---:|---:|---:|---:|---:|---:|
| 1.000 | 11.250 | 4,4 s | 2,3 s | 0,09 s | 0,09 s (999/1000 do cache) |
| 5.000 | 56.250 | 26,3 s | 10,9 s | 0,6 s | 0,6 s (4.999/5.000 do cache) |

- Paralelo ≈ 2,4×; 8 processos foram **piores** (13,3 s) que 4: padrão agora é 4.
- Linear no nº de modelos: ~5 ms por modelo. Gargalo do paralelo = serializar o projeto para cada processo (`spawn`).
- Script sem `if __name__ == "__main__"` gerou cascata de processos (o `spawn` reimporta o módulo e cada filho abria outro pool). Corrigido: processo filho nunca abre pool; teste cobre.
- SQL sintético é mais simples que o real (ra_data_warehouse: ~18 ms/modelo); espere ~3× mais lento em projeto real.
