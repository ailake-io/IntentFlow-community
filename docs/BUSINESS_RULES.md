# Follow business rules with IntentFlow

[Português](pt-BR/BUSINESS_RULES.md)

Unreleased checkout feature. Declare rules as **native dbt tests** tagged `business_rule`; IntentFlow reads their definitions/results. It does not infer what constitutes correct revenue, currency, customer or time policy. The business owner must define that expectation and the action on failure. No separate IntentFlow YAML or SQL execution engine is needed.

## Add a rule

Create `tests/paid_orders_reconcile.sql` in your dbt project:

```sql
{{ config(tags=['business_rule'], severity='error', meta={'owner': 'finance'}) }}

with paid as (
  select order_id, sum(amount) as paid_amount
  from {{ ref('payments') }}
  where status = 'settled'
  group by order_id
)
select o.order_id
from {{ ref('orders') }} o
left join paid p on p.order_id = o.order_id
where o.status = 'paid'
  and abs(o.total - coalesce(p.paid_amount, 0)) > {{ var('payment_tolerance', 0.01) }}
```

The test returns violating rows, as usual in dbt. Adapt table/column names, settlement/cancellation rules, amount types, currency scope and tolerance to your business. A `failures` value is the count returned by the test configuration: an aggregate reconciliation may return one violation, not every affected transaction. Negative values can be valid refunds. Do not label them invalid without the relevant event/state condition.

Run dbt, preserve the **build** run_results before docs generation, and ingest with `intentflow check --target target --root .`. Configure `dbt.target`/`dbt.root` in the store for watch/UI. Use:

```bash
intentflow rules --target target --root .
intentflow rules --target target --root . --format json
```

`rules` is a read-only catalog command; its exit status is not a quality gate. Use preflight or check with --fail-on in CI.

The **Business rules** UI page shows name, description when present in dbt metadata, owner, latest result, failing-count value, directly read models, downstream impact and recent executions. `--all` includes untagged tests in the CLI catalog; only tagged business rules have the dedicated history.

Generic tests can use the same native `config.tags` and `config.meta.owner` in their dbt YAML. Unit tests are also supported when their definitions/results are present. An explicit test owner wins; otherwise one unambiguous input-model owner is inherited. For multi-owner inputs set the rule owner explicitly.

## What gets followed

- DBTR001 reports failed/warning/error tests, including singular tests referencing several models and dbt unit tests. It keeps test id, owner, model dependencies and downstream impact. Existing incident routing can use that owner.
- DBTR003 reports models skipped after failed tests; skipped unit tests are not counted as models.
- BIZ001 reports missing, skipped or stale rule execution (24-hour default; `policy.max_rule_age_hours` configures this monitoring age). BIZ002 reports a rule without an explicit or unambiguous inherited owner.
- A result can be pass/fail/warn/error/skipped. Missing or stale evidence is shown separately, never as pass. A real zero-violation result counts as pass.
- Normal checks persist the last **20 observed invocations per tagged test id** in the project store, deduplicated by dbt invocation. Re-reading the same build does not add executions. This is bounded execution history, not an unlimited business-metric time series. Keep the store persistent/backed up; changed dbt test ids start a separate history.
- Preflight blocks failed/skipped/full-build gaps independently of finding severity and can enforce all warning results. Failed preflight artifact validation returns diagnostics before ingesting; normal check/watch is responsible for following those failed runs in the shared store.

The UI reads results; editing and executing the rules happens in the dbt repository and its normal CI/deployment process. No rule runs continuously just because it was declared. Schedule dbt and IntentFlow watch/check, and configure notification routes as needed. This does not automatically identify a business-rule definition that was deleted or weakened; review the rule diff as part of the PR.

## Daily cases and how to express them

| Case | Rule or evidence |
|---|---|
| Duplicate key / replay | unique + not_null, incremental unique_key, PRB001, retry/backfill test |
| Orphan customer/order | relationships or a custom anti-join test |
| Null mandatory field | not_null, contract required, PRB003 with trusted history |
| Invalid state or currency | accepted_values or a custom state/currency test |
| Paid amount differs from invoice | grouped-payment reconciliation with explicit tolerances |
| Wrong arithmetic / units | independent expected calculation or unit test; data diff shows changes, not which version is correct |
| Daily/customer/ledger totals diverge | reconciliation across fact/marts/ledger, retaining unknown-customer totals |
| Missing/partial batch | source-to-target counts/partitions, PRB002, explicit business completeness tests |
| Future/out-of-order/late event | explicit reference clock, timezone and lateness window; event-time and ingestion-time rules separately |
| Legitimate refund, zero-price gift, correction | condition rules by transaction state; preserve/quarantine evidence according to agreed policy |
| Schema change / broken downstream | catalog baseline + SCH checks and complete build/tests |
| Sensitive data spreading | declared PII metadata + DBT004/OL003; use separate discovery/security tools where needed |

Automated real-dbt regression exercises seven rules with clean and bad data: payment reconciliation, state, currency, paid positive amount, customer present, customer exists, future event. A cancelled negative amount stays valid. The UCI retail lab separately exercises reconciliations, duplication, partial extraction, pricing, schema changes and incremental retry. These are tests of specified scenarios, not proof of every production pattern.

## Contracts and external quality tools

Existing Data Contract Specification `models:` and ODCS v3 `schema:` files can define model/field names, types, required/unique constraints and PII. Set `contracts.dir`; CTR001 compares them with the catalog and declared enforcement. This is not a general evaluator of every ODCS/DCS quality expression. Required tests still need to execute and succeed. `unique_key` is configuration, not an executed uniqueness proof.

GX/Soda results are already ingested through `dq.results`; Elementary output has its existing integration. Those checks still require correctly configured results and do not become tagged dbt rules/history entries automatically. For mandatory contract availability use preflight `--require-check CTR001`. See [PREFLIGHT](PREFLIGHT.md) for promotion gates, CI provenance and remaining limits.

Contract model matching must be unambiguous. If multiple packages define orders, use sales.orders, model.sales.orders or the full physical relation. Simple ambiguous names produce a high-severity CTR001 instead of picking the first model; semantic package/model identities are more portable across environments than physical schemas.
