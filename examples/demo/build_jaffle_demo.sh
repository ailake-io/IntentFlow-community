#!/usr/bin/env bash
# Monta um projeto dbt REAL e não trivial para testar o IntentFlow: jaffle-shop (dbt-labs) + dbt-project-evaluator (pacote de terceiros),
# em DuckDB, com dados de 150 mil linhas. Uso: examples/demo/build_jaffle_demo.sh [DIRETORIO]  (padrão: ./demo_jaffle)
# Requer: git, dbt-core + dbt-duckdb no PATH (ou DBT=/caminho/dbt), acesso à internet (GitHub e hub.getdbt.com).
set -euo pipefail
DEST="${1:-demo_jaffle}"
DBT="${DBT:-dbt}"
mkdir -p "$DEST" && cd "$DEST"
[ -d _src_jaffle ] || git clone -q --depth 1 https://github.com/dbt-labs/jaffle-shop.git _src_jaffle
[ -d dbt-project-evaluator ] || git clone -q --depth 1 https://github.com/dbt-labs/dbt-project-evaluator.git dbt-project-evaluator
rm -rf jaffle && cp -r _src_jaffle jaffle && rm -rf jaffle/.git
cd jaffle
# ajustes para rodar localmente em DuckDB
sed -i 's/require-dbt-version: ">=2.0.0"/require-dbt-version: ">=1.5.0"/' dbt_project.yml
sed -i '/^dbt-cloud:/,/project-id/d' dbt_project.yml
cat > profiles.yml <<'P'
default:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: jaffle.duckdb
      threads: 4
P
cat > packages.yml <<'P'
packages:
  - local: ../dbt-project-evaluator
P
mkdir -p macros && cat > macros/generate_schema_name.sql <<'P'
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}{{ target.schema }}{%- else -%}{{ custom_schema_name | trim }}{%- endif -%}
{%- endmacro %}
P
"$DBT" deps --profiles-dir .
# fontes não dependem dos seeds no grafo do dbt: carrega os dados brutos antes do build
"$DBT" seed --profiles-dir . --vars '{load_source_data: true}'
"$DBT" build --profiles-dir . --vars '{load_source_data: true}'
cp target/run_results.json /tmp/_rr.json
"$DBT" docs generate --profiles-dir . --vars '{load_source_data: true}'
cp /tmp/_rr.json target/run_results.json      # `docs generate` sobrescreve; mantém o resultado do build
echo
echo "Pronto: $(pwd)"
echo "  intentflow config set dbt.target $(pwd)/target"
echo "  intentflow config set dbt.root   $(pwd)"
echo "  intentflow config set warehouse.duckdb $(pwd)/jaffle.duckdb"
echo "  intentflow check && intentflow ui"
echo "  intentflow lineage-verify --target target --duckdb jaffle.duckdb --package jaffle_shop"
