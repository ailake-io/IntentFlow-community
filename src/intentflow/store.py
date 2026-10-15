"""Store (SQLite or PostgreSQL): findings with a lifecycle, history, samples, runs, proposals and audit trail."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from .i18n import tr
from .model import Finding

_INITIALIZED: set = set()   # paths whose schema was already prepared in this process
_WLOCKS: dict = {}          # one write lock per file (within the process)
_WLOCKS_GUARD = threading.Lock()
_WRITE_PREFIX = ("insert", "update", "delete", "replace", "create", "alter", "drop")


class _Conn(sqlite3.Connection):
    """Serialize this process's write transactions: with hundreds of writers SQLite's backoff turns into an unfair queue
    (throughput collapsed and 'database is locked' showed up). The lock is taken on the first write and released on commit/rollback."""
    _wl = None
    _held = 0

    def _acquire(self):
        if self._wl is not None and not self._held:
            self._wl.acquire()
            self._held = 1

    def _release(self):
        if self._wl is not None and self._held:
            self._held = 0
            self._wl.release()

    def execute(self, sql, *a, **k):
        head = sql.lstrip()[:7].lower()
        ddl = head.startswith(("create", "alter", "drop"))      # DDL autocommits: the lock only lasts for the statement
        if head.startswith(_WRITE_PREFIX):
            self._acquire()
        try:
            return super().execute(sql, *a, **k)
        except Exception:
            if not self.in_transaction:
                self._release()
            raise
        finally:
            if ddl:
                self._release()

    def executemany(self, sql, *a, **k):
        self._acquire()
        try:
            return super().executemany(sql, *a, **k)
        except Exception:
            if not self.in_transaction:
                self._release()
            raise

    def executescript(self, sql):
        self._acquire()
        try:
            return super().executescript(sql)
        finally:
            self._release()

    def commit(self):
        try:
            super().commit()
        finally:
            self._release()

    def rollback(self):
        try:
            super().rollback()
        finally:
            self._release()

    def close(self):
        self._release()
        super().close()

    def __del__(self):
        try:
            self._release()
        except Exception:
            pass


# ====================================================================== PostgreSQL
_PG_POOLS: dict = {}
_PG_GUARD = threading.Lock()
_PG_LOCK_KEY = 727274           # advisory lock to create/migrate the schema without races between processes


class _Row(tuple):
    """Row accessible by index and by name (compatible with sqlite3.Row in the Store's uses)."""
    _names: tuple = ()

    def __new__(cls, names, values):
        o = super().__new__(cls, values)
        o._names = tuple(names)
        return o

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._names.index(k))
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._names)


