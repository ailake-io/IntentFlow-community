# Pilot guide: try IntentFlow on a real pipeline

[Português](pt-BR/PILOT.md)

Goal: in **2 to 3 hours** of one person who knows the project, decide whether IntentFlow is worth keeping in their CI.
What has been proven (public projects) is the *engine*; what is left to prove is **usefulness on a production pipeline**.

Nothing here changes the project or the warehouse: everything is read-only, except where it says "copy".

## 0. Prerequisites (10 min)
- A dbt (1.x) project that compiles: `dbt build && dbt docs generate` (produces `target/manifest.json` **and** `target/catalog.json`; without
  the catalog column lineage loses precision).
- Python 3.11+: `pip install -e ".[dev]"` (or `pip install intentflow` once published).
- Optional: a **development** DuckDB/PostgreSQL for probes and diffs. Never production without permission.
- Language: English by default; `INTENTFLOW_LANG=pt` for Portuguese.

## 1. Findings (20 min): `check`
```bash
intentflow check --target target --root . --format markdown > findings.md
intentflow stats          # precision per check (after marking false positives)
```
For each finding, mark it in the UI (`intentflow ui`) or the CLI: **useful** (you would act), **false positive** (`intentflow fp ID`), **noise** (true, but irrelevant).
Write down: total, % useful, % false positive. **Suggested threshold:** < 50 % false positives to turn on `--fail-on` in CI.

## 2. Lineage and impact (30 min): the differentiator
Pick **5 columns** someone has already broken or was afraid to change.
```bash
intentflow lineage --target target --column model.column --impact
```
Without `--impact` only 1 hop shows (origin and direct consumers); with it, the transitive closure (including use in join/filter and external
jobs), comparable to `dbt ls --select model+`.
Compare with what the team knows:
| Question | How to measure |
|---|---|
| Did it list something that does **not** depend on it (false positive)? | review each listed model |
| Did it **miss** something that depends on it (false negative)? | the worst case; write it down and open an issue with the SQL |
| Is it narrower than `dbt ls --select model+`? | count models in both |
Goal: no false negative on the 5 columns. False positives are tolerable.

## 3. The PR that breaks (30 min): the main use case
1. Build the `main` `target/` and keep it (e.g. `../main/target`).
2. On a branch, **rename** (`column as new_column`, without updating the consumers) or **remove** a column used downstream. `dbt build` fails on
   the consumers: that is expected.
3. `dbt build && dbt docs generate`, then:
```bash
intentflow check --target target --base ../main/target --fail-on high --format markdown
```
Expected: a critical `SCH001` finding (column removed/renamed, with a rename candidate) listing direct consumers and the transitive impact;
exit code 1. Time and clarity of the message matter: would a reviewer understand it without opening the SQL?

## 4. Dynamic lineage validation (20 min, DuckDB only)
```bash
intentflow lineage-verify --target target --duckdb dev.duckdb --sample-rows 1000 --out verify.json
```
Runs on a **temporary copy**. Read: `direct.precision_lower_bound` (a floor, not the real value), `model_level.fn` (must be 0) and
`unbuildable` (models DuckDB cannot run). For BigQuery/Snowflake projects many models will be `unbuildable`: normal; use step 2.

## 5. Data diff (20 min): `diff`
```bash
intentflow diff --base prod.orders --head dev.orders --key order_id --sample-pct 10 --duckdb dev.duckdb
```
`--base`/`--head` are `schema.table` in the same DuckDB (or use `--model` with `--base-schema`/`--head-schema`). With `--sample-pct` the counts
are the sample's and an estimate of the total comes along. Check that the numbers match what you expect from a known change.

## 5b. What can break (5 min)
```bash
intentflow risks --target target --base ../main/target
```
Read the high-severity items: each one says what breaks and how to fix it ([RISKS](RISKS.md)). Write down how many you agree are real.

## 6. Scale (10 min)
```bash
time intentflow lineage --target target --column model.column    # 1st time (cold)
time intentflow lineage --target target --column model.column    # 2nd time (cached)
```
Write down the number of models and columns and both times.

## 7. Fixes (optional, 20 min, Enterprise)
In `intentflow ui` → Findings: **Suggest fix** on a DBT002 (enter the key) and on a DBT003 (enter the owner). Look at the diff, **do not apply it in
production**; `fix validate --mode build` requires the sandbox Docker image (Enterprise docs).

## What to send back
Fill in and send (or paste in an issue):

| Measure | Value |
|---|---|
| Number of models / columns / dialect | |
| Findings: total · useful · false positives · noise | |
| Lineage: columns tested · false negatives · false positives | |
| Synthetic PR: detected? time? was the message enough? | |
| `lineage-verify`: precision (floor) · FN · `unbuildable` models | |
| Cold / cached time (step 6) | |
| Past incidents IntentFlow would have prevented (describe 1–2) | |
| Would you turn it on in CI? yes / no / with caveats, and why | |

## Criteria to continue (from the [ROADMAP](ROADMAP.md))
| Measure | Threshold |
|---|---|
| Columns with correct lineage | ≥ 85 % |
| Impact false negatives | 0 on the columns tested |
| False positives per check | < 50 % |
| Incidents/month from upstream changes | ≥ 1 (otherwise the gain is small) |

## If something goes wrong
- **SQLMesh project:** `intentflow sqlmesh-export PROJECT` in a trusted environment and use the JSON as `--target` (reading the project runs its code).
- **`select *` without a schema:** shows up as "unresolved"; run `dbt docs generate` to get `catalog.json`.
- **Too slow:** `INTENTFLOW_WORKERS=4` (CLI) and check the cache; in the web server the 1st run is serial.
