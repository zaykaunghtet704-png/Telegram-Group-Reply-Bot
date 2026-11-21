import re
import time
from difflib import SequenceMatcher
from database import connect as _db

def init_learning_db():
    with _db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS learning_settings (
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            threshold INTEGER NOT NULL DEFAULT 72
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS learned_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            response TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS learn_pending (
            chat_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            created_at REAL NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_learned_chat ON learned_pairs(chat_id)")


def get_learning_settings(chat_id):
    with _db() as conn:
        conn.execute("INSERT OR IGNORE INTO learning_settings(chat_id) VALUES(?)", (chat_id,))
        row = conn.execute("SELECT * FROM learning_settings WHERE chat_id=?", (chat_id,)).fetchone()
        return dict(row)


def set_learning_setting(chat_id, key, value):
    if key not in ("enabled", "threshold"):
        raise ValueError("Invalid learning setting")
    with _db() as conn:
        conn.execute("INSERT OR IGNORE INTO learning_settings(chat_id) VALUES(?)", (chat_id,))
        conn.execute(f"UPDATE learning_settings SET {key}=? WHERE chat_id=?", (value, chat_id))


def normalize_text(value):
    return re.sub(r"[\s\W_]+", "", value.casefold().strip(), flags=re.UNICODE)


def similarity(left, right):
    a, b = normalize_text(left), normalize_text(right)
    if min(len(a), len(b)) < 4:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def record_conversation(chat_id, user_id, message_text, reply_to_user_id=None):
    """Learn only from an explicit Telegram reply to the previous user's message."""
    text = message_text.strip()
    if len(text) < 3 or len(text) > 1000:
        return
    now = time.time()
    with _db() as conn:
        previous = conn.execute(
            "SELECT user_id,message_text,created_at FROM learn_pending WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if (previous and reply_to_user_id is not None
                and previous["user_id"] == reply_to_user_id
                and previous["user_id"] != user_id
                and now - previous["created_at"] <= 300):
            prompt = previous["message_text"].strip()
            if len(prompt) >= 3 and normalize_text(prompt) != normalize_text(text):
                conn.execute(
                    "INSERT INTO learned_pairs(chat_id,prompt,response) VALUES(?,?,?)",
                    (chat_id, prompt[:1000], text[:1000]),
                )
        conn.execute(
            "INSERT INTO learn_pending(chat_id,user_id,message_text,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id,"
            "message_text=excluded.message_text,created_at=excluded.created_at",
            (chat_id, user_id, text[:1000], now),
        )


def find_learned_reply(chat_id, incoming, threshold):
    with _db() as conn:
        rows = conn.execute(
            "SELECT id,prompt,response,hits FROM learned_pairs "
            "WHERE chat_id=? ORDER BY hits DESC,id DESC LIMIT 3000",
            (chat_id,),
        ).fetchall()
    best = None
    best_score = 0.0
    for row in rows:
        score = similarity(incoming, row["prompt"])
        if score > best_score:
            best, best_score = row, score
    if best is not None and best_score >= threshold / 100.0:
        return dict(best)
    return None


def list_learned(chat_id, limit=30):
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) n FROM learned_pairs WHERE chat_id=?", (chat_id,)).fetchone()["n"]
        rows = conn.execute(
            "SELECT id,prompt,response,hits FROM learned_pairs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return total, [dict(row) for row in rows]


def delete_learned(chat_id, pair_id):
    with _db() as conn:
        cur = conn.execute("DELETE FROM learned_pairs WHERE id=? AND chat_id=?", (pair_id, chat_id))
        return cur.rowcount > 0


def clear_pending(chat_id):
    with _db() as conn:
        conn.execute("DELETE FROM learn_pending WHERE chat_id=?", (chat_id,))
