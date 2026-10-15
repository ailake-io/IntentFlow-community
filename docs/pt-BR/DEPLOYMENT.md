# Implantação para uma equipe

[English](../DEPLOYMENT.md)

Há dois modos, escolhidos automaticamente:

| Modo | Quando | Acesso |
|---|---|---|
| **Local** | Nenhum usuário cadastrado | Um operador, só em `127.0.0.1`, sem login. É o padrão no notebook. |
| **Equipe** | Pelo menos um usuário cadastrado | Login por token, papéis, trilha por pessoa. Pode escutar fora do loopback. |

## Papéis

| Papel | Pode |
|---|---|
| `viewer` (leitor) | Ver achados, linhagem, pipelines, cluster, correções e auditoria |
| `engineer` (engenheiro) | Tudo do leitor + reconhecer, marcar falso positivo, atribuir, comentar, criar/validar/aprovar/aplicar/reverter correções, rodar verificações e diff de dados |
| `admin` | Tudo + configuração, políticas e gestão da equipe |

Regras: **quem cria uma correção não pode aprová-la** (com mais de um usuário; desligável com `policy.separate_approver = false`);
o último admin ativo não pode ser removido; usuário desativado perde o acesso na hora; toda ação grava o **nome de quem fez** na auditoria.

## Passo a passo

```bash
# 1) cria o primeiro admin (o token aparece UMA vez; só o hash é guardado)
intentflow --store /srv/intentflow/intentflow.db user add maria --role admin

# 2) sobe o servidor
intentflow --store /srv/intentflow/intentflow.db ui --host 0.0.0.0 --port 8787 \
    --tls-cert /etc/ssl/intentflow.pem --tls-key /etc/ssl/intentflow.key
#    ou atrás de um proxy reverso que termina TLS:
intentflow --store ... ui --host 127.0.0.1 --port 8787 --behind-proxy   # (host != loopback exige --behind-proxy ou TLS)

# 3) maria entra no navegador com o token e cadastra o restante em "Equipe" (ou pela CLI)
intentflow --store ... user add joao --role engineer
intentflow --store ... user add ana  --role viewer
```

Recusas automáticas: escutar fora do loopback **sem usuários**, ou **sem HTTPS/`--behind-proxy`**.
Atrás de proxy, informe o host público em `INTENTFLOW_ALLOWED_HOSTS=intentflow.exemplo.com`.

## Operação

- **Agendador:** `intentflow --store ... watch --interval 300` em um processo separado (coleta, verifica e notifica). Um só por armazém.
- **Backup:** `intentflow --store ... backup /backups/if-$(date +%F).db` (cópia consistente, permissão 600; o arquivo contém hashes de token). Em PostgreSQL, prefira `pg_dump` (ver abaixo).
- **Retenção:** `intentflow --store ... prune --days 90`.
- **Rotação de token:** `intentflow user rotate NOME` (ou "Novo token" na UI).
- **Idioma:** `INTENTFLOW_LANG=pt` deixa a CLI e os achados do `watch` em português; a UI segue o navegador e o seletor de cada pessoa.
- **Vários projetos:** um armazém por projeto (`--store` ou `INTENTFLOW_STORE`); suba uma UI por armazém. Vários projetos num servidor, com papel por projeto, é Enterprise (abaixo).

## Notificações por e-mail (SMTP)

Sink `email` (`intentflow` guarda só nomes de variáveis): `SMTP_HOST`, `SMTP_PORT`, e, se o relay autentica, `SMTP_USER` e `SMTP_PASSWORD`.
Opções do sink: `tls` = `auto` (STARTTLS se o servidor oferecer; padrão) | `starttls` (exige) | `ssl` (porta 465) | `none`;
`tls_verify: false` só para certificado autoassinado em desenvolvimento. **O login é recusado em canal sem criptografia**, a menos que o
sink tenha `allow_insecure_auth: true`. Verificado contra Mailpit (texto puro, STARTTLS, login).

## Segredos cifrados em repouso

