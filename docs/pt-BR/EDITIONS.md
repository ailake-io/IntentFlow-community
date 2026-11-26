# Edições

[English](../EDITIONS.md)

Desde a 0.4.0 há uma única edição. As funções do antigo pacote privado Enterprise (SSO/OIDC, papéis por projeto,
correções governadas com validação em sandbox, sugestões de IA e triagem Jev, sinks Jira/PagerDuty/Opsgenie) fazem parte do
`intentdataflow` sob Apache-2.0, sem chave de licença.

- Instalação: `pip install intentdataflow` (`[secrets]` para SSO e segredos cifrados).
- `INTENTFLOW_LICENSE` / `INTENTFLOW_LICENSE_FILE` não são mais lidos.
- Não instale o antigo `intentflow-enterprise` junto com a 0.4.0: ele registraria as mesmas funções duas vezes. Desinstale.
