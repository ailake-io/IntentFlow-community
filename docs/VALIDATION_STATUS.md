# Validation status — 2026-11-19

[Português](pt-BR/VALIDATION_STATUS.md)

This page records November validation evidence. Counts refer to the described configurations; skipped tests are not passing tests. Dated October reports remain historical records.

## Code, installation and CI

- Checkout versions: Community `0.3.5`, Enterprise `0.3.4`, requiring `intentflow>=0.3.5` and `cryptography`. Changes after the release artifacts belong in `Unreleased`.
- Community runs independently. Enterprise is a plugin in the same Python environment, with a valid license required to enable features.
- Both checkout wheels were built and installed together in a clean Python 3.14.6 venv; `pip check` and `intentflow --version` passed. Customer private-index access was not verified.
- Main CI covers Python 3.11, 3.13 and 3.14. Actions use Node 24 (`checkout@v6`, `setup-python@v6`; Community also `setup-java@v5`).
- Separate local Python 3.13.11 and 3.14.6 runs: Community **287 passed / 44 skipped**; Enterprise **120 passed / 16 skipped**. Installed extras differ from the container images.
- Enterprise PostgreSQL 16 run: **120 passed / 16 skipped**, with a disposable `INTENTFLOW_SECRET_KEY`.
- Community CI also passed PostgreSQL/Mailpit, Spark 3.5 and `dbt-ol`; the latter two use separate Python 3.12 jobs. Core compatibility with 3.14 does not establish compatibility of external runtimes with 3.14.
- Enterprise CI passes on all three versions; its workflow does not provision Keycloak or the dbt sandbox. Real Keycloak was tested separately: **8 passed**, after fixing the assertions' language.

## Enterprise browser flow and recovery

In headless Chrome, using a temporary copy of `dbt_shop` and SQLite, create proposal → approve → validate build in the container → apply → roll back passed. Rollback restored the original SQL bytes; no JavaScript errors were captured. The license/key were disposable and trusted only within the test process.

A SQLite backup reopened in another Store retained the same proposal, findings and audit entries. This does not replace restoration on the target PostgreSQL/infrastructure. The real sandbox build validation for expanding `SELECT *` also passed.

Multi-file apply uses a recovery journal and atomic renames **per file**. External readers ignoring the lock may observe a partial batch. The sandbox's concurrency semaphore is **per Python process**, not a global limit across host processes.

## Audited image

Image `intentflow-community:local-review`, ID `sha256:6ceb078bc1a2d81e94067d3864cc73b1193000cdb9f2f0b534f440a137cef630`. [Trivy scan snapshot](security/scan-2026-11-19.json).

- Debian base: **50 HIGH occurrences / 13 distinct CVEs / 0 CRITICAL** in the selected filter.
- **3 occurrences** had a fix listed; the remaining findings had no fixed version listed for that base in the consulted database.
- Image Python packages: **0 HIGH/CRITICAL**; lower severities were outside this query.
- These are potentially affected packages, not demonstrated exploitation of the application. One image scan does not cover all custom Enterprise images or optional extras.
- Apply available fixes, assess remaining findings and scan again before production. Documentation records the findings; it does not remediate these CVEs.

## Local dbt lab

The separate project `../intentflow-dbt-tests` was created on this machine outside both repositories. It is not included in wheels or a new clone. It has 3 seeds, 7 models, 16 dbt tests, fictitious `example.invalid` data and Python 3.13.11. See its own README.

- `baseline`: 26 dbt resources completed with no failure; IntentFlow found SQL001, SQL003, DBT002, DBT003 and DBT004; the `--fail-on high` gate returned the expected exit code 1.
- `data-error`: one expected uniqueness failure, three skipped dependents; IntentFlow also found DBTR001.
- Baseline was rerun at the end to restore data. The script uses `--full-refresh`; do not target a production database.
- `dbt docs generate` overwrote `run_results.json` in the installed dbt version. The script preserves the build result and restores it after generating the catalog.
- The initial lab run exposed a DBTR003 gap after failing tests. An Unreleased correction on develop now reports these skipped models as well as those following model errors.