TOTP, `id_token` e `refresh_token` de SSO são cifrados (Fernet/AES + HMAC, prefixo `enc:v1:`) quando `cryptography` e a chave estão disponíveis.
Instale `pip install ".[secrets]"` e confira `intentflow secrets status`. A imagem Docker inclui esse extra.
Sem a biblioteca ou sem chave, a gravação de novos segredos em armazéns persistentes é recusada. Segredos legados em texto claro continuam
legíveis para migração; não são cifrados retroativamente sem `secrets migrate`. O armazém `:memory:` pode manter valores em memória sem cifragem.

- **Chave:** `INTENTFLOW_SECRET_KEY` (texto longo; recomendado em produção e **obrigatório com PostgreSQL**), ou, em SQLite em arquivo, o
  arquivo `<armazém>.key` (modo 600, criado na primeira gravação). **Guarde a chave junto do backup**: sem ela os segredos não se recuperam
  (as pessoas refazem o MFA/SSO; tokens de acesso e códigos de recuperação são hashes e não dependem dela).
- **Armazéns antigos:** `intentflow secrets migrate` cifra o que estava em texto (idempotente). `intentflow secrets status` mostra o estado.
- **Troca de chave:** defina a nova, rode `secrets status` (falha com "chave diferente") e peça novo MFA/SSO; não há rotação automática.
- **Fora do escopo:** a senha do DSN do PostgreSQL e as variáveis de ambiente continuam sendo responsabilidade do ambiente (gerenciador de segredos).

## Alertas e PRs (sinks)

Em `notify.sinks` (configuração, não arquivo): cada sink tem `name`, `type` e nomes de variáveis de ambiente para os segredos.

| `type` | Campos | Segredo (variável) |
|---|---|---|
| `slack` / `webhook` | `url_env` | URL do webhook |
| `teams` (Enterprise) | `url_env` (padrão `TEAMS_URL`) | URL do fluxo de webhook do Teams (Adaptive Card) |
| `email` | `to`, `from`, `tls`, `tls_verify`, `allow_insecure_auth` | `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD` |
| `pagerduty` (Enterprise) | `routing_key_env`, `source`, `url` | `PAGERDUTY_ROUTING_KEY` (usa o id do incidente como `dedup_key`) |
| `opsgenie` (Enterprise) | `api`, `token_env` | `OPSGENIE_API_KEY` (alias = id do incidente; prioridade P1–P5 pela severidade) |
| `jira` (Enterprise) | `url`, `project`, `issue_type`, `labels`, `user_env`, `token_env` | `JIRA_USER`, `JIRA_TOKEN` (abre uma issue por envio; REST v2) |
| `github` | `repo`, `pr`, `api` | `GITHUB_TOKEN` |
| `gitlab` (Enterprise) | `project` (id ou `grupo/projeto`), `mr`, `api` | `GITLAB_TOKEN` |
| `bitbucket` (Enterprise) | `workspace`, `repo`, `pr`, `api` | `BITBUCKET_TOKEN` |
| `azure_devops` (Enterprise) | `org`, `project`, `repo`, `pr`, `api` | `AZURE_DEVOPS_PAT` |

Rotas em `notify.routes`: `{sink, min_severity, owner, check_prefix}`. Anti-spam: o mesmo incidente com a mesma severidade só reenvia após 24 h.

## Métricas (Prometheus e OpenTelemetry)

- **Prometheus:** `GET /metrics` (exige o mesmo `Authorization: Bearer` da API; `bearer_token_file` no `scrape_config`). Séries:
  `intentflow_findings_open{severity}`, `intentflow_findings_open_by_check{check}`, `intentflow_runs{system,state}`,
  `intentflow_check_last_run_timestamp_seconds`, `intentflow_check_last_findings`.
- **OpenTelemetry:** `intentflow otel-push --endpoint http://collector:4318` (OTLP/HTTP JSON; ponha no cron/`watch`). Cabeçalhos por
  `OTEL_EXPORTER_OTLP_HEADERS`.

## Contêiner

