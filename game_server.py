"""3D Telegram Mini App game server. Authenticates Telegram WebApp initData and reuses the bot PostgreSQL coin accounts."""
import hashlib
import hmac
import json
import os
import random
import time
import urllib.parse
import urllib.request
from flask import Flask, jsonify, request, send_from_directory
from database import connect as db

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "game_web")
app = Flask(__name__, static_folder=WEB_DIR)


def _telegram_user(init_data):
    if not BOT_TOKEN or not init_data:
        return None
    values = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    supplied = values.pop("hash", "")
    if not supplied:
        return None
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        return None
    try:
        auth_date = int(values.get("auth_date", "0"))
        if abs(time.time() - auth_date) > 86400:
            return None
        user = json.loads(values["user"])
        uid = int(user["id"])
        if user.get("is_bot"):
            return None
        return {"id": uid, "name": (user.get("first_name", "Player") + (" " + user.get("last_name", "") if user.get("last_name") else ""))[:80],
                "username": user.get("username", "")}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _api_member(chat_id, user_id):
    """Confirm the signed-in player is still a member of the selected Telegram group."""
    if not BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
    body = urllib.parse.urlencode({"chat_id": chat_id, "user_id": user_id}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=8) as response:
            data = json.loads(response.read().decode())
        status = data.get("result", {}).get("status")
        return bool(data.get("ok") and status in ("creator", "administrator", "member", "restricted"))
    except Exception:
        app.logger.exception("Telegram group membership verification failed")
        return False


def _session():
    user = _telegram_user(request.headers.get("X-Telegram-Init-Data", ""))
    try:
        chat_id = int(request.args.get("chat_id") or request.json.get("chat_id"))
    except (TypeError, ValueError, AttributeError):
        return None, None, (jsonify({"error": "Group session မတွေ့ပါ။ Bot မှ /games ကို ပြန်ဖွင့်ပါ။"}), 400)
    if not user:
        return None, None, (jsonify({"error": "Telegram ထဲကနေ Game ကို ဖွင့်ပါ။"}), 401)
    if not _api_member(chat_id, user["id"]):
        return None, None, (jsonify({"error": "ဒီ Group ရဲ့ Member မဟုတ်ပါ။"}), 403)
    with db() as conn:
        enabled = conn.execute("SELECT enabled FROM game_groups WHERE chat_id=?", (chat_id,)).fetchone()
        if enabled and not enabled["enabled"]:
            return None, None, (jsonify({"error": "ဒီ Group မှာ Game ပိတ်ထားပါတယ်။ Admin ကို /game on လုပ်ခိုင်းပါ။"}), 403)
        conn.execute(
            "INSERT OR IGNORE INTO game_accounts(chat_id,user_id,username,display_name,coins) VALUES(?,?,?,?,100)",
            (chat_id, user["id"], user["username"], user["name"]),
        )
        conn.execute("UPDATE game_accounts SET username=?,display_name=? WHERE chat_id=? AND user_id=?",
                     (user["username"], user["name"], chat_id, user["id"]))
    return user, chat_id, None


def _account(chat_id, user_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM game_accounts WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    return dict(row) if row else None


def _reward(chat_id, user_id, coins=0, xp=0, wins=0, losses=0):
    with db() as conn:
        conn.execute(
            "UPDATE game_accounts SET coins=GREATEST(0,coins+?),xp=xp+?,wins=wins+?,losses=losses+? WHERE chat_id=? AND user_id=?",
            (coins, xp, wins, losses, chat_id, user_id),
        )
    return _account(chat_id, user_id)


def _level(xp):
    return 1 + int(max(0, xp) // 100)


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "pro-reply-3d-games"})


@app.get("/api/profile")
def profile():
    user, chat_id, error = _session()
    if error:
        return error
    row = _account(chat_id, user["id"])
    return jsonify({"user": user, "account": row, "level": _level(row["xp"])})


