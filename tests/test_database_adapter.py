import unittest

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

    def test_legacy_pragma_returns_sqlite_compatible_rows(self):
        raw = FakeRawConnection()
        db = Connection(raw)
        rows = db.execute("PRAGMA table_info(settings)").fetchall()
        self.assertEqual(rows[0][1], "chat_id")
        self.assertEqual(rows[0][5], 0)


if __name__ == "__main__":
    unittest.main()
