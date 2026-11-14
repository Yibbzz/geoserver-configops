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


@dataclass
class Database:
    host: str
    port: int
    database: str
    username: str
    password: str
    schema: str = DEFAULT_SCHEMA  # where a layer's `table: <name>` is

    def connect(self):
        return psycopg2.connect(host=self.host, port=self.port, dbname=self.database, user=self.username, password=self.password)


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
                    username=values["username"], password=values["password"], schema=schema_of(project))


def tables_exist(conn, schema: str, tables: list[str]) -> bool:
    """Whether each of these is a table, view, materialized view, foreign or partitioned table in `schema`.
    Read from pg_class: information_schema.tables has no materialized views, and only the tables this login has rights on."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = ANY(%s) AND c.relkind IN ('r', 'v', 'm', 'f', 'p')",
                    (schema, tables))
        return cur.fetchone()[0] == len(set(tables))
