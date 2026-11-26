# Install IntentFlow Community

[Português](pt-BR/INSTALLATION.md)

The Python distribution is named `intentdataflow` (import and command: `intentflow`); the repository is `IntentFlow-community`. Requires Python >=3.11; current CI covers 3.11, 3.13 and 3.14. The standard Dockerfile uses Python 3.12. See [VALIDATION_STATUS](VALIDATION_STATUS.md) for measured coverage and pending work.

## From source

From the Community checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[secrets]'
intentflow --version
intentflow ui
```

Open http://127.0.0.1:8787. For dbt examples and tests also install the dev extra:

```bash
python -m pip install -e '.[dev,secrets]'
```

The editable source install uses this checkout.

## From a release wheel

Download the wheel from the approved Community release source. In an activated venv, install the real file; the glob below assumes a directory with exactly one approved version:

```bash
python -m pip install /path/to/wheels/intentflow-*.whl
intentflow --version
```

Installing by name (`python -m pip install intentdataflow`) requires a configured index containing the intended distribution/version. The existence of a GitHub release does not establish publication on PyPI. Customer/private-index access is not covered by local wheel tests.

## Optional dependencies

From the checkout use `python -m pip install -e '.[EXTRA]'`, replacing EXTRA with the needed extras. Package-index examples in other guides assume that the trusted index has the intended distribution.

| Extra | Purpose |
|---|---|
| `secrets` | cryptography for persistent MFA/SSO secrets; required for those writes |
| `postgres` | PostgreSQL store and probes |
| `dev` | pytest, dbt-core, dbt-duckdb, moto for development |
| `mysql`, `warehouses` | PyMySQL, Trino and ClickHouse |
| `snowflake`, `bigquery`, `databricks` | cloud warehouse connectors; real-account coverage is limited |
| `kafka`, `aws`, `lakehouse`, `lint`, `jev` | specialized integrations; see pyproject.toml and INTEGRATIONS |

PostgreSQL persistent secrets also require `INTENTFLOW_SECRET_KEY`. SQLite file stores use an environment key or a local `.key` file. See [DEPLOYMENT](DEPLOYMENT.md).

## uv and Docker

The Community `uv.lock` fixes dependencies including optional extras. The Dockerfile installs only `secrets` and `postgres` with `uv sync --locked`. It contains Community only, and image OS findings remain pending; see [VALIDATION_STATUS](VALIDATION_STATUS.md). Keep pyproject.toml and the lock consistent.

## Team features

SSO, per-project roles, governed fixes, AI suggestions and corporate sinks ship in the same package (formerly Enterprise); no license is needed. SSO needs the `secrets` extra. Governed apply/recovery requires Linux; local dbt validation also needs Docker and the sandbox image, see [VALIDATION_RECOVERY](VALIDATION_RECOVERY.md) and [AI](AI.md).
