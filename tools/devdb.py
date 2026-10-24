#!/usr/bin/env python3
"""Development PostgreSQL for media-library, without root or Docker.

Uses the official PGDG Debian packages of the production version (18.6),
unpacked into ``.devdb/root`` (plus the few libraries the container lacks),
and runs a cluster on a unix socket only. Same engine, version, ICU and
initdb options as production (DESIGN.md §5.3), so tests exercise the real
thing.

    python3 tools/devdb.py fetch     # download + verify + unpack the packages
    python3 tools/devdb.py init      # new cluster in .devdb/data (refuses if one exists)
    python3 tools/devdb.py start | stop | status
    python3 tools/devdb.py env       # shell exports for the medialib CLI
    python3 tools/devdb.py psql [ARGS...]

Secrets (superuser and role passwords) are generated into
``.devdb/secrets/`` (0600, gitignored); nothing is printed.
"""
import gzip
import hashlib
import lzma
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV = os.path.join(ROOT, ".devdb")
DL = os.path.join(DEV, "dl")
PREFIX = os.path.join(DEV, "root")
DATA = os.path.join(DEV, "data")
SOCK = os.path.join(DEV, "sock")
ARCHIVE = os.path.join(DEV, "wal-archive")
SECRETS = os.path.join(DEV, "secrets")
LOG = os.path.join(DEV, "postgres.log")
PG_VERSION = "18.6"
BIN = os.path.join(PREFIX, "usr", "lib", "postgresql", "18", "bin")
LIB = os.path.join(PREFIX, "usr", "lib", "x86_64-linux-gnu")

PGDG = "https://apt.postgresql.org/pub/repos/apt/"
DEBIAN = "https://deb.debian.org/debian/"
PACKAGES = [  # (repo, package, version prefix)
    ("pgdg", "postgresql-18", PG_VERSION),
    ("pgdg", "postgresql-client-18", PG_VERSION),
    ("pgdg", "libpq5", PG_VERSION),
    ("debian", "libicu72", ""),
    ("debian", "libnuma1", ""),
    ("debian", "libxml2", ""),
    ("debian", "libxslt1.1", ""),
    ("debian", "liburing2", ""),
]
# Same initdb options as production (DESIGN.md §5.3).
INITDB_ARGS = ["--locale-provider=builtin", "--builtin-locale=C.UTF-8",
               "--encoding=UTF8", "--data-checksums", "--wal-segsize=1"]


def _index(url, opener):
    with urllib.request.urlopen(url, timeout=60) as r:
        text = opener(r.read()).decode("utf-8")
    out = {}
    for block in text.split("\n\n"):
        f = dict(re.findall(r"^([A-Za-z0-9-]+): (.*)$", block, re.M))
        if "Package" in f:
            out.setdefault(f["Package"], []).append(f)
    return out


def fetch():
    os.makedirs(DL, exist_ok=True)
    idx = {"pgdg": _index(PGDG + "dists/bookworm-pgdg/main/binary-amd64/Packages.gz",
                          gzip.decompress),
           "debian": _index(DEBIAN + "dists/bookworm/main/binary-amd64/Packages.xz",
                            lzma.decompress)}
    base = {"pgdg": PGDG, "debian": DEBIAN}
    debs = []
    for repo, name, ver in PACKAGES:
        cands = [p for p in idx[repo].get(name, []) if p["Version"].startswith(ver)]
        if not cands:
            sys.exit("package {} {} not found in {}".format(name, ver, repo))
        pkg = cands[0]
        path = os.path.join(DL, os.path.basename(pkg["Filename"]))
        if not os.path.exists(path):
            urllib.request.urlretrieve(base[repo] + pkg["Filename"], path)
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if digest != pkg["SHA256"]:
            os.remove(path)
            sys.exit("sha256 mismatch for {}".format(name))
        print("ok {} {}".format(name, pkg["Version"]))
        debs.append(path)
    shutil.rmtree(PREFIX, ignore_errors=True)
    os.makedirs(PREFIX)
    for path in debs:
        subprocess.run(["dpkg-deb", "-x", path, PREFIX], check=True)
    out = run("postgres", "--version", capture=True)
    print(out.strip())


def environ():
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = LIB + (":" + env["LD_LIBRARY_PATH"]
                                    if env.get("LD_LIBRARY_PATH") else "")
    env["PATH"] = BIN + ":" + env.get("PATH", "")
    return env


def run(prog, *args, capture=False, check=True, env=None):
    cmd = [os.path.join(BIN, prog), *args]
    r = subprocess.run(cmd, env=env or environ(), check=check, text=True,
                       capture_output=capture)
    return r.stdout if capture else r.returncode


def _secret(name):
    os.makedirs(SECRETS, mode=0o700, exist_ok=True)
    path = os.path.join(SECRETS, name + ".pw")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(32))
    return path


def init():
    if os.path.exists(os.path.join(DATA, "PG_VERSION")):
        sys.exit("cluster exists in {} (stop it and remove the directory first)".format(DATA))
    os.makedirs(SOCK, exist_ok=True)
    os.makedirs(ARCHIVE, exist_ok=True)
    for role in ("postgres", "ml_migrator", "ml_app", "ml_readonly"):
        _secret(role)
    run("initdb", "-D", DATA, "-U", "postgres", "--pwfile", _secret("postgres"),
        "--auth-local=scram-sha-256", "--auth-host=reject", *INITDB_ARGS)
    archive_cmd = "{} %p %f {}".format(os.path.join(ROOT, "ops", "archive-wal.sh"), ARCHIVE)
    with open(os.path.join(DATA, "postgresql.conf"), "a") as fh:
        fh.write("\n# media-library dev settings (tools/devdb.py)\n")
        for k, v in {
            "listen_addresses": "''",
            "unix_socket_directories": "'{}'".format(SOCK),
            "timezone": "'UTC'",
            "shared_buffers": "'128MB'",
            "wal_level": "replica",
            "archive_mode": "on",
            "archive_command": "'{}'".format(archive_cmd),
            "archive_timeout": "3600",
            "summarize_wal": "on",
            "wal_compression": "zstd",
            "password_encryption": "'scram-sha-256'",
            "log_min_duration_statement": "500",
            "log_lock_waits": "on",
        }.items():
            fh.write("{} = {}\n".format(k, v))
    print("initialised", DATA)


def start():
    run("pg_ctl", "-D", DATA, "-l", LOG, "-w", "start")


def stop():
    run("pg_ctl", "-D", DATA, "-m", "fast", "-w", "stop", check=False)


def status():
    return run("pg_ctl", "-D", DATA, "status", check=False)


def env_lines():
    return "\n".join([
        "export MEDIALIB_DB_HOST={}".format(SOCK),
        "export MEDIALIB_DB_PORT=5432",
        "export MEDIALIB_SECRETS_DIR={}".format(SECRETS),
        "export MEDIALIB_PG_BIN={}".format(BIN),
        "export LD_LIBRARY_PATH={}".format(LIB),
    ])


def psql(args):
    env = environ()
    with open(_secret("postgres")) as fh:
        env["PGPASSWORD"] = fh.read().strip()
    os.execve(os.path.join(BIN, "psql"),
              ["psql", "-h", SOCK, "-U", "postgres", *args], env)


def main(argv):
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    if cmd == "fetch":
        fetch()
    elif cmd == "init":
        init()
    elif cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        return status()
    elif cmd == "env":
        print(env_lines())
    elif cmd == "psql":
        psql(rest)
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
