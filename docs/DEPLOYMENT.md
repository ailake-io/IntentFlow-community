# Deploying for a team

[Português](pt-BR/DEPLOYMENT.md)

There are two modes, picked automatically:

| Mode | When | Access |
|---|---|---|
| **Local** | No registered user | One operator, only on `127.0.0.1`, no login. The default on a notebook. |
| **Team** | At least one registered user | Token login, roles, per-person trail. May listen outside loopback. |

## Roles

| Role | Can |
|---|---|
| `viewer` | See findings, lineage, pipelines, cluster, fixes and audit |
| `engineer` | Everything a viewer can + acknowledge, mark false positive, assign, comment, run checks and data diffs; with Enterprise, create/validate/approve/apply/roll back fixes |
| `admin` | Everything + settings, policies and team management |

Settings that make the server reach hosts, read paths or use environment secrets (`notify.sinks`, `*.url`, `*.host`, `*_env`, warehouse and
orchestrator connections…) need a **global** admin. Someone who is admin of just one project can edit `policy.*`, `scan.*`, `watch.*`,
`lineage.*` and `dbt.*`.

Rules: **whoever creates a fix cannot approve it** (with more than one user; `policy.separate_approver = false` turns it off); the last active
admin cannot be removed; a disabled user loses access immediately; every action records **who did it** in the audit trail.

## Step by step

```bash
# 1) create the first admin (the token shows up ONCE; only its hash is stored)
intentflow --store /srv/intentflow/intentflow.db user add maria --role admin

# 2) start the server
intentflow --store /srv/intentflow/intentflow.db ui --host 0.0.0.0 --port 8787 \
    --tls-cert /etc/ssl/intentflow.pem --tls-key /etc/ssl/intentflow.key
#    or behind a reverse proxy that terminates TLS:
intentflow --store ... ui --host 127.0.0.1 --port 8787 --behind-proxy   # (host != loopback requires --behind-proxy or TLS)

# 3) maria logs in with the token and adds the others under "Team" (or through the CLI)
intentflow --store ... user add joao --role engineer
intentflow --store ... user add ana  --role viewer
```

Automatic refusals: listening outside loopback **without users**, or **without HTTPS/`--behind-proxy`**.
Behind a proxy, set the public host in `INTENTFLOW_ALLOWED_HOSTS=intentflow.example.com`.

## Operation

- **Scheduler:** `intentflow --store ... watch --interval 300` in a separate process (collect, check and notify). One per store.
- **Backup:** `intentflow --store ... backup /backups/if-$(date +%F).db` (consistent copy, mode 600; the file holds token hashes). On PostgreSQL prefer `pg_dump`.
- **Retention:** `intentflow --store ... prune --days 90`.
- **Token rotation:** `intentflow user rotate NAME` (or "New token" in the UI).
- **Language:** `INTENTFLOW_LANG=pt` for Portuguese in the CLI and in findings produced by `watch`; the UI follows each person's browser and selector.
- **Several projects:** one store per project (`--store` or `INTENTFLOW_STORE`) and one UI per store; serving several projects from one
  server with per-project roles is Enterprise (below).

## Container deployment checklist (verified in a production-like run)

Verified with Docker: PostgreSQL 16, the UI over TLS, a `watch` scheduler and a webhook receiver on one network, 50 concurrent users, restarts and
`kill -9` of the database, `pg_dump`/`pg_restore`, and a rename caught by `check --base` (exit 1).

- **Build:** `docker build --network=host -t intentflow:0.3.0 .` if your Docker DNS cannot reach PyPI during the build.
- **Store and secrets:** `IF_DB_URL` and `INTENTFLOW_SECRET_KEY` as environment variables (use your secret manager); `--store env:IF_DB_URL`.
- **TLS files** must be readable by uid `10001` (the image user), including their directory. Otherwise the UI exits with `PermissionError`.
- **Scheduler:** run `watch` as its own container with `--restart unless-stopped`. It reconnects after a database restart or crash
  (a cycle that fails is audited as `watch_error`); the UI opens a connection per request and needs no restart.
