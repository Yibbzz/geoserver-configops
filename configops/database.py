"""The Project's PostGIS database: where layers' tables are. configops only reads it, to know
whether a layer's table exists yet and which columns it has; loading data is another tool's
job (geodata-sync, or your own)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg2

from . import secrets
from .loader import Resource

KEYS = ("host", "port", "database", "username", "password")
DEFAULT_SCHEMA = "public"


class DatabaseError(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


CONNECT_TIMEOUT = 15  # seconds


@dataclass
class Database:
    host: str
    port: int
    database: str
    username: str
    password: str
    schema: str = DEFAULT_SCHEMA  # where a layer's `table: <name>` is

    sslmode: str | None = None  # libpq's sslmode (`verify-full` checks the server's certificate); unset: $PGSSLMODE, else libpq's own

    def connect(self):
        """A connection that only reads, one statement at a time: nothing stays open in a transaction for the
        length of an apply (which a server's idle-in-transaction timeout would cut off), and a database that
        doesn't answer gives up after CONNECT_TIMEOUT seconds."""
        extra = {"sslmode": self.sslmode} if self.sslmode else {}
        conn = psycopg2.connect(host=self.host, port=self.port, dbname=self.database, user=self.username, password=self.password,
                                connect_timeout=CONNECT_TIMEOUT, application_name="configops", **extra)
        conn.set_session(readonly=True, autocommit=True)
        return conn


def schema_of(project: Resource) -> str:
    return (project.spec.get("database") or {}).get("schema") or DEFAULT_SCHEMA


def database(project: Resource, root: Path) -> Database | None:
    """The Project's database, or None when it names none (a project with no layers on tables)."""
    spec = project.spec.get("database")
    if not spec:
        return None
    secret = spec["connection"]["secret"]
    values = {key: secrets.lookup(secret, key, root) for key in KEYS}
    missing = [key for key, value in values.items() if value is None and key != "port"]
    if missing:
        names = ", ".join(f"${secrets.env_name(secret, key)}" for key in missing)
        raise DatabaseError(f"the database secret {secret} is missing {', '.join(missing)}: set {names}, or add them to secrets.yaml")
    return Database(host=values["host"], port=int(values["port"] or 5432), database=values["database"],
                    username=values["username"], password=values["password"], schema=schema_of(project),
                    sslmode=secrets.lookup(secret, "sslmode", root))


def tables_exist(conn, schema: str, tables: list[str]) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = ANY(%s)",
                    (schema, tables))
        return cur.fetchone()[0] == len(set(tables))
