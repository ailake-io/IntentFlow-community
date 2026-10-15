# O que pode quebrar no processo todo

[English](../RISKS.md)

`intentflow risks --target target [--base ../main/target] [--declared intentflow.lineage.json] [--fail-on-high]` lista, **para o seu projeto**,
o que hoje pode quebrar sem ninguém ser avisado. Cada item traz: o que é, o que quebra e como corrigir. Esta página é o mapa geral.

## 1. Visibilidade: onde o IntentFlow não enxerga
| Lacuna | Consequência | Correção |
|---|---|---|
| Modelo sem `catalog.json` | coluna removida/renomeada não é detectada | `dbt docs generate` depois do build |
| Coluna com linhagem não resolvida (`select *` sem esquema, macro) | mudança na origem não alerta; cai para o modelo inteiro | colunas explícitas, catálogo |
| Dependência opaca | impacto do modelo inteiro: alarme demais ou de menos | tornar o SQL explícito |
| Dataset de job externo ambíguo ou sem modelo | mudança no modelo não chega ao job | `lineage --alias DATASET=MODELO` |
| Job externo sem coluna de saída conhecida | propagação conservadora (alarme em excesso) | `feeds`, `sql_file`, `py_file` ou `columnLineage` |

## 2. Fragilidade: o que mais dói se mudar
| Ponto | Consequência | Correção |
|---|---|---|
| Coluna com ≥ 3 consumidores (modelos + jobs) sem teste ou contrato | quebra em cascata e nada barra no PR | teste na coluna + `contract.enforced` |
| Modelo com consumidores e sem dono | ninguém é avisado | `meta.owner` (`DBT003`) |
| Job externo sem dono | `OL001`/`OL003` sem destinatário | `owner` na declaração |
| PII que chega a modelo ou job sem máscara | vazamento | `DBT004`, `OL003` |

## 3. Processo: o que faz o IntentFlow parar de proteger
| Falha | Consequência | Correção |
|---|---|---|
| Sem `--base` | gate de PR não compara com a `main` | gerar o `target/` da `main` no CI |
| Sem fonte de jobs externos | Spark/Python/notebooks fora do impacto | OpenLineage ou `intentflow.lineage.json` |
| Eventos OpenLineage antigos (> 14 dias) | impacto usa leituras desatualizadas | conferir o emissor |
| Pipeline com execução falha | dado desatualizado a jusante | `intentflow runs` |
| Checks desligados por política | problema deixa de ser reportado | revisar `disabled_checks` |
| Sem amostras de monitoramento | anomalia de volume/frescor não é vista | agendar `intentflow monitor` |

## 4. Limites conhecidos do próprio IntentFlow
| Limite | Efeito |
|---|---|
| Linhagem é estática (SQL compilado) | SQL gerado em tempo de execução fora do dbt não é visto |
| Precisão medida é um piso (~40 % em projetos públicos), recall alto | pode listar a mais; não deve listar a menos |
| Análise de job Python é heurística (AST) | o que não entende vira conservador (`opaque`), nunca some |
| Casamento de dataset por sufixo do nome | nome parecido pode colidir: `OL002` avisa |
| Hash de amostragem de 9 motores só gerado | `diff --sample-pct` só executado de verdade em DuckDB |
| Nenhum pipeline de produção de terceiros medido | ver [PILOT](PILOT.md) |

## Como usar no CI
```bash
intentflow risks --target target --base ../main/target --fail-on-high   # falha se houver item de severidade alta
```
Rode junto com `intentflow check --base ... --fail-on high`: o `check` olha **esta mudança**; o `risks` olha **o estado do processo**.
