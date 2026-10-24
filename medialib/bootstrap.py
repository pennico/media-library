"""Cluster bootstrap (superuser, idempotent): roles, database, role settings.

DESIGN.md §5.2. Passwords are read from the secrets directory and sent to the
server only as SCRAM verifiers computed client-side (libpq
PQencryptPasswordConn), so the plain text never reaches the server or its logs.
"""
import psycopg
from psycopg import sql

from . import db

LOGIN_ROLES = {
    # role: (connection limit, settings)
    "ml_migrator": (3, {}),
    "ml_app": (10, {"statement_timeout": "10s", "idle_in_transaction_session_timeout": "60s",
                    "lock_timeout": "5s"}),
    "ml_readonly": (5, {"statement_timeout": "5s", "default_transaction_read_only": "on",
                        "idle_in_transaction_session_timeout": "60s"}),
}


def _exists(conn, query, *args):
    return conn.execute(query, args).fetchone() is not None


def bootstrap(cfg, dbname=None, log=print):
    dbname = dbname or cfg.db_name
    with psycopg.connect(cfg.dsn(db.ROLE_SUPERUSER, "postgres"), autocommit=True) as conn:
        if not _exists(conn, "SELECT 1 FROM pg_roles WHERE rolname = 'ml_owner'"):
            conn.execute("CREATE ROLE ml_owner NOLOGIN")
            log("created role ml_owner")
        for role, (limit, settings) in LOGIN_ROLES.items():
            verifier = conn.pgconn.encrypt_password(cfg.password(role).encode(), role.encode(),
                                                    b"scram-sha-256").decode()
            if _exists(conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", role):
                conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT {}").format(
                    sql.Identifier(role), sql.Literal(verifier), sql.Literal(limit)))
            else:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT {}").format(
                    sql.Identifier(role), sql.Literal(verifier), sql.Literal(limit)))
                log("created role " + role)
            for key, value in settings.items():
                conn.execute(sql.SQL("ALTER ROLE {} SET {} = {}").format(
                    sql.Identifier(role), sql.Identifier(key), sql.Literal(value)))
        # the migrator may become ml_owner, but holds none of its rights until SET ROLE
        conn.execute("GRANT ml_owner TO ml_migrator WITH INHERIT FALSE, SET TRUE")
        if not _exists(conn, "SELECT 1 FROM pg_database WHERE datname = %s", dbname):
            conn.execute(sql.SQL(
                "CREATE DATABASE {} OWNER ml_owner TEMPLATE template0 ENCODING 'UTF8'"
                " LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8'").format(sql.Identifier(dbname)))
            log("created database " + dbname)
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(dbname)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO ml_migrator, ml_app, ml_readonly").format(
            sql.Identifier(dbname)))
        conn.execute(sql.SQL("GRANT CREATE, TEMPORARY ON DATABASE {} TO ml_owner").format(
            sql.Identifier(dbname)))


def drop_database(cfg, dbname):
    """Tests only: drop a scratch database."""
    with psycopg.connect(cfg.dsn(db.ROLE_SUPERUSER, "postgres"), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