- **Fixes (Enterprise):** the project directory must be **writable** by uid `10001` (the fix journal lives in `.intentflow-transactions`).
  Validation needs the Docker CLI and socket, which the slim image does not have: set `INTENTFLOW_VALIDATION=remote` on the UI container and run `intentflow validate-worker` on a host or CI runner that has Docker and the same
  store (Enterprise docs: VALIDATION_RECOVERY). Without a worker or Docker the validation stays queued or fails closed, and `apply` is refused.
- **CI gate:** `check --base` needs `catalog.json` for the head build (`dbt docs generate`); `parse` alone leaves columns unknown and the rename is not seen.
- **Backups:** `pg_dump -Fc` and `pg_restore` into a clean database reproduce users, findings and audit.
- **Rollback to the core:** the same store opens with the core-only image; Enterprise features switch off and the data stays.

## E-mail notifications (SMTP)

`email` output (IntentFlow keeps only variable names): `SMTP_HOST`, `SMTP_PORT` and, if the relay authenticates, `SMTP_USER` and `SMTP_PASSWORD`.
Output options: `tls` = `auto` (STARTTLS if offered; default) | `starttls` (required) | `ssl` (port 465) | `none`; `tls_verify: false` only for a
self-signed certificate in development. **Login is refused over an unencrypted channel** unless the output has `allow_insecure_auth: true`.
Verified against Mailpit (plain text, STARTTLS, login).

## Secrets encrypted at rest

TOTP, SSO `id_token` and `refresh_token` are encrypted (Fernet/AES + HMAC, prefix `enc:v1:`) when `cryptography` and the key are available.
Install `pip install ".[secrets]"` and check `intentflow secrets status`. The Docker image includes this extra.
Without the library or a key, writing new secrets to persistent stores is refused. Legacy plain-text secrets stay readable for migration.

- **Key:** `INTENTFLOW_SECRET_KEY` (long text; recommended in production and **required with PostgreSQL**), or, for a SQLite file, the
  `<store>.key` file (mode 600, created on the first write). **Keep the key with the backup**: without it the secrets cannot be recovered.
- **Old stores:** `intentflow secrets migrate` encrypts what was in plain text (idempotent).
- **Changing the key:** set the new one, run `secrets status` (it fails with "key differs") and ask people to redo MFA/SSO.

## Alerts and PRs (outputs)

In `notify.sinks` (settings, not a file): each output has `name`, `type` and environment variable names for the secrets.

| `type` | Fields | Secret (variable) |
|---|---|---|
| `slack` / `webhook` | `url_env` | webhook URL |
| `email` | `to`, `from`, `tls`, `tls_verify`, `allow_insecure_auth` | `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD` |
| `github` | `repo`, `pr`, `api` | `GITHUB_TOKEN` |
| `teams` (Enterprise) | `url_env` (default `TEAMS_URL`) | Teams webhook flow URL (Adaptive Card) |
| `pagerduty` (Enterprise) | `routing_key_env`, `source`, `url` | `PAGERDUTY_ROUTING_KEY` (incident id as `dedup_key`) |
| `opsgenie` (Enterprise) | `api`, `token_env` | `OPSGENIE_API_KEY` |
| `jira` (Enterprise) | `url`, `project`, `issue_type`, `labels`, `user_env`, `token_env` | `JIRA_USER`, `JIRA_TOKEN` |
| `gitlab` (Enterprise) | `project` (id or `group/project`), `mr`, `api` | `GITLAB_TOKEN` |
| `bitbucket` (Enterprise) | `workspace`, `repo`, `pr`, `api` | `BITBUCKET_TOKEN` |
| `azure_devops` (Enterprise) | `org`, `project`, `repo`, `pr`, `api` | `AZURE_DEVOPS_PAT` |

