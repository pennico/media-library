"""medialib command line.

    python -m medialib bootstrap                 # superuser, once per cluster
    python -m medialib migrate [--check]
    python -m medialib import SNAPSHOT_DIR [--apply] [--offline] [--out DIR]
    python -m medialib serve [--host 0.0.0.0] [--port 8099]
    python -m medialib joplin (--dir DIR | --tar FILE|-) [--apply] [--offline]
    python -m medialib adopt-files SNAPSHOT_DIR [--apply]
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
    j = sub.add_parser("joplin", help="Joplin review notes -> the library (dry run unless --apply)")
    src = j.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", help="the Joplin sync folder (or a copy)")
    src.add_argument("--tar", help="a tar stream of the sync folder ('-' = stdin)")
    j.add_argument("--notebook", default="Reviews")
    j.add_argument("--apply", action="store_true", help="write (default: show what it would do)")
    j.add_argument("--offline", action="store_true", help="no Apple Books / Open Library search")
    f = sub.add_parser("adopt-files", help="copy the imported CWA ebook files into the store (dry run unless --apply)")
    f.add_argument("snapshot")
    f.add_argument("--apply", action="store_true")
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
    elif a.cmd == "joplin":
        from .feeds import joplin
        if a.tar == "-":
            return joplin.main(cfg, tar=sys.stdin.buffer, apply=a.apply, offline=a.offline, notebook=a.notebook)
        if a.tar:
            with open(a.tar, "rb") as fh:
                return joplin.main(cfg, tar=fh, apply=a.apply, offline=a.offline, notebook=a.notebook)
        return joplin.main(cfg, source_dir=a.dir, apply=a.apply, offline=a.offline, notebook=a.notebook)
    elif a.cmd == "adopt-files":
        from . import adopt
        return adopt.main(cfg, a.snapshot, apply=a.apply)
    elif a.cmd == "serve":
        from .web import server
        server.serve(cfg, host=a.host, port=a.port, **server.from_env(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
