#!/usr/bin/env python3
"""Backup restore drill on the development cluster (DESIGN.md §5.5-5.6).

The same two paths the production restore test runs (ops/restore-test.sh,
phase 4), here against .devdb:

1. logical: pg_dump -Fc the database, pg_restore it into a scratch database,
   compare row counts of every table and verify the audit chain;
2. point in time: pg_basebackup, write marker A, note the time, write
   marker B, archive the WAL; restore the base backup into a new data
   directory, replay the archived WAL to the noted time, start it on its own
   socket, check that A is there and B is not, verify the audit chain and
   run pg_amcheck --heapallindexed.

    python3 tools/restore_drill.py [--db medialib_dev]

Writes out/restore-drill-<time>.json; exit code 0 only if every check passed.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import devdb  # noqa: E402
from medialib import config, db  # noqa: E402

TABLES_SQL = """SELECT n.nspname || '.' || c.relname AS t FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r' AND n.nspname IN ('catalog', 'library', 'audit', 'ingest', 'meta') ORDER BY 1"""


def counts(conn):
    out = {}
    for r in conn.execute(TABLES_SQL).fetchall():
        out[r["t"]] = conn.execute("SELECT count(*) AS n FROM {}".format(r["t"])).fetchone()["n"]
    return out


def pg(prog, *args, env=None, capture=False):
    e = devdb.environ()
    e["PGPASSWORD"] = open(os.path.join(devdb.SECRETS, "postgres.pw")).read().strip()
    e.update(env or {})
    r = subprocess.run([os.path.join(devdb.BIN, prog), *args], env=e, text=True,
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("{} failed: {}".format(prog, (r.stderr or r.stdout)[-800:]))
    return r.stdout if capture else r.stderr


def marker(cfg, name):
    with db.connect(cfg, db.ROLE_APP) as conn:
        with db.write_tx(conn, "owner", "restore-drill"):
            conn.execute("INSERT INTO library.shelf (name, kind, media_kind, position)"
                         " VALUES (%s, 'manual', 'book', (SELECT max(position) + 1 FROM library.shelf"
                         " WHERE media_kind = 'book'))", (name,))


def logical(cfg, work, result):
    t0 = time.monotonic()
    dump = os.path.join(work, "medialib.dump")
    pg("pg_dump", "-h", cfg.db_host, "-U", "postgres", "-Fc", "-f", dump, cfg.db_name)
    pg("pg_dumpall", "-h", cfg.db_host, "-U", "postgres", "--globals-only", "--no-role-passwords",
       "-f", os.path.join(work, "globals.sql"))
    target = cfg.db_name + "_restored"
    with db.connect(cfg, db.ROLE_SUPERUSER, "postgres") as su:
        su.execute("DROP DATABASE IF EXISTS {} WITH (FORCE)".format(target))
        su.execute("CREATE DATABASE {} TEMPLATE template0".format(target))
    pg("pg_restore", "-h", cfg.db_host, "-U", "postgres", "--exit-on-error", "-d", target, dump)
    with db.connect(cfg, db.ROLE_SUPERUSER) as src, db.connect(cfg, db.ROLE_SUPERUSER, target) as dst:
        a, b = counts(src), counts(dst)
        chain = dst.execute("SELECT * FROM audit.verify()").fetchone()
    with db.connect(cfg, db.ROLE_SUPERUSER, "postgres") as su:
        su.execute("DROP DATABASE {} WITH (FORCE)".format(target))
    diff = {t: (a.get(t), b.get(t)) for t in set(a) | set(b) if a.get(t) != b.get(t)}
    result["logical"] = {"ok": not diff and chain["first_bad"] is None, "seconds": round(time.monotonic() - t0, 1),
                         "dump_bytes": os.path.getsize(dump), "tables": len(a), "rows": sum(a.values()),
                         "differences": diff, "audit_checked": chain["checked"], "audit_first_bad": chain["first_bad"]}


def pitr(cfg, work, result):
    t0 = time.monotonic()
    base = os.path.join(work, "base")
    pg("pg_basebackup", "-h", cfg.db_host, "-U", "postgres", "-D", base, "-Ft", "-z", "-X", "fetch",
       "--checkpoint=fast", "--manifest-checksums=SHA256")
    tag = datetime.datetime.now(datetime.timezone.utc).strftime("%H%M%S%f")
    a_name, b_name = "PITR marker A " + tag, "PITR marker B " + tag
    marker(cfg, a_name)
    time.sleep(1.5)
    with db.connect(cfg, db.ROLE_SUPERUSER) as su:
        target_time = su.execute("SELECT now() AS t").fetchone()["t"]
    time.sleep(1.5)
    marker(cfg, b_name)
    with db.connect(cfg, db.ROLE_SUPERUSER) as su:
        su.execute("SELECT pg_switch_wal()")
        expected = counts(su)
    time.sleep(2)                                   # let the archiver copy the last segment
    data = os.path.join(work, "pitr-data")
    os.makedirs(data, mode=0o700)
    subprocess.run(["tar", "-xzf", os.path.join(base, "base.tar.gz"), "-C", data], check=True)
    shutil.copy(os.path.join(base, "backup_manifest"), os.path.join(data, "backup_manifest"))
    pg("pg_verifybackup", "--no-parse-wal", data)
    os.remove(os.path.join(data, "backup_manifest"))
    sock = os.path.join(work, "sock")
    os.makedirs(sock)
    with open(os.path.join(data, "postgresql.auto.conf"), "a") as fh:
        fh.write("unix_socket_directories = '{}'\n".format(sock))
        fh.write("listen_addresses = ''\narchive_mode = off\n")
        fh.write("restore_command = 'gzip -dc {}/%f.gz > %p'\n".format(devdb.ARCHIVE))
        fh.write("recovery_target_time = '{}'\n".format(target_time.isoformat()))
        fh.write("recovery_target_action = 'promote'\n")
    open(os.path.join(data, "recovery.signal"), "w").close()
    log = os.path.join(work, "pitr.log")
    pg("pg_ctl", "-D", data, "-l", log, "-w", "-t", "120", "start")
    try:
        rcfg = config.from_env(db_host=sock, db_name=cfg.db_name)
        with db.connect(rcfg, db.ROLE_SUPERUSER) as r:
            names = {x["name"] for x in r.execute("SELECT name FROM library.shelf")}
            chain = r.execute("SELECT * FROM audit.verify()").fetchone()
            got = counts(r)
            checksums = r.execute("SHOW data_checksums").fetchone()["data_checksums"]
        amcheck = pg("pg_amcheck", "-h", sock, "-U", "postgres", "--install-missing", "--heapallindexed",
                     "-d", cfg.db_name, capture=True)
    finally:
        pg("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")
    result["pitr"] = {
        "ok": a_name in names and b_name not in names and chain["first_bad"] is None and checksums == "on",
        "seconds": round(time.monotonic() - t0, 1), "target_time": target_time.isoformat(),
        "marker_a_present": a_name in names, "marker_b_absent": b_name not in names,
        "audit_checked": chain["checked"], "audit_first_bad": chain["first_bad"], "data_checksums": checksums,
        "amcheck": "ok" if not amcheck.strip() else amcheck.strip()[:500],
        "audit_rows_restored_vs_now": [got.get("audit.change"), expected.get("audit.change")],
    }


def cleanup_markers(cfg):
    with db.connect(cfg, db.ROLE_APP) as conn:
        with db.write_tx(conn, "owner", "restore-drill"):
            conn.execute("UPDATE library.shelf SET deleted_at = now() WHERE name LIKE 'PITR marker %%'"
                         " AND deleted_at IS NULL")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="medialib_dev")
    a = ap.parse_args(argv)
    for k, v in (line.replace("export ", "", 1).split("=", 1) for line in devdb.env_lines().splitlines()):
        os.environ.setdefault(k, v)
    cfg = config.from_env(db_name=a.db)
    result = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "database": a.db,
              "postgres": pg("postgres", "--version", capture=True).strip()}
    work = tempfile.mkdtemp(prefix="restore-drill-", dir=os.path.join(ROOT, ".devdb"))
    try:
        logical(cfg, work, result)
        pitr(cfg, work, result)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        cleanup_markers(cfg)
    os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
    path = os.path.join(ROOT, "out", "restore-drill-{}.json".format(
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")))
    with open(path, "w") as fh:
        json.dump(result, fh, indent=1, default=str)
    print(json.dumps(result, indent=1, default=str))
    print("report:", path)
    return 0 if result["logical"]["ok"] and result["pitr"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