@app.post("/api/action/<action>")
def action(action):
    user, chat_id, error = _session()
    if error:
        return error
    now = time.time()
    cooldowns = {"fish": 60, "hunt": 120, "spin": 43200, "quiz": 0, "duel": 0}
    if action not in cooldowns:
        return jsonify({"error": "Unknown game action"}), 404
    with db() as conn:
        previous = conn.execute(
            "SELECT last_used FROM game_cooldowns WHERE chat_id=? AND user_id=? AND action=?",
            (chat_id, user["id"], action),
        ).fetchone()
        wait = cooldowns[action] - (now - float(previous["last_used"])) if previous else 0
        if wait > 0:
            return jsonify({"error": f"နောက်တစ်ကြိမ်ကစားရန် {int(wait)+1} စက္ကန့် စောင့်ပါ။", "wait": int(wait)+1}), 429
        conn.execute(
            "INSERT INTO game_cooldowns(chat_id,user_id,action,last_used) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id,user_id,action) DO UPDATE SET last_used=excluded.last_used",
            (chat_id, user["id"], action, now),
        )

    if action == "fish":
        caught = random.choices(
            [("ငါးသေး", 5, 12), ("ငါးကြင်း", 12, 22), ("ငါးကြီး", 20, 35), ("ရွှေငါး", 35, 60)],
            weights=[55, 28, 13, 4], k=1
        )[0]
        amount = random.randint(caught[1], caught[2])
        row = _reward(chat_id, user["id"], amount, 5)
        return jsonify({"message": f"{caught[0]} ဖမ်းမိပြီ!", "coins": amount, "account": row, "level": _level(row["xp"])})
    if action == "hunt":
        monster = random.choice(["Goblin", "Wolf", "Shadow Beast", "Dragonling"])
        if random.randint(1, 100) <= 65:
            amount = random.randint(20, 55)
            row = _reward(chat_id, user["id"], amount, 20, wins=1)
            return jsonify({"message": f"{monster} ကို အနိုင်ယူလိုက်ပြီ!", "coins": amount, "xp": 20, "won": True, "account": row, "level": _level(row["xp"])})
        row = _reward(chat_id, user["id"], 0, 5, losses=1)
        return jsonify({"message": f"{monster} က ဒီတစ်ကြိမ် အနိုင်ရသွားတယ်။", "coins": 0, "xp": 5, "won": False, "account": row, "level": _level(row["xp"])})
    if action == "spin":
        amount = random.choices([0, 10, 25, 50, 100, 200], weights=[20, 25, 25, 18, 10, 2], k=1)[0]
        row = _reward(chat_id, user["id"], amount, 3 if amount else 0)
        return jsonify({"message": "Lucky Spin!", "coins": amount, "account": row, "level": _level(row["xp"])})
    if action == "quiz":
        questions = [
            {"q": "မြန်မာနိုင်ငံ၏ မြို့တော်က ဘယ်မြို့လဲ?", "a": ["ရန်ကုန်", "နေပြည်တော်", "မန္တလေး", "ပဲခူး"], "correct": 1},
            {"q": "တစ်ပတ်မှာ ရက်ဘယ်နှစ်ရက်ရှိလဲ?", "a": ["၅", "၆", "၇", "၈"], "correct": 2},
            {"q": "ရေ၏ ဓာတုဖော်မြူလာက ဘာလဲ?", "a": ["CO2", "H2O", "O2", "NaCl"], "correct": 1},
        ]
        q = random.choice(questions)
        return jsonify({"question": q, "account": _account(chat_id, user["id"])})
    return jsonify({"error": "Duel ကို နောက်ထပ်အဆင့်မှာ Multiplayer Arena အဖြစ် ချိတ်ဆက်ပေးမယ်။"}), 501


@app.post("/api/quiz-answer")
def quiz_answer():
    user, chat_id, error = _session()
    if error:
        return error
    try:
        choice = int((request.get_json(silent=True) or {}).get("choice", -1))
    except (TypeError, ValueError):
        choice = -1
    # Server-selected question is stored per signed-in player to prevent client-side reward spoofing.
    key = f"mini_quiz_{chat_id}_{user['id']}"
    with db() as conn:
        row = conn.execute("SELECT setting_value FROM owner_settings WHERE setting_key=?", (key,)).fetchone()
        if not row:
            return jsonify({"error": "Quiz မေးခွန်းမရှိပါ။ အသစ်စပါ။"}), 400
        q = json.loads(row["setting_value"])
        if time.time() > q["expires"]:
            conn.execute("DELETE FROM owner_settings WHERE setting_key=?", (key,))
            return jsonify({"error": "Quiz အချိန်ကုန်သွားပါပြီ။"}), 400
        conn.execute("DELETE FROM owner_settings WHERE setting_key=?", (key,))
    if choice != q["correct"]:
        return jsonify({"message": "အဖြေမမှန်ပါ။", "correct": False, "account": _account(chat_id, user["id"] )})
    row = _reward(chat_id, user["id"], 25, 10, wins=1)
    return jsonify({"message": "အဖြေမှန်ပါတယ်! +25 Coin / +10 XP", "correct": True, "coins": 25, "account": row, "level": _level(row["xp"])})


@app.post("/api/quiz")
def create_quiz():
    user, chat_id, error = _session()
    if error:
        return error
    questions = [
        {"q": "မြန်မာနိုင်ငံ၏ မြို့တော်က ဘယ်မြို့လဲ?", "a": ["ရန်ကုန်", "နေပြည်တော်", "မန္တလေး", "ပဲခူး"], "correct": 1},
        {"q": "တစ်ပတ်မှာ ရက်ဘယ်နှစ်ရက်ရှိလဲ?", "a": ["၅", "၆", "၇", "၈"], "correct": 2},
        {"q": "ရေ၏ ဓာတုဖော်မြူလာက ဘာလဲ?", "a": ["CO2", "H2O", "O2", "NaCl"], "correct": 1},
    ]
    q = random.choice(questions)
    key = f"mini_quiz_{chat_id}_{user['id']}"
    with db() as conn:
        conn.execute("INSERT INTO owner_settings(setting_key,setting_value) VALUES(?,?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                     (key, json.dumps({"q": q["q"], "a": q["a"], "correct": q["correct"], "expires": time.time()+90}, ensure_ascii=False)))
    return jsonify({"question": {"q": q["q"], "a": q["a"]}, "account": _account(chat_id, user["id"])})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
