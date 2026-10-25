# Install

media-library runs as one Docker Compose project: `medialib-db` (PostgreSQL
18.6, no host port) and `medialib-app` (the console on `127.0.0.1:8099`),
plus a one-off `tools` service for bootstrap, migrations and imports. Tailscale
Serve publishes the console on your tailnet only.

Needs: Linux with Docker Compose v2, Tailscale, `git`, `python3` (for the
password helper), about 800 MB of RAM to spare.

## 1. Folder, code and secrets

```bash
BASE=/srv/media-library            # any folder; the examples use this one
sudo install -d -o "$USER" -g "$USER" -m 750 "$BASE"
git clone https://github.com/pennico/media-library.git "$BASE/app"
cd "$BASE"
install -d -m 700 secrets state state/no-passkeys
install -d -m 750 media out
# written by the postgres user (uid 999) inside medialib-db; your group may read
sudo install -d -o 999 -g 999 -m 700 wal-archive
sudo install -d -o 999 -g "$(id -g)" -m 2770 backups backups/dumps backups/base

for f in postgres ml_migrator ml_app ml_readonly; do
  ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/$f.pw )
done
python3 app/deploy/make-password.py secrets/console-password   # the unlock password
( umask 077; python3 -c 'import secrets; a = "abcdefghjkmnpqrstuvwxyz23456789"; print("-".join("".join(secrets.choice(a) for _ in range(4)) for _ in range(5)))' > secrets/reader.pw )   # reading apps
cp app/deploy/env.example .env && chmod 600 .env && "${EDITOR:-nano}" .env
cp app/deploy/docker-compose.yml docker-compose.yml
```