O `Dockerfile` instala dependências Python a partir de `uv.lock` (`uv sync --locked`); mantenha o lock atualizado junto com
`pyproject.toml`. As imagens-base de Python e uv estão fixadas por digest no Dockerfile.

```bash
docker build -t intentflow .
docker run --rm -v if-data:/data intentflow user add maria --role admin
docker run -d -p 127.0.0.1:8787:8787 -v if-data:/data intentflow ui --host 0.0.0.0 --behind-proxy
```

Publique a porta **só atrás de um proxy com TLS** (nginx, Caddy, Traefik).
A imagem roda como UID/GID **10001**, sem root. Em bind mounts ou volumes antigos, prepare a propriedade/permissão de `/data` para esse usuário;
não abra permissão global de escrita. O contexto de build exclui bancos locais, chaves, `.env` e diretórios de demonstração.
A imagem com o lock foi construída em 13/10/2026; `intentflow --help` iniciou em container descartável sem rede e com filesystem
read-only. A imagem ainda precisa de varredura de CVEs do sistema operacional e verificação da inicialização do servidor antes da publicação.

`uv.lock` inclui os extras opcionais. A auditoria de 13/10/2026 encontrou `oauthlib 3.3.1` vulnerável no extra Databricks; o
conector `databricks-sql-connector 4.6.0` requer `oauthlib <4.0.0`, enquanto a correção indicada é `4.0.0`. A imagem padrão não
instala o extra Databricks. Não habilite esse extra em produção até o conector liberar uma versão compatível ou a combinação ser
atualizada e validada. A auditoria dos extras incluídos na imagem padrão (`secrets` e `postgres`) não encontrou CVEs conhecidas.
A auditoria não substitui varredura da imagem/base OS nem automação contínua de CVEs.

## Verificação em duas etapas (MFA/TOTP)

Cada pessoa ativa no próprio menu ("ativar MFA") ou pela CLI (`intentflow user mfa-begin NOME` → `mfa-confirm NOME --code 123456`).
Com MFA ativo: o login exige token **e** código de 6 dígitos (RFC 6238, tolerância de ±1 passo, **cada código só vale uma vez**);
o token isolado deixa de abrir a API. Para automação (CI, scripts) use um usuário de serviço **sem** MFA e papel mínimo.
O navegador guarda uma **sessão** (12 h) em cookie `HttpOnly`/`SameSite=Strict`, nunca o token de longa duração; "sair" a invalida.

## Login único (SSO / OpenID Connect) — Enterprise

```bash
export INTENTFLOW_SSO_SECRET=...           # segredo do cliente: só por variável de ambiente
intentflow --store ... sso set --issuer https://login.exemplo.com/realms/dados --client-id intentflow \
    --client-secret-env INTENTFLOW_SSO_SECRET --default-role viewer --auto-provision yes --allowed-domains exemplo.com
```

Registre no provedor o redirect `https://SEU-HOST/auth/sso/callback`.
As chamadas ao provedor (discovery, JWKS, token) bloqueiam destino interno na conexão, não seguem redirecionamento e ignoram proxy de ambiente.
Provedor em **rede privada** (ex.: Keycloak interno em `10.x`): defina `INTENTFLOW_SSO_ALLOW_INTERNAL=1`. Issuer em `localhost`/loopback literal dispensa.
 Fluxo *authorization code* com PKCE, `state` e `nonce`; o
`id_token` é validado (assinatura **RS256** pelo JWKS, `iss`, `aud`, `exp`, `nonce`; `alg` `none`/HS256 são recusados) e o e-mail
precisa estar verificado. Quem não existe é **negado**, a menos que `auto-provision` esteja ligado e o domínio permitido. O MFA fica a
cargo do provedor.

### Grupos do provedor → papéis

```bash
intentflow --store ... sso set --group-claim groups \
    --group-map 'dados-admin=admin; dados-leitura=viewer; crm-eng=engineer@crm; vendas-bloqueio=none@vendas' \
    --require-group yes --sync-roles yes
```

