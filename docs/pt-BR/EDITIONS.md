# Edições: open core

[English](../EDITIONS.md)

O núcleo é **open source (Apache-2.0)**. Uma edição comercial (**IntentFlow Enterprise**) acrescenta o que só faz sentido com
várias pessoas ou numa empresa: integração de identidade, governança por projeto, correções governadas, IA e integrações corporativas.

**A regra:** o que um engenheiro ou um time pequeno precisa para não quebrar produção é grátis e continua grátis. Linhagem, checks,
diff e segurança (login, MFA, cifragem em repouso) nunca serão pagos. É pago o que serve à governança e às integrações de uma empresa.

| Open source (Apache-2.0) | Enterprise (licença comercial) |
|---|---|
| `check`, `gate`, `lineage` (impacto, jobs externos), `diff`, `risks` | SSO com OpenID Connect (Keycloak, Entra ID, Okta, Google…), grupos → papéis, logout back-channel |
| Todos os checks (SQL, dbt, schema, PII, orquestração, OpenLineage, cluster) | Papéis por projeto e escopo de escrita por pasta/modelo |
| UI e API, em inglês e português | Vários projetos num servidor (um armazém por projeto) |
| Login de equipe por token, papéis (viewer/engineer/admin), MFA (TOTP) | Correções governadas: proposta → aprovação → validação em sandbox → aplicação → rollback (`fix`) |
| Segredos cifrados em repouso, TLS, trilha de auditoria | Sugestões de correção por IA e triagem/porta de risco com Jev (`fix-ai`, `llm`) |
| Armazém SQLite ou PostgreSQL | Saídas corporativas: PagerDuty, Opsgenie, Jira, Teams, GitLab, Bitbucket, Azure DevOps |
| Saídas: Slack, webhook, e-mail, comentário de PR no GitHub | Suporte com prazo de resposta e implantação assistida |
| Coleta de Airflow, Kestra, dbt, warehouses, OpenLineage | |

## Como se encaixa

- O pacote Enterprise (`intentflow-enterprise`, repositório privado) se registra no entry point `intentflow.extensions`. O núcleo
  funciona sem ele e nunca o importa.
- A licença é um token assinado com Ed25519 e verificado offline (`INTENTFLOW_LICENSE` ou `INTENTFLOW_LICENSE_FILE`); nada é enviado
  para fora. Sem licença válida a extensão carrega, mas não liga nenhum recurso.
- `/api/me` informa os recursos ativos; a UI esconde as telas Enterprise quando estão desligados.

## Repositórios

- **Núcleo (este repositório, público):** [ailake-io/IntentFlow-community](https://github.com/ailake-io/IntentFlow-community). Começou com
  histórico novo, então nunca conteve código do Enterprise.
- **Enterprise (privado):** disponível para clientes com contrato comercial.

## Contribuições

Contribuições ao núcleo usam DCO (`git commit -s`), ver [CONTRIBUTING](../../CONTRIBUTING.md). A Apache-2.0 já permite usar código do
núcleo dentro do Enterprise, então não é preciso CLA.
