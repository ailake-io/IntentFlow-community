# Editions

[Português](pt-BR/EDITIONS.md)

Since 0.4.0 there is a single edition. The features of the former private Enterprise package (SSO/OIDC, per-project roles,
governed fixes with sandbox validation, AI suggestions and Jev triage, Jira/PagerDuty/Opsgenie sinks) are part of
`intentdataflow` under Apache-2.0, with no license key.

- Install: `pip install intentdataflow` (`[secrets]` for SSO and encrypted secrets).
- `INTENTFLOW_LICENSE` / `INTENTFLOW_LICENSE_FILE` are no longer read.
- Do not install the old `intentflow-enterprise` package next to 0.4.0: it would register the same features twice. Uninstall it.
