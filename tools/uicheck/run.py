#!/usr/bin/env python3
"""Browser checks of the console, against a copy of the development database.

For each check script in checks/ (or the ones named): a fresh copy of the dev
database (``--template``, default ``medialib``) is made, migrated and given the
test items, a console is started on a free local port with a throw-away
password, the script drives it in headless Chromium (Playwright), and the copy
goes again. A script prints PASS/FAIL lines and ends with ALL PASS or N FAILED.

    .venv/bin/python tools/uicheck/run.py                 # every check
    .venv/bin/python tools/uicheck/run.py shelf keyboard  # some
    .venv/bin/python tools/uicheck/run.py --perf 10 4     # timings: 10x the library, 4x slower CPU

Needs the dev database (tools/devdb.py) with a library in it, the covers in
media/, Node and Playwright (README.md).
"""
import argparse
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LOGIN = "owner@uicheck.test"


def dev_env():
    out = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "devdb.py"), "env"], check=True,
                         capture_output=True, text=True).stdout
    env = {}
    for line in out.splitlines():
        if line.startswith("export ") and "=" in line:
            k, _, v = line[7:].partition("=")
            env[k] = v
    return env


def psql(env, sql):
    secret = open(os.path.join(env["MEDIALIB_SECRETS_DIR"], "postgres.pw")).read().strip()
    subprocess.run([os.path.join(env["MEDIALIB_PG_BIN"], "psql"), "-h", env["MEDIALIB_DB_HOST"], "-p", env["MEDIALIB_DB_PORT"],
                    "-U", "postgres", "-d", "postgres", "-qc", sql], check=True, env=dict(os.environ, **env, PGPASSWORD=secret))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def busy_template(env, template):
    """Sessions on the template database (a copy needs it alone): a console or psql left on it."""
    secret = open(os.path.join(env["MEDIALIB_SECRETS_DIR"], "postgres.pw")).read().strip()
    r = subprocess.run([os.path.join(env["MEDIALIB_PG_BIN"], "psql"), "-h", env["MEDIALIB_DB_HOST"], "-p", env["MEDIALIB_DB_PORT"],
                        "-U", "postgres", "-d", "postgres", "-qAtc", "SELECT count(*) FROM pg_stat_activity WHERE datname = '{}'".format(template)],
                       env=dict(os.environ, **env, PGPASSWORD=secret), capture_output=True, text=True)
    return int(r.stdout.strip() or 0)


def run_one(name, args, base_env, template):
    db = "uicheck_" + secrets.token_hex(4)
    pw = secrets.token_urlsafe(12)
    port = free_port()
    state = tempfile.mkdtemp(prefix="uicheck-")           # the throw-away password's hash and the server's log: removed after
    env = dict(os.environ, **base_env, MEDIALIB_DB_NAME=db)
    server = None
    try:
        psql(base_env, 'CREATE DATABASE "{}" TEMPLATE "{}" OWNER ml_owner'.format(db, template))
        for cmd in (["migrate"], ["test-data"]):
            r = subprocess.run([sys.executable, "-m", "medialib"] + cmd, cwd=ROOT, env=env, capture_output=True, text=True)
            if r.returncode:
                return False, ["setup: medialib {} failed:".format(cmd[0])] + (r.stderr or r.stdout).strip().splitlines()[-8:]
        sys.path.insert(0, ROOT)
        from medialib.web import auth
        with open(os.path.join(state, "pw"), "w") as fh:
            fh.write(auth.hash_password(pw) + "\n")
        env.update(MEDIALIB_MEDIA_DIR=os.path.join(ROOT, "media"), OWNER_LOGIN=LOGIN, MEDIALIB_COOKIE_SECURE="0",
                   MEDIALIB_ORIGIN="http://127.0.0.1:{}".format(port), MEDIALIB_PASSWORD_FILE=os.path.join(state, "pw"),
                   MEDIALIB_PASSKEYS_FILE=os.path.join(state, "passkeys.json"), MEDIALIB_STATE_DIR=state)
        log = open(os.path.join(state, "server.log"), "w")
        server = subprocess.Popen([sys.executable, "-m", "medialib", "serve", "--host", "127.0.0.1", "--port", str(port)],
                                  cwd=ROOT, env=env, stdout=log, stderr=log)
        base = "http://127.0.0.1:{}/".format(port)
        up = False
        for _ in range(100):
            if server.poll() is not None:
                break
            try:
                up = urllib.request.urlopen(base + "healthz", timeout=2).status == 200
                break
            except OSError:
                time.sleep(0.2)
        if not up:
            log.flush()
            return False, ["setup: the console did not start:"] + open(os.path.join(state, "server.log")).read().strip().splitlines()[-8:]
        node_env = dict(os.environ, ML_BASE=base, ML_PW=pw, ML_LOGIN=LOGIN)
        r = subprocess.run(["node", os.path.join("checks", name + ".js")] + args, cwd=HERE, env=node_env,
                           capture_output=True, text=True, timeout=600)
        lines = [ln for ln in (r.stdout + r.stderr).splitlines()
                 if ln.startswith(("PASS", "FAIL", "ALL PASS", "items", "PAGEERR")) or " FAILED" in ln or "wall" in ln or "Error" in ln or "waiting for" in ln or ".js:" in ln]
        return r.returncode == 0 and any(ln.startswith("ALL PASS") for ln in lines) or (name == "perf" and r.returncode == 0), lines
    finally:
        if server:
            server.terminate()
            server.wait(10)
        psql(base_env, 'DROP DATABASE IF EXISTS "{}" WITH (FORCE)'.format(db))
        shutil.rmtree(state, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("checks", nargs="*", help="names in checks/ (default: all but perf)")
    ap.add_argument("--template", default="medialib", help="the dev database to copy (default medialib)")
    ap.add_argument("--perf", nargs="*", metavar="N THROTTLE", help="run the timings instead: clone factor, CPU slowdown")
    a = ap.parse_args()
    env = dev_env()
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "devdb.py"), "start"], check=False, capture_output=True)
    n = busy_template(env, a.template)
    if n:
        print("{} session(s) on the database {}: a copy needs it alone. Stop the dev console or psql on it, "
              "or copy it once to another name and pass --template.".format(n, a.template))
        return 2
    if a.perf is not None:
        todo = [("perf", a.perf or ["10", "4"])]
    else:
        names = a.checks or sorted(f[:-3] for f in os.listdir(os.path.join(HERE, "checks")) if f.endswith(".js") and f != "perf.js")
        todo = [(n, []) for n in names]
    failed = 0
    for name, args in todo:
        ok, lines = run_one(name, args, env, a.template)
        print("== {} {}".format(name, "ok" if ok else "FAILED"))
        for ln in lines:
            if not ln.startswith("PASS") or not ok:
                print("   " + ln)
        failed += not ok
    print("{} of {} checks failed".format(failed, len(todo)) if failed else "all {} checks pass".format(len(todo)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
