# Decisões de escopo

[English](../DECISIONS.md)

## Histórico

1. **Conceito inicial** (framework de engenharia de dados orientado a intenção, com runtime próprio em Rust, agentes de IA e topologia edge→cloud): construído como protótipo (contrato YAML, runtime batch/streaming, conectores PostgreSQL/CDC/Delta/Iceberg/CouchDB/Qdrant, agente simulado, UI, benchmark).
2. **Avaliação (11/10/2026):** o protótipo reimplementava o que DuckDB/dbt/Airflow já fazem melhor (medido: ~4,5× mais lento que DuckDB na mesma agregação). O diferencial real estava na governança, não no motor.
3. **Mudança de escopo (decisão do usuário):** abandonar o motor próprio e unir, em um único lugar, os problemas recorrentes de pipelines para preveni-los antes da produção; plugar em dbt, Airflow, Kestra e warehouses locais e de nuvem. O código Rust foi **removido**.

## Decisões vigentes

| Decisão | Motivo |
|---|---|
| Sem motor de execução próprio | Evitar competir com ferramentas maduras; operar ao lado delas |
| Python | Ecossistema (SQLGlot, dbt, Airflow, drivers de warehouse) |
| Leitura por padrão; escrita só por proposta aprovada, validada em sandbox e auditada | Segurança e confiança em correções automáticas |
| Credenciais de conexão por ambiente; MFA/SSO e chaves opcionais de IA no armazém | Novos segredos persistentes exigem `cryptography` e chave disponível; conferir `secrets status` e migrar valores legados |
| Operação local disponível; integrações, notificações e IA externa enviam conteúdo quando configuradas/acionadas | Ver os limites de privacidade na documentação de IA do Enterprise; nesta revisão só foram usados dados sintéticos e recursos locais |
| Adaptadores de nuvem existem mas não foram exercitados contra contas reais | Não há credenciais fornecidas; declarar a lacuna em vez de supor que funciona |
| Sem arquivos YAML próprios (decisão do usuário) | Configuração e políticas no armazém, geridas por CLI/UI; reduz atrito e arquivos a versionar |
| Correções por regra só para o que é mecânico ou decisão humana informada (rename, `*`, dono, `unique_key`); nenhuma edita YAML | Edição textual de YAML corre o risco de duplicar o patch do modelo (o dbt falha); fica para quando houver um editor estrutural |
| Impacto nunca pode subestimar; precisão é secundária | Falso negativo quebra produção calado; falso positivo só custa uma revisão. Por isso a dependência indireta e o fallback `opaque` |
| Carregar projeto SQLMesh exige opt-in explícito | O loader executa o código do projeto; evita execução em CI de PR de fork |
| Conexões do SSO validam o endereço resolvido e conectam nele | Classificar o texto do host tinha bypass (IP decimal/hex, DNS, rebinding, redirect) |
| LLM e Jev opcionais para sugerir e triar; aprovação continua humana | Correções por regra e por IA passam por validação vinculada ao conteúdo; serviços reais de IA não foram exercitados nesta revisão |

A validação de propostas usa contêiner local sem rede e perfil DuckDB temporário. Aplicação e rollback usam journal durável e lock por projeto.
Essa etapa foi solicitada pelo usuário após a revisão; hoje faz parte do IntentFlow Enterprise.

## Pendente de decisão do usuário

- Autorização e credenciais para testar contra Snowflake/BigQuery/Databricks e Airflow reais.
- Projeto dbt de **produção** (com `catalog.json`) para medir a precisão real e a utilidade dos achados: roteiro em [PILOT](PILOT.md).

## Modelo de equipe (decisão do usuário: "pode ter mais de uma pessoa trabalhando")

| Decisão | Motivo |
|---|---|
| Dois modos automáticos: local (sem usuários) e equipe (com usuários) | Zero atrito no notebook; segurança quando há mais de uma pessoa |
| Token por usuário, só o hash guardado; papéis viewer/engineer/admin | Simples, auditável, sem depender de provedor de identidade |
| Separação de autor/aprovador conforme política em equipe; revisão extra exige outra pessoa mesmo em modo solo | A política de revisão extra não pode ser contornada pela existência de apenas um usuário |
| Fora do loopback exige usuários e TLS (ou proxy declarado) | Evitar expor ferramenta com poder de alterar arquivos sem autenticação |
| Armazém SQLite (padrão) **ou** PostgreSQL, um por projeto | SQLite zero-config para uso solo; PostgreSQL para produção (3× a vazão com 100 usuários, sem escritor único). Mesmo código via camada fina (`_PgConn`, `ON CONFLICT`); sem ORM |
| MFA TOTP, SSO OIDC e papéis por projeto (revisão posterior, pedida pelo usuário) | Removem os limites listados na primeira versão; verificados contra IdP simulado e armazéns reais |

## Open core e idioma (14/10/2026, decisão do usuário)

| Decisão | Motivo |
|---|---|
| Núcleo Apache-2.0 + IntentFlow Enterprise (licença comercial, pacote separado por entry point) | Adoção pelo núcleo grátis; receita pelo que serve à empresa (SSO, governança por projeto, correções, IA, integrações corporativas) |
| Login por token, papéis e MFA ficam no núcleo | Segurança não é recurso pago |
| Licença Enterprise verificada offline (Ed25519) | Sem telefonar para casa; funciona em rede fechada |
| Inglês como idioma principal; português por catálogo (`tr()` no Python, `T()` na UI) | Alcance internacional sem perder o público brasileiro; o id do achado não depende do idioma |
