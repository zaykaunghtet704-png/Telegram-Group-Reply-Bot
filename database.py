"""Shared SQLite/PostgreSQL compatibility layer and one-time SQLite importer."""
import os
import re
import sqlite3
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "replies.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

SERIAL_TABLES = {"replies", "blocked_words", "raid_actions", "owner_cases", "warnings", "learned_pairs"}
MIGRATION_TABLES = (
    "settings", "replies", "usage", "known_users", "known_groups",
    "security_settings", "blocked_words", "raid_actions", "owner_cases",
    "moderation_settings", "warnings", "learning_settings", "learned_pairs",
    "learn_pending",
)


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self.raw.rollback()
            else:
                self.raw.commit()
        finally:
            self.raw.close()
        return False

    def execute(self, sql, params=()):
        statement = sql.strip()
        pragma = re.fullmatch(r"PRAGMA[ ]+table_info[(]([A-Za-z0-9_]+)[)]", statement, re.I)
        if pragma:
            table = pragma.group(1)
            rows = self.raw.execute(
                "SELECT ordinal_position, column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name=%s ORDER BY ordinal_position", (table,)
            ).fetchall()
            # Match the SQLite table_info tuple positions used by the legacy schema check.
            return Cursor(_TupleRows([
                (i, row["column_name"], row["data_type"], int(row["is_nullable"] == "NO"),
                 row["column_default"], int(row["column_name"] == "id"))
                for i, row in enumerate(rows)
            ]))

        statement = re.sub(r"^INSERT[ ]+OR[ ]+IGNORE[ ]+INTO", "INSERT INTO", statement, flags=re.I)
        if sql.strip().upper().startswith("INSERT OR IGNORE"):
            if "ON CONFLICT" not in statement.upper():
                statement += " ON CONFLICT DO NOTHING"

        auto_id = False
        match = re.match(r"INSERT[ ]+INTO[ ]+([A-Za-z0-9_]+)", statement, re.I)
        if match and match.group(1).lower() in SERIAL_TABLES and "RETURNING" not in statement.upper():
            statement += " RETURNING id"
            auto_id = True

        statement = re.sub(r"INTEGER[ ]+PRIMARY[ ]+KEY[ ]+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", statement, flags=re.I)
        cur = self.raw.execute(statement, params or ())
        lastrowid = None
        if auto_id:
            result = cur.fetchone()
            if result:
                lastrowid = result["id"]
        return Cursor(cur, lastrowid)


class _TupleRows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is missing. Configure PostgreSQL on Render.")
    return Connection(psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15))


def migrate_sqlite_once():
    """Import legacy replies.db tables into PostgreSQL without deleting the source file."""
    if not DATABASE_URL or not DB_PATH.exists():
        return {"status": "skipped", "reason": "No DATABASE_URL or legacy SQLite file."}

    with connect() as pg:
        pg.execute("""CREATE TABLE IF NOT EXISTS app_migrations (
            migration_key TEXT PRIMARY KEY,
            completed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        done = pg.execute(
            "SELECT migration_key FROM app_migrations WHERE migration_key=?",
            ("sqlite_replies_db_v1",)
        ).fetchone()
        if done:
            return {"status": "already_done"}

        source = sqlite3.connect(str(DB_PATH))
        source.row_factory = sqlite3.Row
        imported = {}
        try:
            for table in MIGRATION_TABLES:
                exists = source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                source_columns = [r["name"] for r in source.execute(f"PRAGMA table_info({table})")]
                target_rows = pg.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name=? ORDER BY ordinal_position",
                    (table,)
                ).fetchall()
                target_columns = {r["column_name"] for r in target_rows}
                columns = [name for name in source_columns if name in target_columns]
                if not columns:
                    continue
                quoted = ", ".join('"' + name + '"' for name in columns)
                placeholders = ", ".join(["%s"] * len(columns))
                rows = source.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
                count = 0
                for row in rows:
                    values = tuple(row[name] for name in columns)
                    pg.execute(
                        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders}) ON CONFLICT DO NOTHING',
                        values,
                    )
                    count += 1
                imported[table] = count

            for table in SERIAL_TABLES:
                pg.execute(
                    "SELECT setval(pg_get_serial_sequence(?, 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM \"{table}\"), 1), "
                    f"EXISTS(SELECT 1 FROM \"{table}\") )",
                    (table,)
                )
            pg.execute(
                "INSERT INTO app_migrations(migration_key) VALUES(?)",
                ("sqlite_replies_db_v1",)
            )
        finally:
            source.close()

    return {"status": "imported", "tables": imported}
