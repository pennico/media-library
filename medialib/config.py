"""Settings from the environment. Secrets are files (Docker secrets), never
environment values: MEDIALIB_SECRETS_DIR/<role>.pw."""
import dataclasses
import os

from psycopg.conninfo import make_conninfo


@dataclasses.dataclass(frozen=True)
class Config:
    db_host: str
    db_port: int
    db_name: str
    secrets_dir: str
    media_dir: str
    cwa_library: str
    owner_login: str
    pg_bin: str

    def password(self, role):
        path = os.path.join(self.secrets_dir, role + ".pw")
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()

    def dsn(self, role, dbname=None):
        return make_conninfo(host=self.db_host, port=self.db_port, user=role,
                             dbname=dbname or self.db_name, password=self.password(role),
                             application_name="medialib", connect_timeout=10)


def from_env(**overrides):
    env = os.environ
    values = dict(
        db_host=env.get("MEDIALIB_DB_HOST", "db"),
        db_port=int(env.get("MEDIALIB_DB_PORT", "5432")),
        db_name=env.get("MEDIALIB_DB_NAME", "medialib"),
        secrets_dir=env.get("MEDIALIB_SECRETS_DIR", "/run/secrets"),
        media_dir=env.get("MEDIALIB_MEDIA_DIR", "/media"),
        cwa_library=env.get("MEDIALIB_CWA_LIBRARY", ""),
        owner_login=env.get("OWNER_LOGIN", ""),
        pg_bin=env.get("MEDIALIB_PG_BIN", ""),
    )
    values.update(overrides)
    return Config(**values)
