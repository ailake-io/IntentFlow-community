import os, shutil, subprocess, sys
from pathlib import Path

os.environ["INTENTFLOW_LANG"] = "en"      # assertions use the English source strings

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _build(src: Path, dst: Path):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("target", "logs"))
    dbt = str(Path(sys.executable).parent / "dbt")
    for cmd in (["build"], ["docs", "generate"]):
        r = subprocess.run([dbt, *cmd, "--profiles-dir", "."], cwd=dst, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]
    return dst


@pytest.fixture(scope="session")
def shop_target(tmp_path_factory):
    """Example dbt project compiled in a temporary directory (real manifest + catalog)."""
    return _build(ROOT / "examples/dbt_shop", tmp_path_factory.mktemp("shop") / "shop")


@pytest.fixture(scope="session")
def lab_target(tmp_path_factory):
    """Lab: deliberately bad SQL + hard lineage cases."""
    return _build(ROOT / "examples/dbt_lab", tmp_path_factory.mktemp("lab") / "lab")


# ---- test store: SQLite (default) or PostgreSQL (INTENTFLOW_TEST_BACKEND=postgres) ----
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

BACKEND = os.environ.get("INTENTFLOW_TEST_BACKEND", "sqlite")
PG_ADMIN = os.environ.get("INTENTFLOW_TEST_PG_ADMIN", "postgresql://postgres:intentflow-test@127.0.0.1:55432/postgres")
_CREATED: list = []


def _pg_admin():
    import psycopg2
    c = psycopg2.connect(PG_ADMIN, connect_timeout=5)
    c.autocommit = True
    return c


def new_db(tmp_path, name="s.db", backend=None):
    """SQLite path or DSN of a new, empty PostgreSQL database (one per call)."""
    backend = backend or BACKEND
    if backend != "postgres":
        return str(tmp_path / name)
    dbname = "ift_" + uuid.uuid4().hex[:12]
    c = _pg_admin()
    c.cursor().execute(f'create database "{dbname}"')
    c.close()
    _CREATED.append(dbname)
    u = urlsplit(PG_ADMIN)
    return urlunsplit((u.scheme, u.netloc, "/" + dbname, "", ""))


@pytest.fixture(scope="session", autouse=True)
def _drop_test_databases():
    yield
    if _CREATED:
        try:
            c = _pg_admin()
            for d in _CREATED:
                c.cursor().execute(f'drop database if exists "{d}" with (force)')
            c.close()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _close_pg_pools():
    yield
    from intentflow.store import close_pools
    close_pools()

