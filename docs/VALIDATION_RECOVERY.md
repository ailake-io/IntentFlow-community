# Isolated dbt validation and change recovery

[Português](pt-BR/VALIDATION_RECOVERY.md)

These features ship in the core package (formerly the private Enterprise extension); install: [INSTALLATION](INSTALLATION.md).

The 2026-11-19 sandbox image scan found 50 HIGH Debian-package occurrences (13 distinct CVEs), no CRITICAL findings and no HIGH/CRITICAL Python findings. Remediation remains pending; see [VALIDATION_STATUS](VALIDATION_STATUS.md).

## Local preparation

Requirements: Linux, Docker with a local Unix socket and a persistent store for recovery after the process restarts.
Default limits of one validation: 2 CPUs, 1 GiB of RAM, 128 processes and 600 seconds. No GPU.
At most 2 validations run at the same time **per Python process** (`INTENTFLOW_DBT_MAX_VALIDATIONS`); a third in that process gets a "try again" message. Separate processes have separate semaphores; this is not a host-wide global cap.

Build the dedicated image from the root of this repository:

```bash
docker build -f docker/dbt-sandbox.Dockerfile -t intentflow-dbt-sandbox:local docker
```

Preparing downloads the Python image and public libraries if they are not available. It receives no dbt project files. Later validations use the
installed image, resolved by ID, with `--pull=never`; they download no images or packages. For an offline environment move a prebuilt image with
`docker save`/`load`. The image uses dbt-core 1.12.5 and dbt-duckdb 1.11.0 (the transitive dependencies are not fully locked yet).

Optional settings, only in the IntentFlow process environment:

```bash
export INTENTFLOW_DBT_IMAGE=intentflow-dbt-sandbox:local
export INTENTFLOW_DBT_DOCKER_SOCKET=/var/run/docker.sock
```

For rootless Docker, point to your local Unix socket. TCP/remote daemons are not accepted. Access to the daemon must stay restricted to a trusted operator.

## What the validation runs

```bash
intentflow fix validate ID --mode build
```

- Builds an input with the model, macro, seed, test, snapshot, analysis and package paths declared in the dbt project.
- Copies only the relevant SQL/YAML/CSV/Markdown/Python files. Excludes the original profiles, hidden files, databases, keys and caches.
- Refuses external paths, symlinks, hardlinks, special files, inputs above 128 MiB/20 thousand files and edits above 32 MiB.
- Applies the diff only to that input and checks the original hash again during the copy.
- Mounts the input read-only. Inside the container, copies it to tmpfs and writes a DuckDB profile pointing at `/work/validation.duckdb`.
- Runs as user 10001, read-only root filesystem, no extra capability, no-new-privileges, network disabled and limited resources.
- Passes no credentials from the IntentFlow environment and never uses the original `profiles.yml`.
- `parse` checks the project with the demo profile. `build` runs the seeds and the selection of changed models, their ancestors and descendants.
- Records mode, result, image ID, `adapter=duckdb` and `scope=local-demo`. Retained output is bounded; the container is removed at the end.

If Docker, the image or the socket is unavailable, the validation fails. **There is no fallback to running dbt on the host.** Applying refuses old
validations without the isolation identifier; validate again.

Build uses `--indirect-selection buildable`: tests whose dependencies are outside the selection/ancestors are not executed. This avoids failures on unbuilt sibling tables; it does not test the whole project. Results record `test_selection=buildable`. See [dbt test selection](https://docs.getdbt.com/reference/node-selection/test-selection-examples) for the selection semantics. In-scope tests remain required and their failures block apply.

### Validation from a container without Docker (queue + worker)

Set `INTENTFLOW_VALIDATION=remote` on the UI process (the slim image). `validate` then stores a request and answers `202 {"queued": true}`
instead of running the sandbox. On a host or runner with Docker and the same store run:

```bash
intentflow validate-worker            # loops; --once for a single pass, --interval N seconds
```

The worker runs each queued validation exactly as an inline one (same isolation, same checks, audited as the person who asked) and
writes the result into the proposal. A request is dropped if the proposal changed after it was queued. The worker must see the project files at the **same absolute path** the UI recorded in the proposal (mount the directory at the same path). One worker per store is expected; the
per-process validation cap still applies; separate workers do not share a host-wide semaphore.

### Validity limits

This is a **local DuckDB demo environment**, with a temporary database and the project's own seed data. It does not validate Snowflake, BigQuery,
PostgreSQL or production profiles/credentials. Projects that depend on sources that cannot be rebuilt locally, extensions not installed or missing
dbt packages may fail. Packages must already be in `dbt_packages`; there is no networked `dbt deps`. Copied code and seeds may contain sensitive
literals; isolation does not anonymize content.

## Recoverable apply and rollback

```bash
intentflow fix approve ID --user reviewer
intentflow fix apply ID
intentflow fix rollback ID
```

For each project:

1. Takes an exclusive lock among IntentFlow operations on the same filesystem; a concurrent operation gets a "try again" message.
2. Checks every file and hash before writing.
3. Writes `.intentflow-transactions/active.json` with the before/after bytes and the proposal data; confirms it with fsync.
4. Persists the `applying` or `rolling_back` state in the store.
5. Replaces each file with an atomic rename, keeping the POSIX mode; confirms file and directory with fsync.
6. Confirms `applied` or `rolled_back` in the store with the transaction id, records the audit entry and removes the journal.

The journal directory has mode 700 and the file 600. Rollback keeps the original bytes, including CRLF. ACLs and extended attributes are not
preserved. A common failure before the commit tries to restore the batch automatically; if that also fails, the journal stays available.

## Recovering after an interruption

```bash
intentflow --store /path/intentflow.db fix recover ID --user reviewer
```

In the UI: **Fixes → Recover operation** (`POST /api/proposals/ID/recover`, with permission to apply fixes). A repeated call without a journal
changes no files.

- **Commit not confirmed:** restores the batch and the proposal's previous state.
- **Commit already confirmed:** checks the resulting files and only finishes cleaning the journal.
- **Content different from both the previous and the proposed one:** refuses the whole recovery before restoring any file, keeping the edit.
- **Another store/proposal:** refuses; use the store that started the operation.

Do not delete the journal to free the project. On a conflict, save the later edit outside the target files, restore one of the known versions,
run the recovery and reapply the saved edit afterwards.

The lock coordinates IntentFlow operations on one filesystem with flock support. Editors and external tools must honor it; external readers that do
not may see part of a batch while it is applied: **there is no atomic swap of several paths at once**. Use a persistent store; `Store(':memory:')`
does not keep state after the process ends. There is no distributed coordination between independent hosts/filesystems.

## Verification

Latest broad run (2026-10-13): 318 passed, 52 skipped, no failures; the isolated load test passed (p95 1.8 s, limit 15 s). The container tests use
the real Docker with the image built locally, synthetic files and a fake secret; interruption tests use `os._exit` in the process that changes files
and reopen the store in another process. Code: [dbt_sandbox.py](../src/intentflow/enterprise/dbt_sandbox.py),
[file_transaction.py](../src/intentflow/enterprise/file_transaction.py), [proposals.py](../src/intentflow/enterprise/proposals.py).
Coverage: [test_validation_recovery.py](../tests/test_ee_validation_recovery.py).