Regra por linha: `grupo=papel` (global) ou `grupo=papel@projeto`. Vale o **papel mais alto** entre os grupos da pessoa; aceita o claim
como lista, texto único ou caminhos (`/dados-admin`). No provedor, o cliente precisa emitir os grupos no `id_token` (no Keycloak:
mapper *Group Membership*, `full path` desligado, claim `groups`).

- **Quem é sincronizado:** contas criadas pelo SSO (`sso_managed`) a cada login; contas cadastradas à mão só com `--sync-roles yes`.
- **Sem grupo global mapeado** o papel global **não** é rebaixado sozinho; use um grupo de leitura para forçar o rebaixamento.
  Os papéis **por projeto** são substituídos pelos do login.
- `--require-group yes`: quem não pertence a nenhum grupo mapeado é **negado**.
- Mudanças ficam na auditoria (`roles_synced`).

### Logout único

"Sair" em uma sessão SSO encerra a sessão local **e** redireciona ao `end_session_endpoint` do provedor com `id_token_hint`
(RP-initiated logout), de modo que o próximo "Entrar com SSO" pede a senha de novo. Registre a URL do app como
*post-logout redirect* no cliente (Keycloak: atributo `post.logout.redirect.uris`). Se o provedor estiver fora do ar, encerra só a
sessão local. **Não há** logout por canal de retorno (back-channel): sair em outra aplicação do provedor não derruba a sessão aqui
antes de ela expirar (12 h).

## Vários projetos no mesmo servidor, com papel por projeto — Enterprise

```bash
intentflow --store /srv/if/main.db project add crm /srv/if/crm.db
intentflow --store /srv/if/main.db project add vendas /srv/if/vendas.db
intentflow --store ... access project-role carol --project crm --role engineer     # carol: leitora no principal, engenheira no crm
intentflow --store ... access project-role bob   --project crm --role none         # bob não enxerga o crm
```

Cada projeto tem **seu próprio armazém** (achados, configuração, políticas e auditoria isolados). Usuários, MFA, SSO e a lista de
projetos ficam no armazém principal (`main`). O papel por projeto sobrescreve o papel padrão (`inherit` volta a herdar). A UI mostra um
seletor de projeto quando a pessoa tem acesso a mais de um.

## PostgreSQL em produção

O armazém roda em **SQLite** (padrão, arquivo) ou **PostgreSQL** (recomendado para equipe/produção). O backend é escolhido
pelo endereço passado em `--store` / `INTENTFLOW_STORE`:

```bash
# DSN direto, ou "env:VAR" para não deixar a senha na linha de comando
export IF_DB_URL='postgresql://intentflow:SENHA@db.interno:5432/intentflow'
intentflow --store env:IF_DB_URL user add maria --role admin
intentflow --store env:IF_DB_URL ui --host 0.0.0.0 --behind-proxy
intentflow --store env:IF_DB_URL watch --interval 300
```

- **Esquema e migrações** são criados/aplicados na primeira conexão, sob `pg_advisory_lock`: vários processos podem subir ao mesmo tempo.
  O usuário do banco precisa de `CREATE` no schema `public` (ou crie as tabelas com um usuário admin e depois use um sem DDL).
- **Pool:** `INTENTFLOW_PG_POOL` (padrão 24 conexões por processo). Some todos os processos (UI + watch + CLI) e fique abaixo de `max_connections`.
- **Vários projetos:** um banco (ou DSN) por projeto, como no SQLite; `intentflow project add NOME DSN` aceita DSN; prefira `env:VAR` (o registro guarda o texto informado e a listagem mascara a senha).
- **Migrar de SQLite:** `intentflow migrate-store /dados/if.db env:IF_DB_URL` copia tudo (usuários, tokens, sessões, achados, notas, auditoria…)
  e ajusta as sequências; tokens continuam válidos. Destino com dados exige `--overwrite`. Também volta: PostgreSQL → arquivo.
- **Backup:** `pg_dump` é o caminho normal; `intentflow backup DEST` (DEST = arquivo ou DSN) faz a cópia lógica pelo próprio IntentFlow.
- **TLS/rede:** use `?sslmode=verify-full` com CA e hostname corretos para validar a identidade do servidor. O banco guarda hashes de token e segredos MFA/SSO; estes dependem de `cryptography` + `INTENTFLOW_SECRET_KEY` para cifragem (ver acima). Trate banco e backups como sensíveis.