class _Cur:
    def __init__(self, cur):
        self._c = cur
        self._names = [d.name for d in cur.description] if cur.description else []
        self.rowcount = cur.rowcount

    def fetchone(self):
        r = self._c.fetchone()
        return _Row(self._names, r) if r is not None else None

    def fetchall(self):
        return [_Row(self._names, r) for r in self._c.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def _pg_pool(dsn: str):
    import queue
    with _PG_GUARD:
        if dsn not in _PG_POOLS:
            n = int(os.environ.get("INTENTFLOW_PG_POOL", "24"))
            _PG_POOLS[dsn] = (queue.LifoQueue(), threading.BoundedSemaphore(n))
        return _PG_POOLS[dsn]


def close_pools(dsn: str | None = None) -> None:
    """Close idle pool connections (all of them, or only those of one DSN). Useful when switching databases or in tests."""
    with _PG_GUARD:
        for k in [dsn] if dsn else list(_PG_POOLS):
            q, _ = _PG_POOLS.pop(k, (None, None))
            while q is not None and not q.empty():
                try:
                    q.get_nowait().close()
                except Exception:
                    pass


class _PgConn:
    """Pool connection with the same minimal surface the Store uses (execute/commit/rollback/close).
    Autocommit for reads (no idle transactions); writes open an explicit BEGIN and close it on commit()."""

    def __init__(self, dsn: str):
        import psycopg2
        self.dsn = dsn
        self._q, self._sem = _pg_pool(dsn)
        if not self._sem.acquire(timeout=60):
            raise RuntimeError(tr("PostgreSQL connection pool exhausted"))
        try:
            self._c = self._q.get_nowait()
            if self._c.closed:
                raise Exception("fechada")
        except Exception:
            try:
                self._c = psycopg2.connect(dsn, connect_timeout=10)
            except Exception:
                self._sem.release()
                raise
        self._c.autocommit = True
        self._tx = False
        self._open = True

    @property
    def in_transaction(self) -> bool:
        return self._tx

    def execute(self, sql: str, params=()):
        low = sql.lstrip()[:8].lower()
        cur = self._c.cursor()
        if low.startswith(("insert", "update", "delete", "create", "alter", "drop")) and not self._tx:
            cur.execute("BEGIN")
            self._tx = True
        try:
            cur.execute(sql.replace("?", "%s") if params else sql, tuple(params) if params else None)
        except Exception:
            self.rollback()
            raise
        return _Cur(cur)

    def executemany(self, sql: str, seq):
        cur = None
        for row in seq:
            cur = self.execute(sql, row)
        return cur

    def executescript(self, script: str):
        for stmt in [x.strip() for x in script.split(";") if x.strip()]:
            self.execute(stmt)
        self.commit()

    def commit(self):
        if self._tx:
            self._tx = False
            self._c.cursor().execute("COMMIT")

    def rollback(self):
        if self._tx:
            self._tx = False
            try:
                self._c.cursor().execute("ROLLBACK")
            except Exception:
                pass

    def close(self):
        if not self._open:
            return
        self._open = False
        try:
            self.rollback()
            if not self._c.closed:
                self._q.put(self._c)
        finally:
            self._sem.release()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _is_pg(spec: str) -> bool:
    return spec.startswith(("postgresql://", "postgres://", "env:"))


def resolve_spec(spec: str) -> str:
    """`env:VAR` keeps the URL (with its password) out of the command line and of files."""
    if spec.startswith("env:"):
        v = os.environ.get(spec[4:])
        if not v:
            raise ValueError(tr("environment variable {name} is not set (PostgreSQL URL)", name=spec[4:]))
        return v
    return spec


SCHEMA = """
create table if not exists findings(id text primary key, check_id text, severity text, title text, detail text,
  nodes text, stage text, evidence text, fix text, confidence real, status text default 'open',
  first_seen real, last_seen real, ack_until real, ack_reason text, ack_by text);
create table if not exists history(key text, ts real, value real);
create index if not exists h_key on history(key, ts);
create table if not exists samples(ts real, source text, scope text, metric text, value real, lim real);
create table if not exists runs(system text, pipeline text, run_id text, state text, start real, "end" real, duration real, url text,
  primary key(system, pipeline, run_id));
create table if not exists proposals(id text primary key, json text, status text, updated real);
create table if not exists audit(ts real, actor text, event text, detail text);
create table if not exists ol_jobs(job text primary key, inputs text, outputs text, last_event real);
create table if not exists ol_edges(job text, src_ds text, src_col text, dst_ds text, dst_col text, primary key(job, src_ds, src_col, dst_ds, dst_col));
create table if not exists lineage_cache(key text primary key, value text);
create table if not exists users(name text primary key, role text not null, token_hash text not null, created real, disabled integer default 0, last_seen real);
create table if not exists notes(id integer primary key autoincrement, finding_id text, author text, ts real, text text);
create index if not exists notes_f on notes(finding_id);
create table if not exists sessions(id_hash text primary key, "user" text, created real, expires real, via text);
create table if not exists login_attempts(ip text, ts real);
create index if not exists la_ip on login_attempts(ip, ts);
create table if not exists settings(key text primary key, value text, updated real);
create index if not exists users_token_hash on users(token_hash);
create index if not exists audit_ts on audit(ts);
create index if not exists samples_ts on samples(ts);
create index if not exists findings_status on findings(status);
"""


class Store:
    """Metadata store. Backends: SQLite (file or :memory:; the default, zero configuration) and PostgreSQL (production;
    `postgresql://…` or `env:VARIABLE` holding the URL). The API is the same for both."""

    _MIGRATIONS = (
        ("findings", "assignee", "text"), ("findings", "assigned_by", "text"),
        ("users", "project_roles", "text default '{}'"), ("users", "totp_secret", "text"),
        ("users", "totp_enabled", "integer default 0"), ("users", "totp_last_step", "integer default 0"),
        ("users", "email", "text"), ("sessions", "id_token", "text"), ("users", "sso_managed", "integer default 0"),
        ("sessions", "sub", "text"), ("sessions", "sid", "text"), ("sessions", "refresh_token", "text"),
        ("sessions", "refreshed_at", "real"), ("users", "token_expires", "real"), ("users", "recovery_hashes", "text"),
        ("users", "scopes", "text default '{}'"),
    )

    def __init__(self, path: str = ":memory:"):
        self.spec = path
        self.backend = "postgres" if _is_pg(path) else "sqlite"
        self._box = None
        if self.backend == "postgres":
            self.path = resolve_spec(path)            # resolved DSN (never display it: it may contain a password)
            self.con = _PgConn(self.path)
            self._init_pg()
            return
        # SQLite: timeout/WAL; escritas serializadas por arquivo dentro do processo
        self.path = path
        self.con = sqlite3.connect(path, timeout=30, factory=_Conn)
        if path != ":memory:":
            ap = os.path.abspath(path)
            with _WLOCKS_GUARD:
                self.con._wl = _WLOCKS.setdefault(ap, threading.Lock())
        self.con.row_factory = sqlite3.Row
        if path != ":memory:":
            self.con.execute("pragma busy_timeout=30000")
            self.con.execute("pragma synchronous=NORMAL")      # safe in WAL: no loss of integrity
        key = os.path.abspath(path) if path != ":memory:" else None
        if key and key in _INITIALIZED and os.path.exists(path):
            return                                              # schema/migrations already applied by this process
        if path != ":memory:":
            self.con.execute("pragma journal_mode=wal")
        self.con.executescript(SCHEMA)
        for table, col, typ in self._MIGRATIONS:
            try:     # migration of databases created before team mode
                self.con.execute(f"alter table {table} add column {col} {typ}")
            except sqlite3.OperationalError:
                pass
        if key:
            _INITIALIZED.add(key)

    def _init_pg(self) -> None:
        if self.path in _INITIALIZED:
            return
        c = self.con
        c.execute("select pg_advisory_lock(?)", (_PG_LOCK_KEY,))
        try:
            ddl = (SCHEMA.replace(" real", " double precision").replace("integer primary key autoincrement", "bigserial primary key"))
            c.executescript(ddl)
            for table, col, typ in self._MIGRATIONS:
                c.execute(f"alter table {table} add column if not exists {col} {typ.replace('real', 'double precision')}")
            c.commit()
        finally:
            c.execute("select pg_advisory_unlock(?)", (_PG_LOCK_KEY,))
        _INITIALIZED.add(self.path)

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _upsert(self, table: str, cols: tuple, pk: tuple, rows, ignore: bool = False) -> None:
        """INSERT … ON CONFLICT (valid in SQLite ≥ 3.24 and PostgreSQL)."""
        q = lambda c: '"end"' if c == "end" else c      # noqa: E731
        names = ", ".join(q(c) for c in cols)
        ph = ",".join("?" * len(cols))
        if ignore:
            tail = "on conflict do nothing"
        else:
            tail = f"on conflict ({', '.join(pk)}) do update set " + ", ".join(f"{q(c)}=excluded.{q(c)}" for c in cols if c not in pk)
        self.con.executemany(f"insert into {table}({names}) values({ph}) {tail}", rows)

    # achados -----------------------------------------------------------------
    def sync_findings(self, findings: list[Finding], checks_run: set[str], now: float | None = None) -> dict:
        now = now or time.time()
        seen = set()
        new = 0
        for f in findings:
            seen.add(f.id)
            row = self.con.execute("select status, ack_until from findings where id=?", (f.id,)).fetchone()
            if row is None:
                new += 1
                self.con.execute("insert into findings(id, check_id, severity, title, detail, nodes, stage, evidence, fix, confidence, status, first_seen, last_seen, ack_until, ack_reason, ack_by) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (f.id, f.check, f.severity, f.title, f.detail, json.dumps(f.nodes), f.stage, json.dumps(f.evidence, default=str),
                                  json.dumps(f.fix) if f.fix else None, f.confidence, "open", now, now, None, None, None))
            else:
                status = row["status"]
                if status == "false_positive":
                    self.con.execute("update findings set last_seen=? where id=?", (now, f.id))
                    continue
                if status == "resolved":
                    status = "open"
                if status == "acked" and row["ack_until"] and row["ack_until"] < now:
                    status = "open"
                self.con.execute("update findings set severity=?, detail=?, evidence=?, fix=?, last_seen=?, status=? where id=?",
                                 (f.severity, f.detail, json.dumps(f.evidence, default=str), json.dumps(f.fix) if f.fix else None, now, status, f.id))
        resolved = 0
        for r in self.con.execute("select id, check_id from findings where status in ('open','acked')").fetchall():
            if r["id"] not in seen and r["check_id"] in checks_run:
                self.con.execute("update findings set status='resolved', last_seen=? where id=?", (now, r["id"]))
                resolved += 1
        self.con.commit()
        return {"new": new, "resolved": resolved, "total": len(findings)}

    def findings(self, status: str | None = "open") -> list[dict]:
        q = ("select f.*, coalesce(n.note_count, 0) as notes from findings f "
             "left join (select finding_id, count(*) as note_count from notes group by finding_id) n "
             "on n.finding_id=f.id" + (" where f.status=?" if status else ""))
        rows = self.con.execute(q, (status,) if status else ()).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["nodes"], d["evidence"] = json.loads(d["nodes"] or "[]"), json.loads(d["evidence"] or "{}")
            d["fix"] = json.loads(d["fix"]) if d["fix"] else None
            out.append(d)
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        out.sort(key=lambda d: (order.get(d["severity"], 9), d["id"]))
        return out

    def mark_false_positive(self, finding_id: str, actor: str, reason: str = "") -> None:
        self.con.execute("update findings set status='false_positive', ack_by=?, ack_reason=? where id=?", (actor, reason, finding_id))
        self.audit(actor, "false_positive", {"id": finding_id, "reason": reason})
        self.con.commit()

    def check_stats(self) -> list[dict]:
        """Por check: total, abertos, resolvidos, falsos positivos e taxa de falso positivo."""
        out = []
        for r in self.con.execute(
                "select check_id, count(*) n, sum(case when status='open' then 1 else 0 end) o, "
                "sum(case when status='resolved' then 1 else 0 end) r, sum(case when status='false_positive' then 1 else 0 end) fp, "
                "sum(case when status='acked' then 1 else 0 end) a from findings group by check_id order by check_id"):
            decided = (r["fp"] or 0) + (r["r"] or 0)
            out.append({"check": r["check_id"], "total": r["n"], "open": r["o"] or 0, "resolved": r["r"] or 0,
                        "false_positive": r["fp"] or 0, "acked": r["a"] or 0,
                        "fp_rate": round((r["fp"] or 0) / decided, 2) if decided else None})
        return out

    def backup(self, dest: str) -> int:
        """Consistent copy of the store. SQLite→SQLite uses the backup API; any other pair uses `copy_store`
        (PostgreSQL→SQLite file is a portable logical backup). Returns the copied rows or, for a file, its size in bytes."""
        if self.backend == "sqlite" and not _is_pg(dest):
            out = sqlite3.connect(dest)
            try:
                self.con.backup(out)
            finally:
                out.close()
            os.chmod(dest, 0o600)    # holds token hashes and metadata: only the owner reads it
            return os.path.getsize(dest)
        n = copy_store(self, Store(dest))
        if not _is_pg(dest) and os.path.exists(dest):
            os.chmod(dest, 0o600)
            return os.path.getsize(dest)
        return n

    def prune(self, days: float, now: float | None = None) -> dict:
        cut = (now or time.time()) - days * 86400
        c = self.con
        res = {}
        for name, sql in (("samples", "delete from samples where ts < ?"), ("history", "delete from history where ts < ?"),
                          ("audit", "delete from audit where ts < ?"),
                          ("findings", "delete from findings where status in ('resolved','false_positive') and last_seen < ?"),
                          ("runs", "delete from runs where coalesce(start, 0) < ?")):
            res[name] = c.execute(sql, (cut,)).rowcount
        c.commit()
        return res

    def ack(self, finding_id: str, actor: str, reason: str, until: float | None = None) -> None:
        self.con.execute("update findings set status='acked', ack_by=?, ack_reason=?, ack_until=? where id=?", (actor, reason, until, finding_id))
        self.audit(actor, "ack", {"id": finding_id, "reason": reason, "until": until})
        self.con.commit()

    # history / samples / runs ------------------------------------------------
    def add_history(self, key: str, ts: float, value: float) -> None:
        self.con.execute("insert into history values(?,?,?)", (key, ts, value))
        self.con.commit()

    def history(self, key: str) -> list[tuple[float, float]]:
        return [(r[0], r[1]) for r in self.con.execute("select ts, value from history where key=? order by ts", (key,))]

    def add_samples(self, samples) -> None:
        self.con.executemany("insert into samples values(?,?,?,?,?,?)", [(s.ts, s.source, s.scope, s.metric, s.value, s.limit) for s in samples])
        self.con.commit()

    def recent_samples(self, since: float):
        from .monitoring import Sample
        return [Sample(r["ts"], r["source"], r["scope"], r["metric"], r["value"], r["lim"])
                for r in self.con.execute("select * from samples where ts>=? order by ts", (since,))]

    def upsert_runs(self, runs) -> None:
        self._upsert("runs", ("system", "pipeline", "run_id", "state", "start", "end", "duration", "url"), ("system", "pipeline", "run_id"),
                             [(r.system, r.pipeline, r.run_id, r.state, r.start, r.end, r.duration, r.url) for r in runs])
        self.con.commit()

    def runs(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self.con.execute("select * from runs order by coalesce(start,0) desc limit ?", (limit,))]

    def upsert_ol_job(self, job: str, inputs: list, outputs: list, ts: float) -> None:
        self._upsert("ol_jobs", ("job", "inputs", "outputs", "last_event"), ("job",), [(job, json.dumps(inputs), json.dumps(outputs), ts)])
        self.con.commit()

    def add_ol_edges(self, job: str, edges: list[tuple]) -> None:
        self._upsert("ol_edges", ("job", "src_ds", "src_col", "dst_ds", "dst_col"), (), [(job, *e) for e in edges], ignore=True)
        self.con.commit()

    def ol_jobs(self) -> list[dict]:
        return [{"job": r["job"], "inputs": json.loads(r["inputs"]), "outputs": json.loads(r["outputs"]), "last_event": r["last_event"]}
                for r in self.con.execute("select * from ol_jobs order by job")]

    def ol_edges(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("select * from ol_edges order by job, dst_ds, dst_col")]

    # team: users, roles, assignment and comments ----------------------------
    @staticmethod
    def _hash(token: str) -> str:
        import hashlib
        return hashlib.sha256(("intentflow|" + token).encode()).hexdigest()

    def add_user(self, name: str, role: str, email: str | None = None, random_token: bool = False, expires_days: float | None = None) -> str:
        """Create the user and return the token (shown only once; only its hash is stored)."""
        import re, secrets
        if role not in ("viewer", "engineer", "admin"):
            raise ValueError(tr("invalid role (viewer, engineer, admin)"))
        if not re.fullmatch(r"[A-Za-z0-9_.@+-]{2,128}", name):
            raise ValueError(tr("invalid name (2–64 characters: letters, digits, _ . @ -)"))
        if self.con.execute("select 1 from users where name=?", (name,)).fetchone():
            raise ValueError(tr("user '{name}' already exists", name=name))
        token = "if_" + secrets.token_urlsafe(32)
        exp = time.time() + expires_days * 86400 if expires_days else None
        self.con.execute("insert into users(name, role, token_hash, created, disabled, last_seen, email, token_expires) values(?,?,?,?,0,NULL,?,?)",
                         (name, role, self._hash(token), time.time(), email, exp))
        self.con.commit()
        return token

    def rotate_token(self, name: str, expires_days: float | None = None) -> str:
        import secrets
        if not self.con.execute("select 1 from users where name=?", (name,)).fetchone():
            raise ValueError(tr("user not found"))
        token = "if_" + secrets.token_urlsafe(32)
        if expires_days is None:   # inherit the policy: the server's default validity, if any
            d = self.get_setting("policy.token_max_age_days")
            expires_days = float(d) if d else None
        exp = time.time() + expires_days * 86400 if expires_days else None
        self.con.execute("update users set token_hash=?, token_expires=? where name=?", (self._hash(token), exp, name))
        self.con.execute("delete from sessions where \"user\"=?", (name,))       # rotating ends open sessions
        self.con.commit()
        return token

    def users_admin(self) -> list[dict]:
        """For team management: includes projects, MFA and e-mail (never the token hash)."""
        out = []
        for r in self.con.execute("select name, role, created, disabled, last_seen, project_roles, totp_enabled, email, token_expires, scopes from users order by name"):
            d = dict(r)
            d["project_roles"] = json.loads(d.get("project_roles") or "{}")
            d["scopes"] = json.loads(d.get("scopes") or "{}")
            d["mfa"] = bool(d.pop("totp_enabled"))
            out.append(d)
        return out

    def users(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("select name, role, created, disabled, last_seen from users order by name")]

    def has_users(self) -> bool:
        return self.con.execute("select 1 from users limit 1").fetchone() is not None

    def user_by_token(self, token: str):
        import hmac
        h = self._hash(token or "")
        rows = self.con.execute("select name, role, token_hash, disabled, last_seen, project_roles, totp_enabled, token_expires, scopes from users where token_hash=?", (h,)).fetchall()   # busca indexada; cursor fechado antes de escrever
        for r in rows:
            if hmac.compare_digest(r["token_hash"], h):
                if r["disabled"]:
                    return None
                now = time.time()
                if r["token_expires"] and now > r["token_expires"]:
                    return None                                              # expired token: requires rotation
                if not r["last_seen"] or now - r["last_seen"] > 60:      # at most one write per minute per person
                    self.con.execute("update users set last_seen=? where name=?", (now, r["name"]))
                    self.con.commit()
                return {"name": r["name"], "role": r["role"], "project_roles": json.loads(r["project_roles"] or "{}"),
                        "mfa": bool(r["totp_enabled"]), "scopes": json.loads(r["scopes"] or "{}")}
        return None

    # sessions (the browser cookie holds a random id, never the long-lived token)
    def create_session(self, user: str, via: str = "token", ttl: float = 12 * 3600, id_token: str | None = None,
                       sub: str | None = None, sid: str | None = None, refresh_token: str | None = None) -> str:
        import secrets
        sess = "ifs_" + secrets.token_urlsafe(32)
        now = time.time()
        self.con.execute("insert into sessions(id_hash, \"user\", created, expires, via, id_token, sub, sid, refresh_token, refreshed_at) "
                         "values(?,?,?,?,?,?,?,?,?,?)", (self._hash(sess), user, now, now + ttl, via, self.box.enc(id_token), sub, sid,
                                                          self.box.enc(refresh_token), now))
        self.con.execute("delete from sessions where expires < ?", (now,))
        self.con.commit()
        return sess

    def user_by_session(self, sid: str):
        r = self.con.execute("select s.\"user\", s.expires, s.via, u.role, u.disabled, u.project_roles, u.totp_enabled, u.scopes from sessions s "
                             "join users u on u.name = s.\"user\" where s.id_hash=?", (self._hash(sid or ""),)).fetchone()
        if not r or r["expires"] < time.time() or r["disabled"]:
            return None
        return {"name": r["user"], "role": r["role"], "project_roles": json.loads(r["project_roles"] or "{}"), "mfa": bool(r["totp_enabled"]),
                "via": r["via"], "scopes": json.loads(r["scopes"] or "{}")}

    @property
    def box(self):
        """Encrypts/decrypts secrets at rest (see secrets_box). Created on demand, once `self.path` exists."""
        if self._box is None:
            from .secrets_box import Box
            self._box = Box(self.path, self.backend)
        return self._box

    def session_info(self, sid: str):
        r = self.con.execute("select \"user\", via, id_token, sub, sid, refresh_token, refreshed_at from sessions where id_hash=?",
                             (self._hash(sid or ""),)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["id_token"], d["refresh_token"] = self.box.dec(d.get("id_token")), self.box.dec(d.get("refresh_token"))
        return d

    def update_session_refresh(self, sid: str, refresh_token: str | None, id_token: str | None) -> None:
        self.con.execute("update sessions set refresh_token=coalesce(?, refresh_token), id_token=coalesce(?, id_token), refreshed_at=? where id_hash=?",
                         (self.box.enc(refresh_token), self.box.enc(id_token), time.time(), self._hash(sid)))
        self.con.commit()

    def end_sessions_by_provider(self, sid: str | None, sub: str | None) -> int:
        """Back-channel logout: end sessions by the provider's `sid` (or, without it, by `sub`)."""
        if sid:
            cur = self.con.execute("delete from sessions where sid=?", (sid,))
        elif sub:
            cur = self.con.execute("delete from sessions where sub=?", (sub,))
        else:
            return 0
        self.con.commit()
        return cur.rowcount

    def end_user_sessions(self, name: str) -> int:
        cur = self.con.execute("delete from sessions where \"user\"=?", (name,))
        self.con.commit()
        return cur.rowcount

    def sso_sync(self, name: str, role: str | None, project_roles: dict | None) -> None:
        """Apply roles coming from the provider (groups) to a user managed by SSO."""
        sets, vals = ["sso_managed=1"], []
        if role is not None:
            sets.append("role=?")
            vals.append(role)
        if project_roles is not None:
            sets.append("project_roles=?")
            vals.append(json.dumps(project_roles))
        self.con.execute(f"update users set {', '.join(sets)} where name=?", (*vals, name))
        self.con.commit()

    def end_session(self, sid: str) -> None:
        self.con.execute("delete from sessions where id_hash=?", (self._hash(sid or ""),))
        self.con.commit()

    def user_record(self, name: str):
        r = self.con.execute("select * from users where name=?", (name,)).fetchone()
        return dict(r) if r else None

    # MFA (TOTP)
    def mfa_begin(self, name: str) -> str:
        from . import totp
        record = self.user_record(name)
        if not record:
            raise ValueError(tr("user not found"))
        if record["totp_enabled"]:
            raise ValueError(tr("MFA is already on; disable it with the current code before setting it up again"))
        secret = totp.new_secret()
        self.con.execute("update users set totp_secret=?, totp_enabled=0, totp_last_step=0 where name=?", (self.box.enc(secret), name))
        self.con.commit()
        return secret

    def mfa_confirm(self, name: str, code: str) -> bool:
        from . import totp
        u = self.user_record(name)
        step = totp.verify(self.box.dec(u["totp_secret"]) or "", code) if u and u["totp_secret"] else None
        if step is None or u["totp_enabled"]:
            return False
        changed = self.con.execute("update users set totp_enabled=1, totp_last_step=? where name=? and totp_enabled=0 and totp_secret=?",
                                   (step, name, u["totp_secret"])).rowcount
        self.con.commit()
        return changed == 1

    def mfa_check(self, name: str, code: str) -> bool:
        """Validate the code and prevent reuse of the same step."""
        from . import totp
        u = self.user_record(name)
        if not u or not u["totp_enabled"]:
            return False
        step = totp.verify(self.box.dec(u["totp_secret"]), code, last_step=u["totp_last_step"] or 0)
        if step is None:
            return False
        changed = self.con.execute("update users set totp_last_step=? where name=? and totp_enabled=1 and totp_secret=? "
                                   "and coalesce(totp_last_step, 0) < ?", (step, name, u["totp_secret"], step)).rowcount
        self.con.commit()
        return changed == 1

    def mfa_recovery_codes(self, name: str) -> list[str]:
        """Generate 10 single-use codes (shown once); only their hashes are stored."""
        import secrets
        codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(10)]
        self.con.execute("update users set recovery_hashes=? where name=?", (json.dumps([self._hash("rc|" + c) for c in codes]), name))
        self.con.commit()
        return codes

    def mfa_use_recovery(self, name: str, code: str) -> bool:
        u = self.user_record(name)
        hashes = json.loads((u or {}).get("recovery_hashes") or "[]")
        h = self._hash("rc|" + (code or "").strip().lower())
        if h not in hashes:
            return False
        hashes.remove(h)                       # single use
        changed = self.con.execute("update users set recovery_hashes=? where name=? and recovery_hashes=? and totp_enabled=1",
                                   (json.dumps(hashes), name, u["recovery_hashes"])).rowcount
        self.con.commit()
        return changed == 1

    def set_scopes(self, name: str, project: str, patterns: list[str] | None) -> None:
        u = self.user_record(name)
        if not u:
            raise ValueError(tr("user not found"))
        sc = json.loads(u.get("scopes") or "{}")
        if patterns:
            sc[project] = patterns
        else:
            sc.pop(project, None)
        self.con.execute("update users set scopes=? where name=?", (json.dumps(sc), name))
        self.con.commit()

    def mfa_disable(self, name: str) -> None:
        self.con.execute("update users set totp_secret=NULL, totp_enabled=0, totp_last_step=0, recovery_hashes=NULL where name=?", (name,))
        self.con.commit()

    def set_project_role(self, name: str, project: str, role: str | None) -> None:
        if role not in (None, "none", "viewer", "engineer", "admin"):
            raise ValueError(tr("invalid role"))
        u = self.user_record(name)
        if not u:
            raise ValueError(tr("user not found"))
        pr = json.loads(u.get("project_roles") or "{}")
        if role is None:
            pr.pop(project, None)
        else:
            pr[project] = role
        self.con.execute("update users set project_roles=? where name=?", (json.dumps(pr), name))
        self.con.commit()

    # persistent attempt limit (survives restarts and holds across processes)
    def attempts_recent(self, ip: str, window: float = 60.0) -> int:
        return self.con.execute("select count(*) from login_attempts where ip=? and ts>?", (ip, time.time() - window)).fetchone()[0]

    def attempt_failed(self, ip: str) -> None:
        self.con.execute("insert into login_attempts values(?,?)", (ip, time.time()))
        self.con.execute("delete from login_attempts where ts < ?", (time.time() - 3600,))
        self.con.commit()

    def set_user(self, name: str, role: str | None = None, disabled: bool | None = None) -> None:
        if role is not None:
            if role not in ("viewer", "engineer", "admin"):
                raise ValueError(tr("invalid role"))
            self.con.execute("update users set role=? where name=?", (role, name))
        if disabled is not None:
            self.con.execute("update users set disabled=? where name=?", (int(disabled), name))
        self.con.commit()

    def assign(self, finding_id: str, assignee: str | None, actor: str) -> None:
        if self.con.execute("select 1 from findings where id=?", (finding_id,)).fetchone() is None:
            raise ValueError(tr("finding not found"))
        self.con.execute("update findings set assignee=?, assigned_by=? where id=?", (assignee or None, actor, finding_id))
        self.audit(actor, "assign", {"id": finding_id, "assignee": assignee})
        self.con.commit()

    def add_note(self, finding_id: str, author: str, text: str) -> None:
        text = (text or "").strip()
        if not text or len(text) > 2000:
            raise ValueError(tr("comment is empty or longer than 2000 characters"))
        if self.con.execute("select 1 from findings where id=?", (finding_id,)).fetchone() is None:
            raise ValueError(tr("finding not found"))
        self.con.execute("insert into notes(finding_id, author, ts, text) values(?,?,?,?)", (finding_id, author, time.time(), text))
        self.audit(author, "note", {"id": finding_id})
        self.con.commit()

    def notes(self, finding_id: str) -> list[dict]:
        return [dict(r) for r in self.con.execute("select author, ts, text from notes where finding_id=? order by id", (finding_id,))]

    # cache de linhagem por modelo
    def get(self, key: str):
        r = self.con.execute("select value from lineage_cache where key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else None

    def set(self, key: str, value) -> None:
        self._upsert("lineage_cache", ("key", "value"), ("key",), [(key, json.dumps(value))])

    def flush(self) -> None:
        self.con.commit()

    # settings and policies (instead of YAML files) --------------------------
    def set_setting(self, key: str, value) -> None:
        self._upsert("settings", ("key", "value", "updated"), ("key",), [(key, json.dumps(value), time.time())])
        self.con.commit()

    def get_setting(self, key: str, default=None):
        r = self.con.execute("select value from settings where key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def unset_setting(self, key: str) -> None:
        self.con.execute("delete from settings where key=?", (key,))
        self.con.commit()

    def settings(self, prefix: str = "") -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.con.execute("select key, value from settings where key like ? order by key", (prefix + "%",))}

    def policies(self) -> dict:
        """Policies as a plain dict (e.g. pii_allowed_models, sla.<pipeline>)."""
        out: dict = {}
        for k, v in self.settings("policy.").items():
            out[k.removeprefix("policy.")] = v
        return out

    # propostas / auditoria --------------------------------------------------
    def put_proposal(self, pid: str, data: dict, status: str) -> None:
        self._upsert("proposals", ("id", "json", "status", "updated"), ("id",), [(pid, json.dumps(data), status, time.time())])
        self.con.commit()

    def proposal(self, pid: str) -> dict | None:
        r = self.con.execute("select * from proposals where id=?", (pid,)).fetchone()
        return {**json.loads(r["json"]), "id": r["id"], "status": r["status"]} if r else None

    def proposals(self) -> list[dict]:
        return [{**json.loads(r["json"]), "id": r["id"], "status": r["status"]}
                for r in self.con.execute("select * from proposals order by updated desc")]

    def audit(self, actor: str, event: str, detail: dict) -> None:
        self.con.execute("insert into audit values(?,?,?,?)", (time.time(), actor, event, json.dumps(detail, default=str)))
        self.con.commit()

    def audit_log(self, limit: int | None = None) -> list[dict]:
        if limit is None:
            return [dict(r) for r in self.con.execute("select * from audit order by ts")]
        rows = self.con.execute("select * from audit order by ts desc limit ?", (max(0, limit),)).fetchall()
        return [dict(r) for r in reversed(rows)]


TABLES = ("findings", "history", "samples", "runs", "proposals", "audit", "ol_jobs", "ol_edges", "lineage_cache",
          "users", "notes", "sessions", "login_attempts", "settings")


def copy_store(src: "Store", dst: "Store", clear: bool = True) -> int:
    """Copy every table from `src` to `dst` (SQLite→PostgreSQL migration, logical backup, restore).
    Reads `src` in a consistent snapshot. Returns the total number of rows."""
    q = lambda c: f'"{c}"' if c in ("end", "user") else c    # noqa: E731
    total = 0
    if src.backend == "postgres":
        src.con.execute("begin isolation level repeatable read read only") if False else None
    if clear:
        for t in reversed(TABLES):
            dst.con.execute(f"delete from {t}")
        dst.con.commit()
    for t in TABLES:
        rows = src.con.execute(f"select * from {t}").fetchall()
        if not rows:
            continue
        cols = list(rows[0].keys())
        cols_dst = {r[1] for r in dst.con.execute(f"pragma table_info({t})").fetchall()} if dst.backend == "sqlite" else None
        use = [c for c in cols if cols_dst is None or c in cols_dst]
        ph = ",".join("?" * len(use))
        sql = f"insert into {t}({', '.join(q(c) for c in use)}) values({ph})"
        for r in rows:
            dst.con.execute(sql, tuple(r[c] for c in use))
        total += len(rows)
    if dst.backend == "postgres":
        dst.con.execute("select setval(pg_get_serial_sequence('notes','id'), greatest(coalesce((select max(id) from notes), 0), 1))")
    dst.con.commit()
    return total


def migrate_secrets(store: "Store") -> dict:
    """Encrypt secrets still in plain text (stores created before encryption at rest). Idempotent."""
    if not store.box.enabled:
        raise RuntimeError(tr("no key: set INTENTFLOW_SECRET_KEY (or use a SQLite file)"))
    n = {"totp": 0, "sessions": 0}
    for r in store.con.execute("select name, totp_secret from users where totp_secret is not null").fetchall():
        if r["totp_secret"] and not str(r["totp_secret"]).startswith("enc:v1:"):
            store.con.execute("update users set totp_secret=? where name=?", (store.box.enc(r["totp_secret"]), r["name"]))
            n["totp"] += 1
    for r in store.con.execute("select id_hash, id_token, refresh_token from sessions").fetchall():
        if any(v and not str(v).startswith("enc:v1:") for v in (r["id_token"], r["refresh_token"])):
            store.con.execute("update sessions set id_token=?, refresh_token=? where id_hash=?",
                              (store.box.enc(r["id_token"]), store.box.enc(r["refresh_token"]), r["id_hash"]))
            n["sessions"] += 1
    store.con.commit()
    return n