Routes in `notify.routes`: `{sink, min_severity, owner, check_prefix}`. Anti-spam: the same incident with the same severity is only resent after 24 h.

## Metrics (Prometheus and OpenTelemetry)

- **Prometheus:** `GET /metrics` (requires the same `Authorization: Bearer` as the API). Series: `intentflow_findings_open{severity}`,
  `intentflow_findings_open_by_check{check}`, `intentflow_runs{system,state}`, `intentflow_check_last_run_timestamp_seconds`, `intentflow_check_last_findings`.
- **OpenTelemetry:** `intentflow otel-push --endpoint http://collector:4318` (OTLP/HTTP JSON). Headers through `OTEL_EXPORTER_OTLP_HEADERS`.

## Container

The `Dockerfile` installs Python dependencies from `uv.lock` (`uv sync --locked`); keep the lock in sync with `pyproject.toml`. The Python and uv
base images are pinned by digest.

```bash
docker build -t intentflow .
docker run --rm -v if-data:/data intentflow user add maria --role admin
docker run -d -p 127.0.0.1:8787:8787 -v if-data:/data intentflow ui --host 0.0.0.0 --behind-proxy
```

Publish the port **only behind a proxy with TLS** (nginx, Caddy, Traefik). The image runs as UID/GID **10001**, without root. The build context
excludes local databases, keys, `.env` and demo folders. The image still needs an OS CVE scan before publishing. The Databricks extra pulls a
vulnerable `oauthlib` (the connector pins `<4.0.0`); the default image does not install it.

## Two-step verification (MFA/TOTP)

Each person turns it on in their own menu ("turn on MFA") or through the CLI (`intentflow user mfa-begin NAME` → `mfa-confirm NAME --code 123456`).
With MFA on, login requires the token **and** a 6-digit code (RFC 6238, ±1 step tolerance, **each code works once**); the bare token no longer
opens the API. For automation use a service user **without** MFA and a minimal role. The browser keeps a **session** (12 h) in an
`HttpOnly`/`SameSite=Strict` cookie, never the long-lived token; "log out" invalidates it.

## Single sign-on (SSO / OpenID Connect) — Enterprise

```bash
export INTENTFLOW_SSO_SECRET=...           # client secret: only through an environment variable
intentflow --store ... sso set --issuer https://login.example.com/realms/data --client-id intentflow \
    --client-secret-env INTENTFLOW_SSO_SECRET --default-role viewer --auto-provision yes --allowed-domains example.com
```

Register the redirect `https://YOUR-HOST/auth/sso/callback` at the provider. Calls to the provider (discovery, JWKS, token) block internal
destinations at connect time, do not follow redirects and ignore environment proxies. A provider on a **private network**: set
`INTENTFLOW_SSO_ALLOW_INTERNAL=1`. Authorization code flow with PKCE, `state` and `nonce`; the `id_token` is validated (**RS256** signature
through JWKS, `iss`, `aud`, `exp`, `nonce`; `alg` `none`/HS256 are refused) and the e-mail must be verified. Unknown people are **denied** unless
`auto-provision` is on and the domain is allowed.

### Provider groups → roles

```bash
intentflow --store ... sso set --group-claim groups \
    --group-map 'data-admin=admin; data-read=viewer; crm-eng=engineer@crm; sales-block=none@sales' \
    --require-group yes --sync-roles yes
```

One rule per line: `group=role` (global) or `group=role@project`. The **highest role** among the person's groups wins. Accounts created by SSO
are synced on every login; hand-made accounts only with `--sync-roles yes`. `--require-group yes` denies people without a mapped group. Changes go
to the audit trail (`roles_synced`).

When no global group matches, the global role falls to the floor (`sso.unmapped_role`, else `sso.default_role`, else `viewer`): leaving a
group at the provider demotes the person at the next login or refresh. Per-project roles are managed only for the projects named in the
group map, and an explicit per-project `none` set by an admin is never lifted by the provider.

