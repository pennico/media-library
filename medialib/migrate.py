"""Versioned SQL migrations (DESIGN.md §5.4).

``migrations/NNNN_name.sql``, forward-only, applied in order as ml_migrator
after ``SET ROLE ml_owner``. Each file runs in one transaction together with
its row in ``meta.schema_migration``; a changed checksum of an applied file
stops everything.
"""
import hashlib
import os
import re
import time

import psycopg

from . import db

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
LOCK_KEY = 7420010002

META = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.schema_migration (
    version      integer PRIMARY KEY CHECK (version > 0),
    name         text NOT NULL,
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at   timestamptz NOT NULL DEFAULT now(),
    applied_by   name NOT NULL DEFAULT session_user,
    duration_ms  integer NOT NULL CHECK (duration_ms >= 0)
);
"""


class MigrationError(RuntimeError):
    pass


def files(directory=MIGRATIONS_DIR):
    out = []
    for name in sorted(os.listdir(directory)):
        m = _NAME.match(name)
        if not m:
            continue
        with open(os.path.join(directory, name), "rb") as fh:
            body = fh.read()
        out.append({"version": int(m.group(1)), "name": m.group(2),
                    "sha256": hashlib.sha256(body).hexdigest(), "sql": body.decode("utf-8")})
    versions = [f["version"] for f in out]
    if len(set(versions)) != len(versions):
        raise MigrationError("duplicate migration version")
    return out


def latest_version(directory=MIGRATIONS_DIR):
    fs = files(directory)
    return fs[-1]["version"] if fs else 0


def status(conn, directory=MIGRATIONS_DIR):
    """[(file, applied_row or None, problem or None)]"""
    applied = {}
    if conn.execute("SELECT to_regclass('meta.schema_migration') AS t").fetchone()["t"]:
        applied = {r["version"]: r for r in conn.execute("SELECT * FROM meta.schema_migration")}
    out = []
    for f in files(directory):
        row = applied.pop(f["version"], None)
        problem = None
        if row and row["sha256"] != f["sha256"]:
            problem = "checksum changed since it was applied"
        out.append((f, row, problem))
    for version, row in sorted(applied.items()):
        out.append(({"version": version, "name": row["name"]}, row, "applied but file missing"))
    return out


def migrate(cfg, dbname=None, directory=MIGRATIONS_DIR, check_only=False, log=print):
    with db.connect(cfg, db.ROLE_MIGRATOR, dbname, autocommit=True) as conn:
        conn.execute("SET ROLE ml_owner")
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            if not check_only:
                conn.execute(META)
            pending = []
            for f, row, problem in status(conn, directory):
                if problem:
                    raise MigrationError("{:04d}_{}: {}".format(f["version"], f["name"], problem))
                if row is None:
                    pending.append(f)
            if check_only:
                for f in pending:
                    log("pending {:04d}_{}".format(f["version"], f["name"]))
                return [f["version"] for f in pending]
            for f in pending:
                t0 = time.monotonic()
                try:
                    with conn.transaction():
                        conn.execute("SELECT set_config('ml.source', 'system', true),"
                                     " set_config('ml.client', %s, true)",
                                     ("migration:{:04d}".format(f["version"]),))
                        conn.execute(f["sql"])
                        conn.execute("INSERT INTO meta.schema_migration (version, name, sha256, duration_ms)"
                                     " VALUES (%s, %s, %s, %s)",
                                     (f["version"], f["name"], f["sha256"],
                                      int((time.monotonic() - t0) * 1000)))
                except psycopg.Error as e:
                    raise MigrationError("{:04d}_{} failed (nothing of it was applied): {}".format(
                        f["version"], f["name"], e)) from e
                log("applied {:04d}_{} ({} ms)".format(f["version"], f["name"],
                                                        int((time.monotonic() - t0) * 1000)))
            return [f["version"] for f in pending]
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
