# Arquitetura

[English](../ARCHITECTURE.md)

## Princípio

Camada de controle **ao lado** das ferramentas existentes. Nunca executa transformação própria.
Coletores orientados a leitura; alterações de código por proposta aprovada. A validação `dbt build` usa DuckDB temporário em contêiner local sem rede.
Credenciais de conexão vêm do ambiente; MFA/SSO e chaves de IA podem ser persistidos (ver cifragem em `DEPLOYMENT.md`).
**Sem arquivos de configuração YAML:** fontes, políticas, rotas e sinks vivem no armazém (tabela `settings`) e são editados por CLI ou UI.

```
dbt (manifest/catalog) · Airflow (REST, DAGs) · Kestra (REST, flows) · warehouses · Docker/host
                │
        Coletores (somente leitura)
                │
      ┌─────────▼──────────┐
      │ Armazém SQLite/PG   │  achados (ciclo de vida) · execuções · amostras · histórico · propostas · auditoria
      └─────────▲──────────┘
                │
   Checks plugáveis  ──►  achados  ──►  CLI (CI/portão)  ·  UI local  ·  propostas governadas
```

## Módulos (`src/intentflow`)

| Módulo | Função |
|---|---|
| `dbt_loader` | Lê `manifest.json`/`catalog.json`. Distingue colunas reais (catálogo) das apenas declaradas no YAML. |
| `checks/` | Registro de checks `@check(id, stage, cost)`. Um check quebrado vira achado `info`, não derruba os demais. |
| `warehouses/` | Leitura: DuckDB, PostgreSQL (local); Snowflake, BigQuery, Databricks (`cloud.py`). `rows`, `scalar`, `explain`, `spill_report`. |
| `orchestrators/` | Airflow (REST v1/v2) e Kestra (REST) → `Run` normalizado. |
| `monitoring` | Amostras de memória (host, Docker, pools do Airflow, spill) e previsão de esgotamento. |
| `lineage` | Linhagem **direta** (coluna a coluna) e **indireta** (coluna usada em JOIN/filtro/agrupamento/ordenação/janela/CASE afeta o modelo inteiro). Medida em projeto real por `lineage_oracle` (perturba dados e compara). Colunas de saída são **inferidas do SELECT** compilado (não só do YAML); pai sem aresta de coluna cai para dependência de modelo (`opaque`), nunca para "nada". Reporta o que **não** resolveu e a taxa. Cache por modelo (hash do SQL, dialeto e esquema dos pais): base e head de um PR compartilham o cache, então só o modelo alterado é recalculado. Modelos fora do cache vão para N processos (`spawn`, padrão 4, `INTENTFLOW_WORKERS`); em processo com threads fica serial. |
| `store` | SQLite (arquivo) ou PostgreSQL (`postgresql://`, `env:VAR`); mesma API, `copy_store` migra entre eles. Achados: abertos → reconhecidos (com validade) → resolvidos automaticamente. |
| `extensions` / `i18n` | Ponto de extensão (o IntentFlow Enterprise adiciona `proposals`, `llm_fix`, `jev`, `oidc`, papéis por projeto e saídas corporativas) e traduções (`tr()`, `locale/pt.json`). |
| `overview` / `web` | Visão única e UI local (loopback, anti-CSRF, anti DNS rebinding). |
| `runner` | Orquestra uma verificação (CLI, UI e agendador): fontes e políticas vêm do armazém. |
| `incidents` / `notify` | Agrupa achados por causa raiz; roteia por dono/severidade para Slack, webhook, e-mail, PR; anti-spam de 24 h. |
| `datadiff` | Diff de dados entre duas relações, executado no warehouse: ~10 consultas independentes do nº de colunas (lotes de 40) e, com `sample_pct`, amostragem por hash da chave (`Warehouse.sample_pred`, hash próprio de cada motor; base e nova caem no mesmo balde). |
| `lineage_oracle` | Validação dinâmica da linhagem (`lineage-verify`): perturba origens numa cópia DuckDB, reconstrói só os modelos afetados e compara. `--sample-rows` e `--source` para bases grandes. Não roda sobre produção. |
| `openlineage` | Entrada de eventos OpenLineage (arquivo ou receptor HTTP). |
| `risks` | Relatório do que pode quebrar no processo todo (visibilidade, fragilidade, processo); CLI `risks`. |
| `pyflow` | Análise estática (AST) de job Python/PySpark/pandas para `external`: datasets lidos/escritos, colunas e `feeds`. |
| `external` | Leituras de jobs fora do SQL (OpenLineage + `intentflow.lineage.json`), casadas com modelos; alimentam `impact` e `OL001`. |
| `cli` | `config`, `policy`, `check`, `gate`, `diff`, `runs`, `monitor`, `watch`, `notify`, `openlineage`, `lineage`, `overview`, `ui`, `ack`, `fp`, `stats`, `prune`, `fix`. |

## Checks: três gatilhos, três custos

- **Gatilho:** `pre-merge` (PR/CI), `pre-run` (portão antes do job), `continuous` (histórico).
- **Custo:** `static` (só código/metadados), `probe` (consulta ao warehouse), `statistical` (histórico acumulado).

Um achado traz: id estável, severidade (por impacto a jusante), evidência, nós afetados, confiança e, quando existe, correção.

## Garantias de segurança

- Carregar um projeto SQLMesh executa o código dele: exige `INTENTFLOW_SQLMESH_ALLOW_EXEC=1` ou o snapshot JSON.
- Coletores usam consultas de leitura. A restrição efetiva também depende das permissões da credencial no warehouse; não há isolamento universal entre drivers.
- Aplicar uma correção exige aprovação e validação ligadas ao mesmo conteúdo, além dos hashes atuais dos arquivos. A política pode exigir `build` e aprovador distinto do autor.
- `dbt_sandbox` executa em Docker sem rede, com perfil temporário, entrada somente para leitura e recursos limitados. O perfil/credenciais originais não entram no ambiente. Falha de isolamento não autoriza execução no host.
- A UI usa loopback por padrão. Exposição externa exige equipe e TLS ou proxy declarado; há verificação de `Host`, origem e cabeçalho `X-IntentFlow`. Corpos de requisição têm limite e timeout.
- Aprovar registra autorização para aplicar o diff; a validação `build` é separada e escreve somente no ambiente temporário de demonstração.
- `file_transaction` usa lock compartilhado/exclusivo por projeto, journal com fsync, estados intermediários, rename por arquivo e recuperação explícita/automática. O snapshot dbt respeita o lock e recusa ler durante/interrupção de uma escrita. Leitores externos ainda podem observar parte de um lote em andamento se não respeitarem o lock; atomicidade visível a qualquer leitor exige layout versionado com ponteiro de geração.
- A implementação de demonstração usa DuckDB; não comprova semântica de outros warehouses. Guia: VALIDATION_RECOVERY (documentação do Enterprise).

Evidências e pendências: [revisão de 13/10/2026](REVIEW_2026-10-13.md).

## Como estender

- **Novo check:** função `(ctx) -> list[Finding]` com `@check("XXX001", "título", stage, cost)` em `checks/`.
- **Novo warehouse:** subclasse de `Warehouse` com `rows()` (e `spill_report()` se houver sinal de memória).
- **Novo orquestrador:** cliente que devolve `Run` (ex.: Dagster, Prefect) e, se houver, pools/filas para `monitoring`.