## Still pending

A pilot on a user's real pipeline/data; customer private-distribution access; external providers/integrations without test accounts; image vulnerability remediation; target production-environment validation. Community's [INTEGRATIONS](INTEGRATIONS.md) catalog includes previous test rounds; it does not promise every service is exercised on every suite run.

## Unreleased corrections from the real-data pilot (develop)

A separate local project, `../intentflow-retail-pilot`, downloads the full UCI Online Retail dataset (541,909 rows; CC BY 4.0). It preserves the source XLSX and checksums and keeps natural data characteristics separate from injected failures. Before the preventive additions, the correction run repeated all Community scenarios, Enterprise browser sandbox validation, apply, byte-for-byte rollback and SQLite backup reopening. The dbt baseline completed with 16 successful builds, 22 passed tests and 4 source-quality warnings, without failures or skips. See the lab README and reports for assumptions and measured results; this is not a production pilot.

- Community: DBTR003 now reports skipped models after failed/errored tests even when no model itself has status error. Four regression cases reproduce failed tests and rule out warning/pass false alarms. Local related tests: 28 passed.
- Enterprise: the isolated build uses dbt indirect-selection buildable, avoiding tests that reference unbuilt siblings. The real-container regression also verifies that a failing in-scope test is still refused. Local validation/recovery tests: 25 passed. Selection scope is recorded in results; sibling tests outside that scope are not covered by sandbox validation.
- The original Debian image vulnerabilities remain unresolved; these code fixes do not remediate the base OS. These checkout fixes do not replace previously published release wheels.

## Preventive validation and business rules (Unreleased)

The develop checkout adds the preflight full-build evidence gate and the rules CLI/UI backed by tagged native dbt tests. See [PREFLIGHT](PREFLIGHT.md) and [BUSINESS_RULES](BUSINESS_RULES.md) for scope and remaining gaps. Automated regressions cover incomplete/stale/mixed artifacts, disabled/errored checks, caught query failures, history warm-up, strict contracts, seven real-dbt business rules, source freshness with the real dbt command, and recognized/mapped/current external-quality fixtures. Partial Enterprise sandbox validation still does not replace a full target-warehouse build.

The expanded retail baseline has 6 seeds, 10 models and 30 tests: 26 pass, 4 natural-data warnings and no failures/skips. Five business rules and their browser UI/history passed. The full gate still blocks preserved raw duplicate candidates; the positive preflight case uses an explicit PRB001 exception only in a separate QA store, retaining full fact-key tests.

## Baseline change review — 2026-11-20 (Unreleased)

[Change review](CHANGE_REVIEW.md) compares existing tests, schema/interfaces, native contracts, incremental keys and freshness policies. Preflight blocks removed/changed controls even when remaining tests pass. Optional strict model-code review and expected deployed-revision metadata are supported; acknowledgements require exact ids/reasons and do not waive other validation.

- Local full Community suite: **415 passed / 44 skipped**. A subsequently added native-dbt regression was verified in the separate **37 passed** comparison suite; these overlapping counts must not be added. CI covers the final collection.
- Paired Enterprise suite: **129 passed / 10 skipped**, including the local Docker sandbox cases. Keycloak (8), GitLab (1) and Ollama (1) were not configured.
- Native dbt acceptance: baseline passes; removed rule, vacuous `where 1=0` rule and reduced embedded unit-test expectation each pass dbt but are blocked by preflight. Exact intentional-change acknowledgements pass; the fixture is restored afterwards.
- Portuguese Chrome change-review page: correct ids/review state and no JavaScript errors. Real artifacts include a native unit-test resource.
- Synthetic 5,000-model metadata comparison: **0.486 s**, bounded 100-resource impact preview with truncation explicitly reported. This is not a production warehouse benchmark.

The acceptance fixture, logs, JSON and screenshot live separately in `../intentflow-retail-pilot/reports/change-review/` and `change-review-results.json`; they are not packaged in either repository. Consumer/runtime/data-snapshot limits are explicit in the guide. Image CVEs and target-warehouse deployment/recovery validation remain pending.
