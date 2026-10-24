"""Connections and audited write transactions.

Every write runs inside ``write_tx``: it declares the source (where the
values come from: owner, yamtrack, apple, ...) and the client (who writes:
``console:<login>``, ``importer``, ...). The database refuses writes without
them (migrations/0004, 0005).
"""
import contextlib

import psycopg
from psycopg.rows import dict_row

ROLE_APP = "ml_app"
ROLE_READONLY = "ml_readonly"
ROLE_MIGRATOR = "ml_migrator"
ROLE_SUPERUSER = "postgres"


def connect(cfg, role=ROLE_APP, dbname=None, autocommit=True):
    """Autocommit by default: every write then goes through ``write_tx``, a
    real BEGIN/COMMIT (never a savepoint), so deferred constraints are checked."""
    return psycopg.connect(cfg.dsn(role, dbname), autocommit=autocommit, row_factory=dict_row)


@contextlib.contextmanager
def write_tx(conn, source, client, reason=None):
    """One transaction with ml.source / ml.client / ml.reason set locally.

    Refuses to nest: inside an open transaction psycopg would only make a
    savepoint, and the deferred consistency checks run at the real COMMIT."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("write_tx needs an idle connection (no transaction already open)")
    with conn.transaction():
        conn.execute("SELECT set_config('ml.source', %s, true), set_config('ml.client', %s, true),"
                     " set_config('ml.reason', %s, true)", (source, client, reason or ""))
        yield conn


def set_source(conn, source):
    """Change the declared source inside an open write_tx (importer: per value group)."""
    conn.execute("SELECT set_config('ml.source', %s, true)", (source,))