### Single logout

"Log out" on an SSO session ends the local session **and** redirects to the provider's `end_session_endpoint` with `id_token_hint`. Back-channel
logout and group refresh through the refresh token are implemented and tested with Keycloak.

## Several projects on one server, with roles per project — Enterprise

```bash
intentflow --store /srv/if/main.db project add crm /srv/if/crm.db
intentflow --store /srv/if/main.db project add sales /srv/if/sales.db
intentflow --store ... access project-role carol --project crm --role engineer   # carol: viewer on main, engineer on crm
intentflow --store ... access project-role bob   --project crm --role none       # bob does not see crm
intentflow --store ... access scope dave --project main --patterns 'models/marts/*'
```

Each project has **its own store** (findings, settings, policies and audit isolated). Users, MFA, SSO and the project list live in the main
store. The per-project role overrides the default role (`inherit` goes back to inheriting). The UI shows a project selector when the person can
see more than one.

## PostgreSQL in production

The store runs on **SQLite** (default, a file) or **PostgreSQL** (recommended for teams/production), picked by the address in `--store` / `INTENTFLOW_STORE`:

```bash
export IF_DB_URL='postgresql://intentflow:PASSWORD@db.internal:5432/intentflow'
intentflow --store env:IF_DB_URL user add maria --role admin
intentflow --store env:IF_DB_URL ui --host 0.0.0.0 --behind-proxy
intentflow --store env:IF_DB_URL watch --interval 300
```

- **Schema and migrations** are created on the first connection, under `pg_advisory_lock`: several processes may start at the same time.
- **Pool:** `INTENTFLOW_PG_POOL` (default 24 connections per process). Add up every process and stay below `max_connections`.
- **Migrating from SQLite:** `intentflow migrate-store /data/if.db env:IF_DB_URL` copies everything and fixes sequences; tokens keep working.
  A destination with data requires `--overwrite`.
- **TLS/network:** use `?sslmode=verify-full` with the right CA and hostname. Treat the database and backups as sensitive.

Verified against a real PostgreSQL 18 (container): behavior parity in `tests/test_store_backends.py`, SQLite ⇄ PostgreSQL migration, 6 processes
creating the schema at once. CI: the `integration` job starts PostgreSQL 16 and Mailpit.

## Measured capacity (one machine, local server, 1 process)

100 simultaneous users × 6 cycles (overview + read + comment), 1200 requests, 0 errors on both backends:

| Backend | Throughput | p50 | p95 | max |
|---|---|---|---|---|
| SQLite (WAL) | 47 req/s | 1.5 s | 3.3 s | — |
| PostgreSQL 18 | 144 req/s | 0.58 s | 1.36 s | 3.3 s |

Not measured with thousands of users, a high-latency remote database or several UI replicas.

## Limits

- **SSO verified against a real Keycloak 26** and in the browser. **Not** exercised with Entra ID, Okta or Google.
- **Roles are per project**; inside one, path scopes (`intentflow access scope`, Enterprise) limit writes to folders. The scope is checked against every file a fix would change, not only the finding's file. Reading is not restricted by scope.
- **MFA is TOTP** with single-use recovery codes; no WebAuthn. Lost device and codes: an admin runs `intentflow user mfa-disable NAME`.
- **Login attempt limit per IP** (10/min, persistent). Behind a proxy set `INTENTFLOW_TRUSTED_PROXIES` so `X-Forwarded-For` is only accepted from them.
- **Warehouse connection secrets** stay in server environment variables (shared by every project in the same process).
- **Tokens:** optional expiry (`user add --expires-days`, `policy.token_max_age_days`); rotation stays manual (`user rotate`).
- **Snowflake/BigQuery/Databricks adapters** were only tested with fake connections; DuckDB and PostgreSQL with real databases.