**Verificado** contra PostgreSQL 18 real (contêiner): suíte inteira (124 testes) nos dois backends, paridade de comportamento em
`tests/test_store_backends.py`, migração SQLite ⇄ PostgreSQL, 6 processos criando o esquema simultaneamente.
CI: `.github/workflows/ci.yml` (job `integration` sobe PostgreSQL 16 e Mailpit; defina `INTENTFLOW_SECRET_KEY` por variável, pois não há arquivo `.key` em PostgreSQL).
Rode no PostgreSQL com `INTENTFLOW_TEST_BACKEND=postgres pytest` (`INTENTFLOW_TEST_PG_ADMIN` aponta o servidor; testes criam e apagam bancos `ift_*`).

## Capacidade medida (esta máquina, servidor local, 1 processo)

100 usuários simultâneos × 6 ciclos (visão geral + leitura + comentário), 1200 requisições, 0 erros nos dois backends:

| Backend | Vazão | p50 | p95 | máx |
|---|---|---|---|---|
| SQLite (WAL) | 47 req/s | 1,5 s | 3,3 s | — |
| PostgreSQL 18 | 144 req/s | 0,58 s | 1,36 s | 3,3 s |

Com 30 usuários no SQLite: p95 ≈ 1,2 s. 24 processos CLI escrevendo no mesmo arquivo: 0 falhas. A visão geral tem cache
(invalidado por escrita), então leituras repetidas são baratas. **Não** foi medido com milhares de usuários, banco remoto com latência
alta, nem várias réplicas da UI (o cache e o limite de login são por processo/armazém; o limite de login é persistente).

## Limites

- **SSO verificado contra Keycloak 26 real** (contêiner local: descoberta, PKCE, formulário de login, assinatura RS256, e-mail não
  verificado, senha errada, usuário desconhecido, provisionamento por domínio, usuário local desativado, **grupos → papéis**,
  `require_group`, **logout único**) **e no navegador** (redirect completo e logout). **Não** foi exercitado com Entra ID, Okta ou Google; claims, `email_verified` e rotação de chaves deles podem exigir ajustes.
  Realm de teste usado: cliente `intentflow` (confidencial, PKCE S256), `sslRequired: none`, redirect `http://127.0.0.1:8790/auth/sso/callback`.
- Cookies do Keycloak em desenvolvimento vêm com `Secure`; navegadores os aceitam em `127.0.0.1`/`localhost`, clientes HTTP simples não.
- **MFA é TOTP** (apps autenticadores) com códigos de recuperação de uso único; não há WebAuthn/chaves físicas. Perdeu dispositivo e códigos: um admin
  executa `intentflow user mfa-disable NOME` e a pessoa recadastra.
- **Papéis são por projeto**; dentro dele, escopos por caminho (`intentflow access scope`, Enterprise) limitam escritas a pastas. Leitura não é restrita por escopo.
- **Logout por canal de retorno (back-channel)** e renovação de grupos via refresh token estão implementados e testados com Keycloak; provedores sem refresh token só reavaliam grupos no próximo login.
- **Limite de tentativas de login por IP** (10/min, persistente). Atrás de proxy, defina `INTENTFLOW_TRUSTED_PROXIES` (IPs/CIDRs) para
  que `X-Forwarded-For` só seja aceito desses endereços.
- **Segredos de conexão ao warehouse** continuam por variável de ambiente do servidor (compartilhadas por todos os projetos no mesmo processo).
- **Tokens**: expiração opcional (`user add --expires-days`, `policy.token_max_age_days`); rotação continua manual (`user rotate`).
- **Adaptadores Snowflake/BigQuery/Databricks** só foram testados com conexões falsas; DuckDB e PostgreSQL, com bancos reais.

## Validação de propostas e recuperação

Faz parte do IntentFlow Enterprise; a documentação fica no pacote Enterprise.
