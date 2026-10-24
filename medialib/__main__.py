"""medialib command line.

    python -m medialib bootstrap                 # superuser, once per cluster
    python -m medialib migrate [--check]
    python -m medialib import SNAPSHOT_DIR [--apply] [--offline] [--out DIR]
    python -m medialib serve [--host 0.0.0.0] [--port 8099]
"""
import argparse
import sys

from . import bootstrap, config, migrate


def main(argv=None):
    ap = argparse.ArgumentParser(prog="medialib", description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="database name (default MEDIALIB_DB_NAME or medialib)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap", help="roles + database (superuser, idempotent)")
    m = sub.add_parser("migrate", help="apply pending migrations")
    m.add_argument("--check", action="store_true", help="list pending, apply nothing")
    i = sub.add_parser("import", help="import a Yamtrack/CWA snapshot (dry run unless --apply)")
    i.add_argument("snapshot")
    i.add_argument("--apply", action="store_true", help="commit (default: dry run, rolled back)")
    i.add_argument("--offline", action="store_true", help="no downloads; only cached/local covers")
    i.add_argument("--out", default="out", help="report directory")
    s = sub.add_parser("serve", help="run the console (page + API)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8099)
    a = ap.parse_args(argv)
    cfg = config.from_env(**({"db_name": a.db} if a.db else {}))
    if a.cmd == "bootstrap":
        bootstrap.bootstrap(cfg)
    elif a.cmd == "migrate":
        pending = migrate.migrate(cfg, check_only=a.check)
        if a.check:
            return 1 if pending else 0
    elif a.cmd == "import":
        from .importer import run
        result = run.run(cfg, a.snapshot, apply=a.apply, offline=a.offline, out_dir=a.out)
        for c in result.get("checks", []):
            print("{} {} ({})".format("ok  " if c["ok"] else "FAIL", c["name"], c["detail"]))
        print("report:", result["report_path"])
        if result.get("failed"):
            return 2
        print("committed" if result.get("committed") else "dry run: nothing written")
    elif a.cmd == "serve":
        from .web import server
        server.serve(cfg, host=a.host, port=a.port, **server.from_env(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
