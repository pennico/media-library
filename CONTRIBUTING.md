# Contributing

Thanks for your interest in media-library. Please read the
[Code of Conduct](CODE_OF_CONDUCT.md) first.

## Ground rules

- **The database enforces the rules.** A new rule is a constraint, domain or
  trigger in a new migration, with a test in `tests/test_schema.py` that
  shows the database refusing bad data. Never edit an applied migration.
- **Every write goes through `db.write_tx`** with a source and a client, so
  provenance, owner protection and the audit stay intact.
- **The import is lossless.** A new source field is either mapped or listed
  as dropped in `medialib/importer/fields.py`, with the reason.
- **Few dependencies, pinned with hashes** (`requirements.lock`). The page
  (`web/index.html`) is one file with no build step.
- **No site specifics or library data in the repository.** Hostnames,
  ports, paths and personal data belong in `.env` or in `private/` (ignored).
  CI runs `scripts/scan-private.sh`.

## Development

See the README. Tests need PostgreSQL 18: either the dev cluster
(`tools/devdb.py`) or any server you point `MEDIALIB_DB_HOST`,
`MEDIALIB_DB_PORT` and `MEDIALIB_SECRETS_DIR` at. The importer tests that need
a real Yamtrack/CWA snapshot in `data/` skip without it.

```bash
pytest -q tests/
scripts/scan-private.sh
```

## Pull requests

1. Fork, create a branch, and make your change with a minimal diff.
2. Add or update tests for the behaviour you changed.
3. Open a PR against `main` describing what changed and how you tested it.
   CI must be green.