In `.env`, set `OWNER_LOGIN` to your Tailscale login (`tailscale status
--json` shows it under `User`) and `MEDIALIB_ORIGIN` to the address you will
publish in step 4. If you use
[agent-console](https://github.com/pennico/agent-console), you can point
`CONSOLE_PASSWORD_FILE` at its password file and `AGENT_CONSOLE_STATE` at its
state folder: the same password and passkeys then unlock both.

## 2. Database

```bash
docker compose build
docker compose up -d db
docker exec -u postgres medialib-db psql -Atc "SHOW data_checksums"   # on
docker compose run --rm tools bootstrap      # roles + database (idempotent)
docker compose run --rm tools migrate
```

## 3. Import (optional)

Skip this to start with an empty library. To import, put read-only copies
in the folder that `SNAPSHOT_DIR` names:

| Path in the snapshot | What | Needed |
|---|---|---|
| `yamtrack.sqlite3` | Yamtrack's database (SQLite) | yes |
| `calibre-library/metadata.db` (+ the book folders) | Calibre library used by Calibre-Web Automated | for CWA data |
| `cwa-app.db` | Calibre-Web's app database (counts only) | optional |
| `library-sync-state/cwa-links.json` | which Calibre book is which Yamtrack item: `{"books": {"<calibre id>": {"media": {"source": "openlibrary", "media_id": "…"}}}}`, or `{"ignore": true}` for box sets and test files | for CWA data: CWA records without a link are not imported |
| `library-sync-state/*.json` | cover and Open Library caches from an earlier pipeline | optional |
| `owner-covers/` | cover files you chose; they win over every other cover | optional |

Take SQLite copies with `sqlite3 <db> ".backup <copy>"`, not `cp`, while the
apps are running.

```bash
docker compose run --rm tools import /snapshot --out /out           # dry run: report in out/, nothing written
docker compose run --rm tools import /snapshot --apply --out /out   # commit; again = 0 changes
```

The report (`out/import-*.md`) lists every check, every field that was
dropped and why, and anything that couldn't be matched. Running the import
again later keeps the values you changed in the console.

## 4. Console

```bash
docker compose up -d app
curl -s http://127.0.0.1:8099/healthz; echo
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8099   # pick a free port; Funnel stays off
```

Open `https://<server>.<tailnet>.ts.net:8443/`. Everyone on the tailnet can
read; only `OWNER_LOGIN` can write, after unlocking with the password.

## 5. Readest (optional)

The console's **Sync** page shows the addresses and, once unlocked, the
reading-app password (`secrets/reader.pw`; user `READER_USER` from `.env`).
Requests must also come from `OWNER_LOGIN` through Tailscale Serve.

- **Catalog**: Readest library → **+** → **OPDS Catalogs** (or **Online
  Library**) → **Add Catalog**: OPDS URL `https://<server>…:8443/opds`, the
  username and password.
- **Positions**: Readest → **Settings** → **Integrations** → **KOReader Sync**:
  Server URL `https://<server>…:8443/kosync`, the same login → **Connect**;
  then turn on **Send Document Metadata**.

The catalog lists books with an ebook file in the store: uploads, and the
imported Calibre-Web files after

```bash
docker compose run --rm tools adopt-files /snapshot --apply
```

## 6. Joplin review notes (optional)

Write one note per book or film in a **Reviews** notebook (sub-notebooks
Books, Audiobooks, Movies, TV-Series; DESIGN.md §13.2 has the format). Tell
`.env` where Joplin syncs to: `JOPLIN_SOURCE_CONTAINER` + `JOPLIN_SOURCE_PATH`
for a folder inside a container (e.g. Nextcloud's data folder), or
`JOPLIN_SOURCE_DIR` for a folder on this machine. End-to-end encrypted sync
targets can't be read. Then:

```bash
app/ops/joplin-sync.sh --dry-run        # what it would do
( crontab -l 2>/dev/null
  echo "12 * * * * flock -n /tmp/medialib-joplin.lock $BASE/app/ops/joplin-sync.sh > $BASE/out/joplin.log 2>&1"
) | crontab -
```

Notes it can't match show on the console's Sync page, with the closest
candidates; add an `isbn:` line or add the book in the console.

## 7. Backups

```bash
OPS=$BASE/app/ops
( crontab -l 2>/dev/null
  echo "5 3,9,15,21 * * * flock -n /tmp/medialib-dump.lock $OPS/backup-dump.sh > $BASE/backups/dump.log 2>&1"
  echo "50 2 * * * flock -n /tmp/medialib-base.lock $OPS/backup-base.sh > $BASE/backups/base.log 2>&1"
  echo "0 4 * * 0 flock -n /tmp/medialib-restore.lock $OPS/restore-test.sh > $BASE/backups/restore-test.log 2>&1"
) | crontab -
$OPS/backup-dump.sh && $OPS/backup-base.sh && $OPS/restore-test.sh
curl -s http://127.0.0.1:8099/api/health
```

Set the dump times a few minutes before your file backup (restic, Backrest,
…) runs, and include the whole install folder in it. The layers:

| Layer | What | Restores |
|---|---|---|
| History | every change in `audit.change` (hash-chained) | one field or one book, without backups |
| Dump | `backups/dumps/medialib-*.dump`, 14 days | any dump, portable across PostgreSQL versions |
| PITR | nightly base backup + WAL archive, 7 days | any second in that window |
| Your file backup | all of the above | its own retention |

`/api/health` returns 503 when a check fails (schema version, page checksums,
WAL archiving, ICU collation drift, audit chain, dump < 7 h, base backup
< 26 h, restore test < 8 days, and the Joplin feed's last run once it is
set up), so an uptime monitor can watch it.

Restore a dump into a scratch database to look at it:

```bash
docker exec -u postgres medialib-db createdb scratch
docker exec -u postgres medialib-db pg_restore -d scratch /backups/dumps/medialib-<time>.dump
```

For point in time, `ops/restore-test.sh` shows every step; add
`recovery_target_time = '<time>'` to stop at a chosen second.

## Update

```bash
cd "$BASE" && git -C app pull --ff-only && cp app/deploy/docker-compose.yml .
docker compose build && docker compose run --rm tools migrate && docker compose up -d app
```

Migrations are forward-only and each runs in one transaction; a changed
migration file stops the run. From 0.1.0 to 0.2.0: create `secrets/reader.pw`
first (step 1); the compose file needs it.

## Upgrades

- **Minor PostgreSQL** (18.6 → 18.7): change the tag in the compose file and
  `docker compose up -d db`. Same data format.
- **Major** (18 → 19): dump, new data folder on the new image, restore,
  verify. It's the weekly restore test with another image, so it is rehearsed.
- **OS / ICU change**: the health check fails "sort collations". Reindex the
  sort indexes, then `ALTER COLLATION … REFRESH VERSION`.

## Remove

```bash
cd "$BASE" && docker compose down
sudo tailscale serve --https=8443 off
crontab -l | grep -v medialib | crontab -
docker image rm media-library:local
sudo rm -rf "$BASE"        # deletes the data and the backups in it
```
