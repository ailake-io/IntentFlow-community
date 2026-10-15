# Desempenho da linhagem de coluna

[English](../BENCHMARKS.md)

Medido em 11/10/2026, Intel i7-6820HQ (8 threads), 31 GB, Python 3.13, SQLGlot 30, projeto **sintético** (`tests/synth_project.py`):
cada modelo faz `JOIN` de duas fontes com CTEs, `SUM`, `COALESCE` e 10 colunas de saída.

| Modelos | Colunas | A frio | Com cache (inalterado) | Pico RSS |
|---|---:|---:|---:|---:|
| 300 | 3.000 | 7,0 s (2,35 ms/coluna) | 0,02 s | — |
| 1.000 | 10.000 | 24,2 s (2,42 ms/coluna) | 0,09 s | 58 MB |

Escala linear. Referência anterior à otimização: 9,4 ms/coluna (28 s para 3.000 colunas).

O que mudou: (1) o esquema é um `MappingSchema` construído uma vez — passar o dict fazia o SQLGlot renormalizar tudo a cada coluna
(≈78% do tempo); (2) `qualify` e escopo uma vez por modelo; (3) cache por modelo, chaveado pelo hash do SQL compilado, do dialeto e do
esquema dos pais, guardado no armazém SQLite (só modelos alterados são recalculados).

**Limites:** SQL sintético e regular; projetos reais com `SELECT *` largos, centenas de colunas por modelo e macros pesadas podem custar
mais por coluna. Antes de afirmar capacidade para um projeto real, medir nele.

**Atualização (segunda rodada):** a causa dominante restante era o `deepcopy` da árvore a cada coluna dentro do `sqlglot.lineage()`;
`trim_selects=False, copy=False` deixou a linhagem ~19× mais rápida com arestas idênticas. Paralelismo (spawn, 4 processos) e escala sintética de 5.000 modelos:
ver [BENCH_REAL_PROJECT](BENCH_REAL_PROJECT.md), seções "Desempenho" e "Escala".


## Linhagem de coluna medida em projeto dbt real (oráculo dinâmico)

**Projeto:** `dbt-labs/jaffle-shop` (13 modelos próprios, 7 seeds, 150 mil linhas) com `dbt-labs/dbt-project-evaluator` instalado como
pacote (≈ 48 modelos de terceiros com SQL pesado em macros) rodando em DuckDB; `dbt build` limpo (`PASS=119 ERROR=0`). Total: 61 modelos,
6 fontes, 23 métricas, 6 modelos semânticos. O IntentFlow resolveu **100%** das colunas sem erro.

**Como se mede sem gabarito manual** (`intentflow lineage-verify`): para cada coluna de origem o oráculo (1) zera os valores, (2) desloca
(+constante, +1 dia, sufixo), (3) inverte a ordem (negativo, data espelhada, `reverse`), reconstrói todos os modelos do projeto a partir do
SQL compilado dentro de uma transação DuckDB (com `ROLLBACK` entre perturbações) e vê **quais colunas de saída mudaram** (valor pareado com a
chave única do modelo, para enxergar `ROW_NUMBER`/`RANK`). Compara com o fechamento da linhagem estática.

| Medida (modelos do projeto jaffle, 30 colunas de origem perturbadas) | Resultado |
|---|---|
| Linhagem **direta**, coluna a coluna: acertos / suspeitos de falso positivo | **88 / 0** (precisão ≥ 100%) |
| Mudanças perdidas pela linhagem direta e **não** explicáveis por dependência indireta | **0** |
| Colunas que mudaram só por JOIN/filtro/agrupamento/ordenação (dependência indireta) | 117 |
| Com dependência indireta, por modelo: acertos / suspeitos de FP / perdidos | **74 / 5 / 0** (precisão ≥ 93,7%, recall 100%) |
| Tempo (13 modelos, 56 colunas × até 3 perturbações) | ≈ 3 min |

**O que o oráculo achou (e foi corrigido):**
1. **Sem linhagem indireta** o recall de valor era só **62,6%**: 52 mudanças vinham de chaves de JOIN e filtros. Agora há dependência
   *indireta* (JOIN, filtro, agrupamento, ordenação, janela, CASE) em nível de modelo, usada no impacto e em `SCH001`/`SCH002` — remover uma
   chave de join que nunca é projetada passa a ser **crítico**.
2. **Seed e fonte para a mesma tabela:** as arestas ligavam ao nó errado. Agora preferem o pai declarado em `depends_on`.
3. **O próprio oráculo tinha vieses**, corrigidos e testados (inclusive que ele *detecta* uma linhagem sabotada): só testar colunas que a
   linhagem diz serem lidas escondia falsos negativos; multiconjunto escondia `ROW_NUMBER`; hash da linha inteira contaminava tudo.

**Limites desta medida** (não é um gabarito):
- "Suspeito de falso positivo" é um **limite superior**: pode ser dependência real que as perturbações não revelam (ex.: `COALESCE` que nunca
  cai no ramo, filtro que nunca exclui). Precisão ≥ X% é um piso.
- Só vale para DuckDB e dados reais pequenos; modelos com `incremental`/`snapshot` são reconstruídos como tabela completa.
- Os modelos do `dbt-project-evaluator` leem o `graph` do dbt (não os dados), então não são exercitados pela perturbação de dados; só a
  resolução estática (100% sem erro) foi verificada neles.
- Um único projeto de 13 modelos de negócio. Para o **seu** projeto: `intentflow lineage-verify --target ... --duckdb ...`.

## Revisão de 13/10/2026: consultas e análise SQL

Microbenchmarks locais, com dados sintéticos, Python 3.13.5 e SQLGlot 30.22.0. Valores medidos antes/depois no mesmo ambiente:

| Operação | Antes | Depois |
|---|---:|---:|
| Listar 2.000 achados: consultas SQLite | 2.001 | 1 |
| Listar 2.000 achados: tempo, uma execução | 0,09185 s | 0,02011 s |
| 11 checks SQL × 300 modelos: chamadas ao parser por execução | 3.300 | 300 |
| Checks SQL: mediana de 3 execuções | 1,26363 s | 0,20841 s |

A lista de achados agora agrega a contagem de notas na mesma consulta. Os checks compartilham o AST de cada modelo durante uma execução;
o cache é limpo antes da próxima rodada. Esses resultados não medem latência HTTP, nuvem, grandes tabelas nem carga concorrente.
Não houve análise estatística de variância. Uma repetição posterior mediu 0,02153 s e 0,21966 s, respectivamente, com as mesmas contagens.

Reproduzir a versão atual, a partir da raiz do projeto instalado:

```bash
.venv/bin/python scripts/benchmark_review.py
```

O script usa somente dados sintéticos e SQLite em memória. Ele não reconstrói automaticamente a versão anterior.
A troca de processos de linhagem para `spawn` evita herdar locks de threads nativas, mas pode aumentar o custo de inicialização;
ela é uma correção de concorrência e não está incluída no ganho de tempo dos checks acima.
Relatório e limites: [REVIEW_2026-10-13](REVIEW_2026-10-13.md).
