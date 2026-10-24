"""Test fixtures: a fresh database on the dev cluster (tools/devdb.py) per
session, bootstrapped and migrated like production."""
import contextlib
import os
import subprocess
import sys
import uuid

import psycopg
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from medialib import bootstrap, config, db, migrate  # noqa: E402


def _dev_env():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "devdb.py"), "env"],
                         capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        k, _, v = line.replace("export ", "", 1).partition("=")
        os.environ.setdefault(k, v)


@pytest.fixture(scope="session")
def cfg():
    _dev_env()
    name = "medialib_test_" + uuid.uuid4().hex[:8]
    c = config.from_env(db_name=name)
    try:
        psycopg.connect(c.dsn(db.ROLE_SUPERUSER, "postgres")).close()
    except psycopg.OperationalError as e:
        pytest.exit("dev cluster not running (python3 tools/devdb.py start): {}".format(e))
    bootstrap.bootstrap(c, log=lambda *_: None)
    migrate.migrate(c, log=lambda *_: None)
    yield c
    bootstrap.drop_database(c, name)


@pytest.fixture
def app(cfg):
    conn = db.connect(cfg, db.ROLE_APP)
    yield conn
    conn.close()


@pytest.fixture
def ro(cfg):
    conn = db.connect(cfg, db.ROLE_READONLY)
    yield conn
    conn.close()


@pytest.fixture
def su(cfg):
    conn = db.connect(cfg, db.ROLE_SUPERUSER, autocommit=True)
    yield conn
    conn.close()


@contextlib.contextmanager
def tx(conn, source="owner", client="test"):
    with db.write_tx(conn, source, client):
        yield conn


def new_book(conn, title="Test book", lang="eng", **item):
    """Insert a book work with its primary edition (inside the caller's tx)."""
    row = conn.execute(
        "INSERT INTO catalog.media_item (kind, title, title_language, locked) VALUES ('book', %s, %s, %s)"
        " RETURNING id", (title, lang, item.get("locked", False))).fetchone()
    conn.execute("INSERT INTO catalog.book (item_id) VALUES (%s)", (row["id"],))
    ed = conn.execute("INSERT INTO catalog.edition (work_id, title, language, is_primary)"
                      " VALUES (%s, %s, %s, true) RETURNING id", (row["id"], title, lang)).fetchone()
    return row["id"], ed["id"]


def add_entry(conn, item_id, status="planned"):
    conn.execute("INSERT INTO library.entry (item_id, status) VALUES (%s, %s)", (item_id, status))
