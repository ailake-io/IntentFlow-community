# Scope decisions

[Português](pt-BR/DECISIONS.md)

## History

1. **Initial concept** (intent-oriented data engineering framework, with its own Rust runtime, AI agents and an edge→cloud topology): built
   as a prototype (YAML contract, batch/streaming runtime, PostgreSQL/CDC/Delta/Iceberg/CouchDB/Qdrant connectors, simulated agent, UI, benchmark).
2. **Evaluation (2026-10-11):** the prototype reimplemented what DuckDB/dbt/Airflow already do better (measured: ~4.5× slower than DuckDB on the
   same aggregation). The real differentiator was governance, not the engine.
3. **Change of scope (user decision):** drop the own engine and bring together, in one place, the recurring problems of pipelines to prevent
   them before production; plug into dbt, Airflow, Kestra and local and cloud warehouses. The Rust code was **removed**.

## Current decisions

| Decision | Reason |
|---|---|
| No execution engine of its own | Do not compete with mature tools; work next to them |
| Python | Ecosystem (SQLGlot, dbt, Airflow, warehouse drivers) |
| Read-only by default; writes only through an approved, sandbox-validated and audited proposal | Safety and trust in automatic fixes |
| Connection credentials through the environment; MFA/SSO and optional AI keys in the store | New persistent secrets require `cryptography` and an available key; check `secrets status` and migrate legacy values |
| Local operation available; integrations, notifications and external AI send content when configured/triggered | See the privacy limits in the Enterprise AI documentation |
| Cloud adapters exist but were not exercised against real accounts | No credentials were provided; state the gap instead of assuming it works |
| No YAML files of its own (user decision) | Settings and policies in the store, managed through the CLI/UI; less friction and fewer files to version |
| Rule-based fixes only for what is mechanical or an informed human decision (rename, `*`, owner, `unique_key`); none edits YAML | Textual YAML edits risk duplicating the model patch (dbt fails); wait for a structural editor |
| Impact must never underestimate; precision is secondary | A false negative silently breaks production; a false positive only costs a review. Hence indirect dependency and the `opaque` fallback |
| Loading a SQLMesh project needs an explicit opt-in | The loader runs the project's code; avoids execution in CI for fork PRs |
| SSO connections validate the resolved address and connect to it | Classifying the host text had bypasses (decimal/hex IP, DNS, rebinding, redirect) |
| Optional LLM and Jev to suggest and triage; approval stays human | Rule and AI fixes go through validation bound to the content |

## Waiting on the user

- Authorization and credentials to test against real Snowflake/BigQuery/Databricks and Airflow.
- A **production** dbt project (with `catalog.json`) to measure real precision and the usefulness of findings: guide in [PILOT](PILOT.md).

## Team model (user decision: "more than one person may work on it")

| Decision | Reason |
|---|---|
| Two automatic modes: local (no users) and team (with users) | Zero friction on a notebook; safety when there is more than one person |
| One token per user, only the hash stored; viewer/engineer/admin roles | Simple, auditable, no dependency on an identity provider |
| Author/approver separation per team policy; extra review requires another person even in solo mode | The extra review policy cannot be bypassed by having a single user |
| Outside loopback requires users and TLS (or a declared proxy) | Do not expose a tool that can change files without authentication |
| SQLite store (default) **or** PostgreSQL, one per project | Zero-config SQLite for solo use; PostgreSQL for production (3× the throughput with 100 users, no single writer). Same code through a thin layer (`_PgConn`, `ON CONFLICT`); no ORM |
| TOTP MFA, OIDC SSO and per-project roles (later review, asked by the user) | Remove the limits of the first version; verified against a simulated IdP, Keycloak and real stores |

## Open core and language (2026-10-14, user decision)

| Decision | Reason |
|---|---|
| Apache-2.0 core + IntentFlow Enterprise (commercial license, separate package through an entry point) | Adoption through the free core; revenue from what serves a company (SSO, per-project governance, fixes, AI, corporate integrations) |
| Token login, roles and MFA stay in the core | Security is not a paid feature |
| Enterprise license checked offline (Ed25519) | No calling home; works on closed networks |
| English as the main language; Portuguese through a catalog (`tr()` in Python, `T()` in the UI) | International reach without losing the Brazilian audience; finding ids do not depend on the language |
