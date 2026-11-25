# Install IntentFlow Community

[Português](pt-BR/INSTALLATION.md)

The Python distribution is named `intentflow`; the public repository is `IntentFlow-community`. Community runs without Enterprise. Requires Python >=3.11; current CI covers 3.11, 3.13 and 3.14. The standard Dockerfile uses Python 3.12. See [VALIDATION_STATUS](VALIDATION_STATUS.md) for measured coverage and pending work.

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

The editable source install uses this checkout. It does not install the private Enterprise package.

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

Enterprise has its own lock with a uv source override to sibling `../IntentFlow-community`. This source override is for source development and does not change published dependency metadata.

## Add Enterprise

Install the authorized private extension into the same venv; the current Enterprise checkout requires `intentdataflow>=0.3.10` and `cryptography`. Source example with sibling checkouts:

```bash
python -m pip install -e . -e ../intentflow-enterprise
export INTENTFLOW_LICENSE_FILE=/path/to/license.txt
intentflow --help
```

A valid license enables the extension. Reopen the process after installing/upgrading the package. The license is checked offline and its cached result is refreshed approximately every 60 seconds. Governed apply/recovery currently requires Linux; local dbt validation also needs Docker and the separate sandbox image.

The [Enterprise installation guide](https://github.com/ailake-io/intentflow-enterprise/blob/main/docs/INSTALLATION.md) requires private repository access.
