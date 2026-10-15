# Roteiro de piloto: testar o IntentFlow num pipeline real

[English](../PILOT.md)

Objetivo: em **2 a 3 horas** de uma pessoa que conhece o projeto, decidir se o IntentFlow vale ficar no CI dela.
O que já foi provado (projetos públicos) é o *motor*; o que falta provar é **utilidade num pipeline de produção**.

Nada aqui altera o projeto nem o warehouse: tudo é leitura, exceto onde dito "cópia".

## 0. Pré-requisitos (10 min)
- Projeto dbt (1.x) que compila: `dbt build && dbt docs generate` (gera `target/manifest.json` **e** `target/catalog.json`; sem o catálogo a linhagem de coluna perde precisão).
- Python 3.11+: `pip install -e ".[dev]"` (ou `pip install intentflow` quando publicado).
- Opcional: DuckDB/PostgreSQL de **desenvolvimento** para sondas e diff. Nunca produção sem permissão.

## 1. Achados (20 min): `check`
```bash
intentflow check --target target --root . --format markdown > achados.md
intentflow stats          # precisão por check (depois de marcar falsos positivos)
```
Para cada achado, marque na UI (`intentflow ui`) ou pela CLI: **útil** (agiria), **falso positivo** (`intentflow fp ID`), **ruído** (verdadeiro, mas não importa).
Anote os números: total, % útil, % falso positivo. **Limiar sugerido:** < 50 % de falso positivo para ligar `--fail-on` no CI.

## 2. Linhagem e impacto (30 min): o diferencial
Escolha **5 colunas** que alguém já quebrou ou teve medo de mudar.
```bash
intentflow lineage --target target --column modelo.coluna --impact
```
Sem `--impact` aparece só 1 salto (origem e consumidores diretos); com ele, o fecho transitivo (incluindo uso em join/filtro), comparável a `dbt ls --select modelo+`.
Compare com o que a equipe sabe:
| Pergunta | Como medir |
|---|---|
| Listou algo que **não** depende (falso positivo)? | revisar cada modelo listado |
| **Faltou** algo que depende (falso negativo)? | o pior caso; anote e abra issue com o SQL |
| É mais estreito que `dbt ls --select modelo+`? | contar modelos nos dois |
Meta: nenhum falso negativo nas 5 colunas. Falso positivo é tolerável.

## 3. O PR que quebra (30 min): o caso de uso principal
1. Gere o `target/` da `main` e guarde (ex.: `../main/target`).
2. Num branch, **renomeie** (`coluna as coluna_nova`, sem ajustar os consumidores) ou **remova** uma coluna usada a jusante. O `dbt build` falha nos consumidores: é o esperado.
3. `dbt build && dbt docs generate`, depois:
```bash
intentflow check --target target --base ../main/target --fail-on high --format markdown
```
Esperado: achado `SCH001` crítico (coluna removida/renomeada, com candidato de renome) listando consumidores diretos e o impacto transitivo; saída com código 1. Tempo e clareza da mensagem importam: um revisor entenderia sem abrir o SQL?

## 4. Validação dinâmica da linhagem (20 min, só com DuckDB)
```bash
intentflow lineage-verify --target target --duckdb dev.duckdb --sample-rows 1000 --out verify.json
```
Roda numa **cópia temporária**. Leia: `direct.precision_lower_bound` (piso, não valor real), `model_level.fn` (deve ser 0) e `unbuildable` (modelos que o DuckDB não executa).
Se o projeto é BigQuery/Snowflake, muitos modelos serão `unbuildable`: normal; use o passo 2.

## 5. Diff de dados (20 min): `diff`
```bash
intentflow diff --base prod.orders --head dev.orders --key order_id --sample-pct 10 --duckdb dev.duckdb
```
`--base`/`--head` são `schema.tabela` no mesmo DuckDB (ou use `--model` com `--base-schema`/`--head-schema`). Com `--sample-pct` a contagem é da amostra e vem uma estimativa do total. Confira se os números batem com o que você espera de uma mudança conhecida.

## 5b. O que pode quebrar (5 min)
```bash
intentflow risks --target target --base ../main/target
```
Leia os itens de severidade alta: cada um diz o que quebra e como corrigir ([RISKS](RISKS.md)). Anote quantos você concorda que são reais.

## 6. Escala (10 min)
```bash
time intentflow lineage --target target --column modelo.coluna    # 1ª vez (a frio)
time intentflow lineage --target target --column modelo.coluna    # 2ª vez (cache)
```
Anote nº de modelos, colunas e os dois tempos.

## 7. Correções (opcional, 20 min)
Em `intentflow ui` → Achados: **Sugerir correção** em um DBT002 (informe a chave) e um DBT003 (informe o dono). Veja o diff, **não aplique em produção**;
`fix validate --mode build` exige a imagem Docker do sandbox (VALIDATION_RECOVERY (documentação do Enterprise)).

## O que devolver
Preencha e envie (ou cole numa issue):

| Medida | Valor |
|---|---|
| Nº de modelos / colunas / dialeto | |
| Achados: total · úteis · falsos positivos · ruído | |
| Linhagem: colunas testadas · falsos negativos · falsos positivos | |
| PR sintético: detectou? tempo? a mensagem bastou? | |
| `lineage-verify`: precisão (piso) · FN · modelos `unbuildable` | |
| Tempo a frio / com cache (passo 6) | |
| Incidentes passados que o IntentFlow teria evitado (descreva 1–2) | |
| Você ligaria no CI? sim / não / com ressalvas, e por quê | |

## Critérios para continuar (de [ROADMAP](ROADMAP.md))
| Medida | Limiar |
|---|---|
| Colunas com linhagem correta | ≥ 85 % |
| Falsos negativos de impacto | 0 nas colunas testadas |
| Falso positivo por check | < 50 % |
| Incidentes/mês por mudança de upstream | ≥ 1 (senão o ganho é pequeno) |

## Se algo der errado
- **Projeto SQLMesh:** `intentflow sqlmesh-export PROJETO` num ambiente confiável e use o JSON como `--target` (ler o projeto executa o código dele).
- **`select *` sem esquema:** aparece como "não resolvido"; rode `dbt docs generate` para ter o `catalog.json`.
- **Muito lento:** `INTENTFLOW_WORKERS=4` (CLI) e confirme o cache; em servidor web a 1ª execução é serial.
- **SSO em rede privada:** `INTENTFLOW_SSO_ALLOW_INTERNAL=1`.
