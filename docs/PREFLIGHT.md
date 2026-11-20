# Validate before promoting a dbt build

[Português](pt-BR/PREFLIGHT.md)

Unreleased source-checkout feature: `intentflow preflight`. Earlier release wheels do not contain this command. It complements `check` and the pre-run `gate`: it requires evidence from a **full candidate dbt build**, not just a static scan or a partial Enterprise sandbox validation.

## What blocks promotion

- Missing/invalid manifest, catalog or build results; candidate artifacts older than 24 hours by default, future timestamps or timestamps without a timezone.
- Results from docs/parse/run/test instead of build; empty results, missing enabled resources, duplicate/mismatched resource ids, missing enabled tests, failed/skipped/unknown statuses.
- Incomplete/erroring catalogs on either side; mismatched project/adapter; different compiled SQL between manifest and build; current SQL/Python model/test files differing from the manifest.
- A required check disabled by policy, an internal check exception, or a failed warehouse query even when its probe catches the exception.
- Removed/changed existing tests, business-rule tracking or interfaces requiring review against the baseline; missing catalog columns for any active relation. See [CHANGE_REVIEW](CHANGE_REVIEW.md).
- Findings at or above `--fail-on` (default high). `--warnings-as-errors` also blocks dbt warning results.

The default required checks are SQL001/002, DBT002/003/004, SCH001/002/003, DBTR001/002/003 and BIZ001/002. The normal check set runs, including continuous checks; the required set cannot silently disappear through disabled policies. `completed_checks` reports completed check functions, not proof that every integration, column or rule was evaluated. A check without its optional input can have nothing applicable to inspect.

`--require-check` adds **exact ids**, not prefixes. For PRB001–004/SQL012–014 it also requires an available warehouse connection. Required PRB002/003 need historical observations; initial warm-up is a gap, not an approval. Preflight reads probe history and does not train volume/null baselines on candidate data. Run normal checks on the trusted baseline first. Required CTR001 loads every declared contract strictly: missing/invalid/unsupported files cannot be silently ignored.

Schema comparison matches dbt node identity when homologation and production use different relation schemas. The change-review comparison also reports resource removals and column/type changes independently of known consumers; a full build/test and business assertions remain necessary.

## Mandatory source and external-quality evidence

`--require-source-freshness` requires `target/sources.json`, a configured freshness SLA for every declared source, complete source ids, current artifact timestamps and the requested revision metadata. Source error/runtime-error results block; warning results follow `--warnings-as-errors`. Loaded-data age is re-evaluated against the manifest SLA at promotion time, so an older passing snapshot cannot outlive its source SLA. Native nullable warn/error thresholds are supported. This covers dbt source freshness, not every source-specific completeness/event-time rule.

For GX/Soda, configure `dq.results` and add `--require-check DQ001`. Every input must exist, parse to recognized nonempty results, have a current native execution timestamp and map unambiguously to the project. Incomplete scans, unknown outcomes, missing files and ambiguous mappings block. Normal DQ001 findings still apply the configured severity threshold to quality failures; warning escalation is optional. Native timestamps correlate the execution age, not its exact commit or source-data snapshot; produce these files in the trusted candidate job.

Additional mandatory evidence is currently supported for SQL001–015, DBT001–005, DBTR001–003, SCH001–003, BIZ001/002, PRB001–004, CTR001 and DQ001. Other known checks remain available in normal checking but are rejected as mandatory preflight evidence until their input/coverage contract is implemented, rather than silently approving absent integration data.

## CI sequence

1. Obtain manifest/catalog artifacts from a **trusted successful baseline**, in `baseline/`. Preserve its original project and adapter metadata. Do not use the candidate itself as the baseline.
2. Build the candidate in an isolated homologation database using its actual adapter, representative data and production-compatible versions/settings. IntentFlow does not execute this build for you.
3. Preserve build results before docs generation, then run preflight in the same trusted checkout/job.
4. Make that job a required status check and make deployment depend on its success. A CLI return code alone cannot enforce a GitHub or orchestrator policy.

Example shell portion of that job, from the dbt project root:

```bash
export DBT_PARTIAL_PARSE=false  # regenerate revision metadata when restoring a dbt cache
export DBT_ENV_CUSTOM_ENV_INTENTFLOW_REVISION="$GITHUB_SHA"
dbt build --target ci
cp target/run_results.json target/build_run_results.json
cp target/manifest.json target/build_manifest.json
# When source freshness is required (dbt 1.x):
dbt source freshness --target ci
dbt docs generate --target ci
cp target/build_run_results.json target/run_results.json
cp target/build_manifest.json target/manifest.json
intentflow preflight --target target --root . --base baseline \
  --revision "$GITHUB_SHA" --require-source-freshness --fail-on high --format json > preflight.json
```

The dbt commands above execute the project's code under the CI account. Configure that account for homologation and never provide production write credentials to untrusted PRs. Preserve/upload failed build artifacts as well; a regular `intentflow check`/watch ingests their findings and rule history. If build fails, the job must remain failed even if a later diagnostic command succeeds.

`--revision` checks `metadata.env.INTENTFLOW_REVISION` in all three candidate artifacts. dbt includes custom metadata variables natively: [dbt artifacts](https://docs.getdbt.com/reference/artifacts/dbt-artifacts). These values are public artifact metadata; put **no secrets** in them. Without the flag, no commit binding is enforced. This is a consistency check for artifacts produced by trusted CI, not a cryptographic attestation or a check of the current Git worktree. SQL text comparison does not validate every YAML/macro/seed edit; regenerate artifacts from the candidate checkout.

Preserving the build manifest also avoids re-rendered runtime macros from docs being mistaken for the executed SQL. SQL rules apply to SQL models; native Python builds/tests do not imply Python static-analysis coverage. Without sources, omit both the freshness command and its required flag.

For contracts and probes, configure the store explicitly and add the ids:

```bash
intentflow config set contracts.dir contracts
intentflow preflight --target target --root . --base baseline \
  --require-check CTR001,PRB001 --postgres-env CI_DATABASE_URL
```

Supported contract formats and rule examples: [BUSINESS_RULES](BUSINESS_RULES.md). `--postgres-env` names the variable holding the URL; it is not the URL itself. DuckDB uses `--duckdb`. Other warehouses use the existing store configuration. Choose severity thresholds and warning policy deliberately; no universal policy fits all data.

Exit codes: 0 passed the configured checks/evidence; 1 blocked; 2 invalid CLI configuration. JSON preserves gaps, warnings, findings, required/completed/failed checks and resource counts. It is evidence for this candidate and configured scope, not a production-readiness certificate.

## Scope and further priorities

This covers dbt artifact/build integrity and the existing check set. It does not replay all production traffic, prove business semantics, enforce an external deployment policy, validate all cloud credentials, discover every sensitive column, or certify source timeliness. Source freshness and GX/Soda evidence must be explicitly required; otherwise preflight does not certify them. Plugins/integrations still require their own setup and validation.

Next priorities: bind external quality to the exact source-data snapshot; representative incremental/backfill/timezone and late-event fixtures; deployment integration and recovery drills on the actual warehouse. Image CVEs and production security/performance validation remain separate pending work. No preventive check can guarantee that an external service, permission or new data pattern will never change after validation.
