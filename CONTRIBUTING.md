# Contributing

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev,secrets]'
.venv/bin/pytest -q            # integration tests (PostgreSQL, Spark, dbt-ol) are optional; see .github/workflows/ci.yml
```

- One PR per change, with a test that fails without it.
- No YAML of its own and no execution engine: IntentFlow reads what already exists (see [docs/DECISIONS.md](docs/DECISIONS.md)).
- User-facing text goes through `tr("English text {placeholder}", placeholder=...)` (Python) or `T("English text")` (UI), with the Portuguese entry in
  `src/intentflow/locale/pt.json`; `tests/test_i18n.py` fails on a missing translation.
- Sign your commits with the DCO (`git commit -s`): you state that you can contribute the code under the Apache-2.0 license
  ([developercertificate.org](https://developercertificate.org/)). A `dco` workflow fails a pull request that has a commit without
  `Signed-off-by`; `main` also requires the `unit` and `integration` checks.
- What is core and what is the commercial edition: [docs/EDITIONS.md](docs/EDITIONS.md).

Contribuições em português são bem-vindas: abra a issue ou o PR no idioma que preferir.
