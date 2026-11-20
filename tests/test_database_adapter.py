import unittest
from unittest.mock import patch

import database
import bot
import security
import moderation
import learning
from database import Connection


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.rowcount = len(self.rows)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def __iter__(self):
        return iter(self.rows)


class FakeRawConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "information_schema.columns" in sql:
            return FakeCursor([{
                "ordinal_position": 1,
                "column_name": "chat_id",
                "data_type": "bigint",
                "is_nullable": "NO",
                "column_default": None,
            }])
        if "RETURNING id" in sql:
            return FakeCursor([{"id": 42}])
        return FakeCursor()


class DatabaseAdapterTests(unittest.TestCase):
    def test_sqlite_schema_syntax_is_converted(self):
        raw = FakeRawConnection()
        db = Connection(raw)
        db.execute(
            "CREATE TABLE replies (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER)"
        )
        sql = raw.calls[-1][0]
        self.assertIn("BIGSERIAL PRIMARY KEY", sql)
        self.assertIn("chat_id BIGINT", sql)

    def test_text_timestamp_default_is_cast_for_postgres(self):
        raw = FakeRawConnection()
        db = Connection(raw)
        db.execute("CREATE TABLE replies (created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        sql = raw.calls[-1][0]
        self.assertIn("CURRENT_TIMESTAMP::text", sql)

    def test_insert_ignore_and_lastrowid(self):
        raw = FakeRawConnection()
        db = Connection(raw)
        cur = db.execute(
            "INSERT OR IGNORE INTO blocked_words(chat_id,word) VALUES(?,?)",
            (-100123, "spam"),
        )
        sql, params = raw.calls[-1]
        self.assertIn("ON CONFLICT DO NOTHING RETURNING id", sql)
        self.assertEqual(params, (-100123, "spam"))
        self.assertEqual(cur.lastrowid, 42)


    def test_all_module_schemas_use_postgres_compatible_sql(self):
        class CaptureRaw:
            statements = []

            def execute(self, sql, params=()):
                self.statements.append(sql)
                if "information_schema.columns" in sql:
                    return FakeCursor([])
                return FakeCursor()

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        raw_connections = []

        def make_raw(*args, **kwargs):
            raw = CaptureRaw()
            raw_connections.append(raw)
            return raw

        with patch.object(database, "DATABASE_URL", "postgresql://test"), \
             patch.object(database.psycopg, "connect", side_effect=make_raw):
            bot.init_db()
            learning.init_learning_db()
            security.init_security_db()
            moderation.init_moderation_db()

        statements = [
            sql
            for raw in raw_connections
            for sql in raw.statements
        ]
        self.assertTrue(statements)
        for sql in statements:
            self.assertNotIn("AUTOINCREMENT", sql.upper())
            self.assertNotRegex(
                sql,
                r"TEXT\s+NOT\s+NULL\s+DEFAULT\s+CURRENT_TIMESTAMP(?!::text)",
            )

    def test_legacy_pragma_returns_sqlite_compatible_rows(self):
        raw = FakeRawConnection()
        db = Connection(raw)
        rows = db.execute("PRAGMA table_info(settings)").fetchall()
        self.assertEqual(rows[0][1], "chat_id")
        self.assertEqual(rows[0][5], 0)


if __name__ == "__main__":
    unittest.main()
