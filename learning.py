import re
import time
from difflib import SequenceMatcher
from database import connect as _db

MAX_PAIRS_PER_GROUP = 5000
MAX_PAIRS_GLOBAL = 500000
BEST_REPLIES_PER_GROUP = 2000
MAX_MESSAGE_LENGTH = 1000


def init_learning_db():
    with _db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS learning_settings (
            chat_id BIGINT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
            threshold INTEGER NOT NULL DEFAULT 72
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS learned_pairs (
            id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL,
            prompt TEXT NOT NULL, response TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS learn_pending (
            chat_id BIGINT PRIMARY KEY, user_id BIGINT NOT NULL,
            message_text TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL
        )""")
        conn.execute("ALTER TABLE learning_settings ALTER COLUMN enabled SET DEFAULT 1")
        conn.execute("UPDATE learning_settings SET enabled=1 WHERE enabled<>1")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_learned_chat ON learned_pairs(chat_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_learned_quality ON learned_pairs(chat_id,hits DESC,id DESC)")


def get_learning_settings(chat_id):
    with _db() as conn:
        conn.execute("INSERT INTO learning_settings(chat_id,enabled) VALUES(?,1) ON CONFLICT(chat_id) DO NOTHING", (chat_id,))
        return dict(conn.execute("SELECT * FROM learning_settings WHERE chat_id=?", (chat_id,)).fetchone())


def set_learning_setting(chat_id, key, value):
    if key != "threshold":
        raise ValueError("Learning is always enabled; only threshold can be changed.")
    with _db() as conn:
        conn.execute("INSERT INTO learning_settings(chat_id,enabled,threshold) VALUES(?,1,?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET threshold=excluded.threshold", (chat_id, value))


def normalize_text(value):
    return re.sub(r"[\s\W_]+", "", (value or "").casefold().strip(), flags=re.UNICODE)


def similarity(left, right):
    a, b = normalize_text(left), normalize_text(right)
    if min(len(a), len(b)) < 4:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _enforce_limits(conn, chat_id):
    count = conn.execute("SELECT COUNT(*) n FROM learned_pairs WHERE chat_id=?", (chat_id,)).fetchone()["n"]
    excess = max(0, int(count) - MAX_PAIRS_PER_GROUP)
    if excess:
        conn.execute("DELETE FROM learned_pairs WHERE id IN (SELECT id FROM learned_pairs WHERE chat_id=? ORDER BY hits ASC,id ASC LIMIT ?)", (chat_id, excess))
    total = conn.execute("SELECT COUNT(*) n FROM learned_pairs").fetchone()["n"]
    excess = max(0, int(total) - MAX_PAIRS_GLOBAL)
    if excess:
        conn.execute("DELETE FROM learned_pairs WHERE id IN (SELECT id FROM learned_pairs ORDER BY hits ASC,id ASC LIMIT ?)", (excess,))


def record_conversation(chat_id, user_id, message_text, reply_to_user_id=None):
    text = (message_text or "").strip()
    if len(text) < 3 or len(text) > MAX_MESSAGE_LENGTH:
        return
    now = time.time()
    with _db() as conn:
        previous = conn.execute("SELECT user_id,message_text,created_at FROM learn_pending WHERE chat_id=?", (chat_id,)).fetchone()
        if (previous and reply_to_user_id is not None and previous["user_id"] == reply_to_user_id
                and previous["user_id"] != user_id and now - float(previous["created_at"]) <= 300):
            prompt = previous["message_text"].strip()
            if len(prompt) >= 3 and normalize_text(prompt) != normalize_text(text):
                existing = conn.execute("SELECT id FROM learned_pairs WHERE chat_id=? AND prompt=? AND response=? ORDER BY id DESC LIMIT 1",
                                        (chat_id, prompt[:MAX_MESSAGE_LENGTH], text[:MAX_MESSAGE_LENGTH])).fetchone()
                if existing:
                    conn.execute("UPDATE learned_pairs SET hits=hits+1 WHERE id=?", (existing["id"],))
                else:
                    conn.execute("INSERT INTO learned_pairs(chat_id,prompt,response) VALUES(?,?,?)",
                                 (chat_id, prompt[:MAX_MESSAGE_LENGTH], text[:MAX_MESSAGE_LENGTH]))
                _enforce_limits(conn, chat_id)
        conn.execute("INSERT INTO learn_pending(chat_id,user_id,message_text,created_at) VALUES(?,?,?,?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id,message_text=excluded.message_text,created_at=excluded.created_at",
                     (chat_id, user_id, text[:MAX_MESSAGE_LENGTH], now))


def find_learned_reply(chat_id, incoming, threshold):
    with _db() as conn:
        rows = conn.execute("SELECT id,prompt,response,hits FROM learned_pairs WHERE chat_id=? ORDER BY hits DESC,id DESC LIMIT ?",
                            (chat_id, BEST_REPLIES_PER_GROUP)).fetchall()
    best, best_score = None, 0.0
    for row in rows:
        score = similarity(incoming, row["prompt"])
        if score > best_score:
            best, best_score = row, score
    return dict(best) if best is not None and best_score >= threshold / 100.0 else None


def list_learned(chat_id, limit=30):
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) n FROM learned_pairs WHERE chat_id=?", (chat_id,)).fetchone()["n"]
        rows = conn.execute("SELECT id,prompt,response,hits FROM learned_pairs WHERE chat_id=? ORDER BY hits DESC,id DESC LIMIT ?", (chat_id, limit)).fetchall()
    return total, [dict(row) for row in rows]


def delete_learned(chat_id, pair_id):
    with _db() as conn:
        cur = conn.execute("DELETE FROM learned_pairs WHERE id=? AND chat_id=?", (pair_id, chat_id))
        return cur.rowcount > 0


def clear_pending(chat_id):
    with _db() as conn:
        conn.execute("DELETE FROM learn_pending WHERE chat_id=?", (chat_id,))
