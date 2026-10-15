# Editions: open core

[Português](pt-BR/EDITIONS.md)

The core is **open source (Apache-2.0)**. A commercial edition (**IntentFlow Enterprise**) adds what only makes sense
for several people or a company: identity integration, per-project governance, governed fixes, AI and corporate integrations.

**The rule:** what an engineer or a small team needs to keep production from breaking is free and stays free. Lineage, checks,
diff and security (login, MFA, encryption at rest) will never be paid. What serves a company's governance and integrations is paid.

| Open source (Apache-2.0) | Enterprise (commercial license) |
|---|---|
| `check`, `gate`, `lineage` (impact, external jobs), `diff`, `risks` | SSO with OpenID Connect (Keycloak, Entra ID, Okta, Google…), group → role mapping, back-channel logout |
| Every check (SQL, dbt, schema, PII, orchestration, OpenLineage, cluster) | Per-project roles and write scopes per folder/model |
| UI and API, English and Portuguese | Several projects on one server (one store per project) |
| Team login with tokens, roles (viewer/engineer/admin), MFA (TOTP) | Governed fixes: proposal → approval → sandbox validation → apply → rollback (`fix`) |
| Secrets encrypted at rest, TLS, audit trail | AI fix suggestions and Jev triage/risk gate (`fix-ai`, `llm`) |
| SQLite or PostgreSQL store | Corporate outputs: PagerDuty, Opsgenie, Jira, Teams, GitLab, Bitbucket, Azure DevOps |
| Outputs: Slack, webhook, e-mail, GitHub pull request comments | Support with response times and assisted rollout |
| Collection from Airflow, Kestra, dbt, warehouses, OpenLineage | |

## How it fits together

- The Enterprise package (`intentflow-enterprise`, private repository) registers itself under the `intentflow.extensions`
  entry point. The core works without it and never imports it.
- The license is an Ed25519-signed token checked offline (`INTENTFLOW_LICENSE` or `INTENTFLOW_LICENSE_FILE`); nothing calls home.
  Without a valid license the extension stays loaded but turns no feature on.
- `/api/me` reports the active features; the UI hides the Enterprise screens when they are off.

## Repositories

- **Core (this repository, public):** [ailake-io/IntentFlow-community](https://github.com/ailake-io/IntentFlow-community). It started from a
  fresh history, so it never contained Enterprise code.
- **Enterprise (private):** available to customers under a commercial agreement.

## Contributions

Contributions to the core use the DCO (`git commit -s`), see [CONTRIBUTING](../CONTRIBUTING.md). Apache-2.0 already allows using
core code inside Enterprise, so no CLA is needed.
