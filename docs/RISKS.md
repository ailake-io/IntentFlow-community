# What can break in the whole process

[Português](pt-BR/RISKS.md)

`intentflow risks --target target [--base ../main/target] [--declared intentflow.lineage.json] [--fail-on-high]` lists, **for your project**,
what can break today without anyone being told. Each item says what it is, what breaks and how to fix it. This page is the general map.

## 1. Visibility: where IntentFlow cannot see
| Gap | Consequence | Fix |
|---|---|---|
| Model without `catalog.json` | removed/renamed columns are not detected | `dbt docs generate` after the build |
| Column with unresolved lineage (`select *` without a schema, macro) | a change at the source raises no alert; falls back to the whole model | explicit columns, catalog |
| Opaque dependency | whole-model impact: too many or too few alarms | make the SQL explicit |
| External job dataset that is ambiguous or has no model | a change in the model does not reach the job | `lineage --alias DATASET=MODEL` |
| External job without a known output column | conservative propagation (too many alarms) | `feeds`, `sql_file`, `py_file` or `columnLineage` |

## 2. Fragility: what hurts most if it changes
| Point | Consequence | Fix |
|---|---|---|
| Column with ≥ 3 consumers (models + jobs) without a test or contract | cascading break and nothing stops it in the PR | column test + `contract.enforced` |
| Model with consumers and no owner | no one is told | `meta.owner` (`DBT003`) |
| External job without an owner | `OL001`/`OL003` have no recipient | `owner` in the declaration |
| PII reaching a model or job without masking | leak | `DBT004`, `OL003` |

## 3. Process: what makes IntentFlow stop protecting
| Failure | Consequence | Fix |
|---|---|---|
| No `--base` | the PR gate does not compare against `main` | build the `main` `target/` in CI |
| No source of external jobs | Spark/Python/notebooks are out of the impact | OpenLineage or `intentflow.lineage.json` |
| Old OpenLineage events (> 14 days) | the impact uses stale reads | check the emitter |
| Pipeline with a failed run | stale data downstream | `intentflow runs` |
| Checks turned off by policy | the problem is no longer reported | review `disabled_checks` |
| No monitoring samples | volume/freshness anomalies are not seen | schedule `intentflow monitor` |

## 4. Known limits of IntentFlow itself
| Limit | Effect |
|---|---|
| Lineage is static (compiled SQL) | SQL generated at runtime outside dbt is not seen |
| Measured precision is a floor (~40 % on public projects), high recall | it may list too much; it should not list too little |
| Python job analysis is heuristic (AST) | what it does not understand becomes conservative (`opaque`), never disappears |
| Dataset matching by name suffix | similar names can collide: `OL002` warns |
| Sampling hash for 9 engines only generated | `diff --sample-pct` only really run on DuckDB |
| No third-party production pipeline measured | see [PILOT](PILOT.md) |
| `risks` looks at the 200 highest-degree columns | very flat graphs may hide a fragile column |

## In CI
```bash
intentflow risks --target target --base ../main/target --fail-on-high   # fails if there is a high severity item
```
Run it next to `intentflow check --base ... --fail-on high`: `check` looks at **this change**; `risks` looks at **the state of the process**.
