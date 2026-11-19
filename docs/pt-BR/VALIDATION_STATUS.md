# Estado de validação — 19/11/2026

[English](../VALIDATION_STATUS.md)

Este documento registra evidências das rodadas de novembro. Números abaixo pertencem às configurações descritas; testes ignorados não contam como aprovados. Relatórios datados de outubro continuam como histórico.

## Código, instalação e CI

- Community no checkout: `0.3.5`; Enterprise: `0.3.4`, exigindo `intentflow>=0.3.5` e `cryptography`. Mudanças posteriores às releases ficam em `Unreleased`.
- Community funciona sozinho; Enterprise é plugin instalado no mesmo ambiente Python e depende de uma licença válida para habilitar recursos.
- Wheels dos dois checkouts foram construídos e instalados juntos em um venv limpo com Python 3.14.6; `pip check` e `intentflow --version` passaram. Isso não verifica acesso ao índice privado de clientes.
- CI em `main`: Python 3.11, 3.13 e 3.14. Actions usam Node 24 (`checkout@v6`, `setup-python@v6`; Community também `setup-java@v5`).
- Rodadas locais com Python 3.13.11 e 3.14.6: Community **287 aprovados / 44 ignorados**; Enterprise **120 aprovados / 16 ignorados**. São rodadas distintas, com extras instalados diferentes dos usados nas imagens.
- Rodada Enterprise em PostgreSQL 16: **120 aprovados / 16 ignorados**, com `INTENTFLOW_SECRET_KEY` descartável.
- CI Community também passou com PostgreSQL/Mailpit, Spark 3.5 e `dbt-ol` (estes dois últimos em Python 3.12, em jobs separados). Compatibilidade do núcleo com 3.14 não garante compatibilidade de runtimes externos com 3.14.
- CI Enterprise passa nas três versões; Keycloak e sandbox não são preparados por esse workflow. Keycloak real foi testado separadamente: **8 aprovados** após alinhar as asserções de idioma.

## Interface Enterprise e recuperação

No Chrome headless, usando cópia temporária de `dbt_shop` e SQLite: criar proposta → aprovar → validar build no container → aplicar → reverter passou. A reversão restaurou os bytes originais do SQL; nenhum erro JavaScript foi capturado. A licença/chave eram descartáveis, aceitas apenas pelo processo de teste.

O backup SQLite foi reaberto em outro Store: proposta, achados e auditoria coincidiram com o banco original. Isso não substitui restauração no PostgreSQL e na infraestrutura de destino. O teste de expansão de `SELECT *` validada por build no sandbox real também passou.

A aplicação em vários arquivos usa journal e recuperação; o rename é atômico **por arquivo**. Leitores externos que não respeitam o lock podem observar um lote parcial. A limitação de validações simultâneas do sandbox vale **por processo Python**, não como um limite global entre processos do host.

## Imagem auditada

Imagem `intentflow-community:local-review`, ID `sha256:6ceb078bc1a2d81e94067d3864cc73b1193000cdb9f2f0b534f440a137cef630`. [Snapshot Trivy da rodada](../security/scan-2026-11-19.json).

- Base Debian: **50 ocorrências HIGH / 13 CVEs distintos / 0 CRITICAL** no filtro usado.
- **3 ocorrências** tinham correção indicada; as demais não tinham versão corrigida indicada para essa base no banco consultado.
- Pacotes Python da imagem: **0 HIGH/CRITICAL**; severidades menores ficaram fora desta consulta.
- Os resultados indicam pacotes potencialmente afetados, não exploração comprovada do aplicativo. A auditoria de uma imagem não cobre todas as imagens customizadas Enterprise nem todos os extras opcionais.
- Tratar correções disponíveis, reavaliar os demais achados e repetir o scan antes de produção. A descoberta foi registrada; os CVEs não foram corrigidos por esta atualização de documentação.

## Laboratório dbt local

O projeto separado `../intentflow-dbt-tests` foi criado nesta máquina, fora dos dois repositórios. Não faz parte dos wheels nem de um clone novo. Tem 3 seeds, 7 modelos, 16 testes dbt, dados fictícios `example.invalid` e Python 3.13.11. Consulte seu README.

- `baseline`: 26 recursos dbt concluídos, sem falha; IntentFlow detectou SQL001, SQL003, DBT002, DBT003 e DBT004; gate `--fail-on high` retornou 1 como esperado.
- `data-error`: uma falha de unicidade esperada, três dependentes pulados; IntentFlow detectou DBTR001 adicional.
- Baseline foi reexecutado ao terminar para restaurar os dados. O script usa `--full-refresh`; não o use em um banco de produção.
- `dbt docs generate` substituiu `run_results.json` na versão utilizada. O roteiro preserva o resultado do build e o restaura depois da geração do catálogo.
- No código atual, DBTR003 acompanha erro de modelo. Uma falha isolada de teste pode pular descendentes sem gerar DBTR003; não foi corrigido código para alterar esse comportamento.

## Ainda pendente

Piloto com pipeline e dados reais de usuário; acesso ao canal privado de distribuição; provedores e integrações externas sem conta de teste; tratamento das vulnerabilidades das imagens; verificação no ambiente de produção. O catálogo [INTEGRATIONS](INTEGRATIONS.md) do Community contém resultados de rodadas anteriores, não promete cobertura de todos os serviços a cada execução da suíte.
