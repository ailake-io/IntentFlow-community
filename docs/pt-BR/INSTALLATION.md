# Instalar o IntentFlow Community

[English](../INSTALLATION.md)

A distribuição Python se chama `intentdataflow` (import e comando: `intentflow`); o repositório é `IntentFlow-community`. Exige Python >=3.11; a CI atual cobre 3.11, 3.13 e 3.14. O Dockerfile padrão usa Python 3.12. Veja [VALIDATION_STATUS](VALIDATION_STATUS.md) para cobertura medida e pendências.

## Pelo código-fonte

No checkout Community:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[secrets]'
intentflow --version
intentflow ui
```

Abra http://127.0.0.1:8787. Para exemplos dbt e testes, instale também o extra dev:

```bash
python -m pip install -e '.[dev,secrets]'
```

A instalação editável usa este checkout.

## Pelo wheel de uma release

Obtenha o wheel pela origem aprovada das releases Community. Em um venv ativado, instale o arquivo real; o padrão abaixo pressupõe um diretório com uma única versão aprovada:

```bash
python -m pip install /caminho/wheels/intentflow-*.whl
intentflow --version
```

Instalar pelo nome (`python -m pip install intentdataflow`) exige um índice configurado contendo a distribuição/versão pretendida. Existir uma release no GitHub não comprova publicação no PyPI. O teste local de wheels não cobre acesso ao índice privado de clientes.

## Dependências opcionais

No checkout use `python -m pip install -e '.[EXTRA]'`, substituindo EXTRA pelos extras necessários. Exemplos de índice nos demais guias pressupõem que a origem confiável tenha a distribuição pretendida.

| Extra | Função |
|---|---|
| `secrets` | cryptography para segredos MFA/SSO persistentes; necessário para essas gravações |
| `postgres` | armazém PostgreSQL e sondas |
| `dev` | pytest, dbt-core, dbt-duckdb e moto para desenvolvimento |
| `mysql`, `warehouses` | PyMySQL, Trino e ClickHouse |
| `snowflake`, `bigquery`, `databricks` | conectores de warehouse em nuvem; cobertura com contas reais é limitada |
| `kafka`, `aws`, `lakehouse`, `lint`, `jev` | integrações específicas; veja pyproject.toml e INTEGRATIONS |

Segredos persistentes em PostgreSQL também exigem `INTENTFLOW_SECRET_KEY`. SQLite em arquivo usa a chave de ambiente ou um arquivo `.key` local. Veja [DEPLOYMENT](DEPLOYMENT.md).

## uv e Docker

O `uv.lock` Community fixa dependências, incluindo extras opcionais. O Dockerfile instala só `secrets` e `postgres` com `uv sync --locked`. A imagem contém apenas Community e ainda tem achados do SO pendentes; veja [VALIDATION_STATUS](VALIDATION_STATUS.md). Mantenha pyproject.toml e lock consistentes.

## Funções de equipe

SSO, papéis por projeto, correções governadas, sugestões de IA e sinks corporativos vêm no mesmo pacote (antes Enterprise), sem licença. SSO precisa do extra `secrets`. Aplicar/recuperar correções exige Linux; a validação local do dbt também precisa de Docker e da imagem do sandbox, veja [VALIDATION_RECOVERY](VALIDATION_RECOVERY.md) e [IA](AI.md).
