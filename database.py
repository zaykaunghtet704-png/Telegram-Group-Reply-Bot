"""PostgreSQL adapter and safe one-time importer for the legacy SQLite database."""
import json
import os
import re
import sqlite3
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "replies.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

SERIAL_TABLES = {
    "replies", "blocked_words", "raid_actions",
    "owner_cases", "warnings", "learned_pairs", "member_notes", "member_reports", "owner_action_logs", "love_reports", "love_deliveries",
}
MIGRATION_TABLES = (
    "settings", "replies", "usage", "known_users", "known_groups",
    "security_settings", "blocked_words", "raid_actions", "owner_cases",
    "moderation_settings", "warnings", "learning_settings", "learned_pairs",
    "learn_pending", "member_profiles", "member_notes", "member_reports", "group_rules",
    "owner_settings", "owner_action_logs", "owner_bans", "love_profiles", "love_channels", "love_group_settings", "love_dm_subscribers", "love_likes", "love_blocks", "love_reports", "love_deliveries",
)


class _TupleRows:
    """Small adapter for the few legacy PRAGMA table_info callers."""
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def __iter__(self):
        return iter(self.rows)


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
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()
        return False

    def execute(self, sql, params=()):
        statement = sql.strip()

        # Compatibility for the old SQLite schema migration in bot.py.
        pragma = re.fullmatch(
            r"PRAGMA[ ]+table_info[(]([A-Za-z0-9_]+)[)]", statement, re.I
        )
        if pragma:
            table = pragma.group(1)
            rows = self.raw.execute(
                "SELECT ordinal_position, column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name=%s ORDER BY ordinal_position",
                (table,),
            ).fetchall()
            return Cursor(_TupleRows([
                (
                    row["ordinal_position"] - 1,
                    row["column_name"],
                    row["data_type"],
                    int(row["is_nullable"] == "NO"),
                    row["column_default"],
                    int(row["column_name"] == "id"),
                )
                for row in rows
            ]))

        ignore_insert = bool(re.match(r"INSERT[ ]+OR[ ]+IGNORE[ ]+INTO", statement, re.I))
        if ignore_insert:
            statement = re.sub(
                r"^INSERT[ ]+OR[ ]+IGNORE[ ]+INTO",
                "INSERT INTO",
                statement,
                flags=re.I,
            )
            if "ON CONFLICT" not in statement.upper():
                statement += " ON CONFLICT DO NOTHING"

        # SQLite INTEGER IDs may exceed PostgreSQL's 32-bit INTEGER range.
        # Convert SQLite's AUTOINCREMENT primary key syntax to BIGSERIAL.
        statement = re.sub(
            r"\bINTEGER[ ]+PRIMARY[ ]+KEY[ ]+AUTOINCREMENT\b",
            "BIGSERIAL PRIMARY KEY",
            statement,
            flags=re.I,
        )
        statement = re.sub(r"\bINTEGER\b", "BIGINT", statement, flags=re.I)
        statement = re.sub(
            r"(TEXT[ ]+NOT[ ]+NULL[ ]+DEFAULT)[ ]+CURRENT_TIMESTAMP\b",
            r"\1 CURRENT_TIMESTAMP::text",
            statement,
            flags=re.I,
        )

        auto_id = False
        match = re.match(r"INSERT[ ]+INTO[ ]+([A-Za-z0-9_]+)", statement, re.I)
        if (
            match
            and match.group(1).lower() in SERIAL_TABLES
            and "RETURNING" not in statement.upper()
        ):
            statement += " RETURNING id"
            auto_id = True

        # The application uses SQLite-style ? placeholders; psycopg uses %s.
        statement = statement.replace("?", "%s")
        cursor = self.raw.execute(statement, params or ())

        lastrowid = None
        if auto_id:
            inserted = cursor.fetchone()
            if inserted is not None:
                lastrowid = inserted["id"]
        return Cursor(cursor, lastrowid)


def connect():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is missing. Configure PostgreSQL on Render."
        )
    raw = psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=15,
    )
    return Connection(raw)


def _sqlite_tables(source):
    return {
        row["name"]
        for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def migrate_sqlite_once():
    """Import recognized legacy SQLite tables atomically; keep the source file."""
    if not DATABASE_URL:
        return {"status": "skipped", "reason": "DATABASE_URL is not configured."}
    if not DB_PATH.exists():
        return {"status": "skipped", "reason": "Legacy replies.db was not found."}

    source = sqlite3.connect(str(DB_PATH))
    source.row_factory = sqlite3.Row
    try:
        integrity = source.execute("PRAGMA quick_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("The uploaded SQLite backup failed its integrity check.")

        available_tables = _sqlite_tables(source)
        recognized = [name for name in MIGRATION_TABLES if name in available_tables]
        if not recognized:
            raise RuntimeError("No supported bot tables were found in the SQLite backup.")

        with connect() as pg:
            pg.execute(
                """CREATE TABLE IF NOT EXISTS app_migrations (
                    migration_key TEXT PRIMARY KEY,
                    completed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            done = pg.execute(
                "SELECT migration_key FROM app_migrations WHERE migration_key=?",
                ("sqlite_replies_db_v1",),
            ).fetchone()
            if done:
                return {"status": "already_done"}

            imported = {}
            for table in recognized:
                source_columns = [
                    row["name"]
                    for row in source.execute(f'PRAGMA table_info("{table}")')
                ]
                target_rows = pg.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name=? "
                    "ORDER BY ordinal_position",
                    (table,),
                ).fetchall()
                target_columns = {row["column_name"] for row in target_rows}
                columns = [name for name in source_columns if name in target_columns]
                if not columns:
                    raise RuntimeError(
                        f"No matching columns found while importing table: {table}"
                    )

                quoted = ", ".join('"' + name + '"' for name in columns)
                placeholders = ", ".join(["%s"] * len(columns))
                rows = source.execute(
                    f'SELECT {quoted} FROM "{table}"'
                ).fetchall()

                inserted_count = 0
                for row in rows:
                    values = tuple(row[name] for name in columns)
                    cursor = pg.execute(
                        f'INSERT INTO "{table}" ({quoted}) '
                        f'VALUES ({placeholders}) ON CONFLICT DO NOTHING',
                        values,
                    )
                    if cursor.rowcount > 0:
                        inserted_count += 1
                imported[table] = inserted_count

            # Reset serial sequences after preserving the original SQLite IDs.
            for table in SERIAL_TABLES:
                if table not in recognized:
                    continue
                pg.execute(
                    "SELECT setval(pg_get_serial_sequence(?, 'id'), "
                    f'COALESCE((SELECT MAX(id) FROM "{table}"), 1), '
                    f'EXISTS(SELECT 1 FROM "{table}"))',
                    (table,),
                )

            pg.execute(
                "INSERT INTO app_migrations(migration_key) VALUES(?)",
                ("sqlite_replies_db_v1",),
            )

        return {"status": "imported", "tables": imported}
    finally:
        source.close()


def export_database_json():
    """Return a portable JSON backup of the PostgreSQL application tables."""
    payload = {"format": "pro-reply-postgresql-backup-v1", "tables": {}}
    with connect() as pg:
        for table in MIGRATION_TABLES:
            exists = pg.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            payload["tables"][table] = pg.execute(
                f'SELECT * FROM "{table}"'
            ).fetchall()
    return json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
        indent=2,
    ).encode("utf-8")
