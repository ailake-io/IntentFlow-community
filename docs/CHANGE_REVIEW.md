# Review changes before production

[Português](pt-BR/CHANGE_REVIEW.md)

Unreleased, available from the source checkout. `intentflow changes` compares native dbt manifests/catalogs from an approved baseline and a candidate. The **Changes before production** page shows the same comparison. `preflight` requires review of changes to existing controls/interfaces even when all remaining dbt tests pass.

## Covered changes

| Change | Default treatment |
|---|---|
| Existing data/unit test removed or disabled | Review required |
| Test SQL, generic arguments, severity, failure thresholds, filter, limit or other execution configuration changed | Review required |
| Embedded unit-test inputs, expectations or overrides changed | Review required |
| Declared test macro dependencies changed, including transitive dependencies | Review required |
| `business_rule` tag removed | Review required |
| Existing model/seed/snapshot/source removed or disabled; physical alias/identifier changed | Review required |
| Column removed or type changed, even without known consumers | Review required |
| Enforced native model contract disabled; declared constraints removed/replaced | Review required |
| Incremental materialization, unique key or strategy changed | Review required |
| Existing source freshness thresholds, filter or reference clock changed | Review required |
| Resource, test or column added | Informational; strict consumers may still break |
| Model SQL/Python or declared macro dependencies changed | Informational; review required with `--review-model-logic` |

Arbitrary changed SQL cannot reliably be classified as stronger or weaker. Both require review. Generic test argument changes can change the generated dbt id; comparison uses a stable test identity and reports ambiguous changed sets without guessing a match. Comments, SQL layout, descriptive metadata and environment database/schema differences are ignored. Literal contents and Python indentation remain significant. Pure `config()` calls are compared through effective configuration where covered above. Runtime-rendered partition filters use their unrendered template when available. Known dbt test execution helpers are included consistently across docs/build artifact phases; changes to their bodies still require review.

The impact preview follows the union of declared baseline/candidate dependencies, including exposures and semantic resources. It includes at most 100 resources per change and reports truncation. The UI displays at most 100 changes; CLI JSON includes every change. Missing catalog columns produce explicit gaps and block preflight. Missing artifact files are errors, rather than evidence of no changes.

## Local and CI usage

First produce a complete successful candidate build/catalog as described in [PREFLIGHT](PREFLIGHT.md). Preserve the approved deployed version's manifest/catalog in a separate directory. Do not recreate the baseline from the PR. Identical resolved candidate/baseline directories are rejected; copied or modified files still require trusted artifact storage.

```bash
intentflow changes --target target --base baseline --format json > changes.json
# A diagnostic preview returns 0 even if review is needed. Use this for a blocking preview:
intentflow changes --target target --base baseline --fail-on-review
# Require review of model code and declared macro changes too:
intentflow preflight --target target --root . --base baseline --review-model-logic
```

`changes` exit codes: 0 comparison completed (or no outstanding reviews/gaps with `--fail-on-review`); 1 outstanding reviews/gaps with that flag; 2 invalid configuration/artifacts. A preview does not execute checks or verify a full build. `preflight` remains the promotion gate.

An intentional change can be acknowledged only by its **exact current id and a nonempty reason**:

```bash
intentflow preflight --target target --root . --base baseline \
  --accept-change 'CHG-<id-from-report>=Reviewed migration; downstream owners validated replacement'
```

Repeat the flag for separate changes. Wildcards, unknown/old ids, duplicate ids and blank reasons are rejected. The id binds the reported before/after transition; it is not a full-commit approval. Reasons appear in JSON and the store audit. They do not waive failed builds, skipped tests, incomplete catalogs, unavailable checks or other findings. The UI/API are read-only for this review; there is no HTTP approval endpoint.

An acknowledgement is a CLI caller's claim, not a verified peer approval. Require protected CI configuration, peer review and trusted artifact retention to govern who may supply it. Untrusted PRs must not control the baseline or their own required-check policy. Enterprise's proposal approval workflow is separate and does not automatically approve these changes.

Optionally bind baseline metadata to the expected deployed revision:

```bash
intentflow changes --target target --base baseline --base-revision "$DEPLOYED_SHA"
intentflow preflight --target target --root . --base baseline \
  --base-revision "$DEPLOYED_SHA" --revision "$CANDIDATE_SHA"
```

Both baseline manifest and catalog must contain the exact native `metadata.env.INTENTFLOW_REVISION`, produced with `DBT_ENV_CUSTOM_ENV_INTENTFLOW_REVISION` during baseline generation. This checks metadata consistency, not cryptographic authenticity. Candidate `--revision` keeps its existing full-artifact checks. For the UI, save `dbt.target`, `dbt.base_target` and optionally `dbt.base_revision` in the selected project store.

## Limits and complementary validation

The comparison reads metadata without executing project code. It cannot discover unregistered consumers, approve business semantics, prove runtime macro dispatch or dynamic Python behavior, or bind every runtime variable, external fixture/file and source-data snapshot. Regenerate artifacts in trusted CI after source changes; use pinned dependencies and fixtures. The model-code strict flag covers recorded SQL/Python and declared macro dependencies, not every possible execution configuration.

Keep full target-warehouse builds and business assertions, exact or sampled data diff with an explicit sampling limit, incremental retry/backfill/late-event scenarios and deployment/rollback drills. An additive column can break a strict consumer. A passing comparison cannot predict new production data or external service failures. Source freshness and external quality are mandatory only when explicitly required in preflight. Container image vulnerabilities remain separate remediation work.
