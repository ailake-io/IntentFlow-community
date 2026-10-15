# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [0.3.3] - 2026-10-15

### Fixed
- PostgreSQL pool: after the server was killed or restarted, requests kept drawing dead pooled connections (`server closed the connection unexpectedly`, empty replies from the UI) until the pool drained. Idle connections are now pinged on checkout and dropped when dead.

## [0.3.2] - 2026-10-15

- New check `SCH003`: when base or head has no `catalog.json`, the PR schema diff said nothing (a rename went unseen). It now reports that the diff is blind and how to fix it.
- `intentflow ui` refuses to start with a clear message when the TLS certificate/key cannot be read (before binding the port).

## [0.3.1] - 2026-10-15

### Fixed
- `watch` no longer exits when the database connection dies (restart, failover): it reopens the store on the next cycle.

### Docs
- Container deployment checklist from a production-like run.

## [0.3.0] - 2026-10-14

### Security
- `/api/settings`: connection keys (`notify.sinks`, `*.url`, `*_env`, …) require a global admin; project admins keep `policy.`, `scan.`, `watch.`, `lineage.` and `dbt.`.

### Added
- English as the main language, with a Portuguese translation of the CLI, UI, findings and docs (`INTENTFLOW_LANG`, browser language, UI selector).
- Open core: Apache-2.0 license, extension point (`intentflow.extensions`), [EDITIONS](docs/EDITIONS.md); `intentflow --version`.
- `intentflow risks`: what can break in the whole process (visibility, fragility, external jobs, process), `--fail-on-high`.
- External jobs (Spark, Python, SQL outside the project) in the impact: OpenLineage and `intentflow.lineage.json`; checks OL001–OL004.
- Static analysis of Python jobs (PySpark and pandas) and parameterized/multi-statement SQL.
- Transitive `lineage --impact`; blast radius in SCH001.
- Pilot guide and pilot offer ([PILOT](docs/PILOT.md), [PILOT_OFFER](docs/PILOT_OFFER.md)).

### Changed
- SSO, per-project roles/scopes, multiple projects per server, governed fixes, AI suggestions and corporate outputs moved to the separate
  IntentFlow Enterprise package. Token login, roles and MFA stay in the core.
- The default project id is now `main` (was `principal`). Match reasons are `ambiguous`/`missing`; `risks` areas and severities use English keys.
- Finding ids are computed from the English rendering of the title: findings from older stores are resolved and reopened once after upgrading.
- `notify --type` accepts the types of the loaded extensions.

### Security
- `sql_file`/`py_file` confined to the declaration directory; linear-time parameter rendering (ReDoS); evaluation budget for Python analysis.
- OIDC: resolved-address validation, no redirects, local IdP only through loopback or opt-in.

## [0.2.0] - 2026-10-13

First version: preventive checks, column lineage, data diff, orchestrator collection, incidents, notifications, governed fixes, UI and team mode.
