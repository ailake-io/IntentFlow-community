# Instalar o IntentFlow Community

[English](../INSTALLATION.md)

A distribuição Python se chama `intentflow`; o repositório público é `IntentFlow-community`. Community funciona sem Enterprise. Exige Python >=3.11; a CI atual cobre 3.11, 3.13 e 3.14. O Dockerfile padrão usa Python 3.12. Veja [VALIDATION_STATUS](VALIDATION_STATUS.md) para cobertura medida e pendências.

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

A instalação editável usa este checkout. Ela não instala o pacote privado Enterprise.

## Pelo wheel de uma release

Obtenha o wheel pela origem aprovada das releases Community. Em um venv ativado, instale o arquivo real; o padrão abaixo pressupõe um diretório com uma única versão aprovada:

```bash
python -m pip install /caminho/wheels/intentflow-*.whl
intentflow --version
```

Instalar pelo nome (`python -m pip install intentflow`) exige um índice configurado contendo a distribuição/versão pretendida. Existir uma release no GitHub não comprova publicação no PyPI. O teste local de wheels não cobre acesso ao índice privado de clientes.

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

Enterprise tem um lock próprio com override uv para o checkout irmão `../IntentFlow-community`. Esse override vale para desenvolvimento pelo código e não muda os metadados publicados de dependências.

## Adicionar Enterprise

Instale a extensão privada autorizada no mesmo venv; o checkout Enterprise atual exige `intentflow>=0.3.9` e `cryptography`. Exemplo com checkouts lado a lado:

```bash
python -m pip install -e . -e ../intentflow-enterprise
export INTENTFLOW_LICENSE_FILE=/caminho/licenca.txt
intentflow --help
```

Uma licença válida habilita a extensão. Reabra o processo após instalar/atualizar o pacote. A licença é verificada offline e o resultado em cache é atualizado aproximadamente a cada 60 segundos. Aplicação/recuperação governada exige Linux atualmente; validação dbt local também precisa de Docker e da imagem sandbox separada.

O [guia de instalação Enterprise](https://github.com/ailake-io/intentflow-enterprise/blob/main/docs/pt-BR/INSTALLATION.md) exige acesso ao repositório privado.
