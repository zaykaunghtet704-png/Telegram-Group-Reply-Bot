"""Secure, single-use or multi-use promotional coin codes for the global game wallet."""
import hashlib
import secrets
import string
import time

from database import connect as db

CODE_ALPHABET = string.ascii_uppercase + string.digits


def init_coin_codes():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS coin_codes (
            code_hash TEXT PRIMARY KEY,
            code_hint TEXT NOT NULL,
            amount BIGINT NOT NULL,
            max_uses BIGINT NOT NULL DEFAULT 1,
            used_count BIGINT NOT NULL DEFAULT 0,
            created_by BIGINT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION,
            active BIGINT NOT NULL DEFAULT 1
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS coin_code_redemptions (
            code_hash TEXT NOT NULL REFERENCES coin_codes(code_hash) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            redeemed_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(code_hash, user_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_coin_codes_created ON coin_codes(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_coin_code_redemptions_user ON coin_code_redemptions(user_id)")


def _hash_code(code):
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def create_coin_code(amount, uses, owner_id, expiry_hours=0):
    now = time.time()
    expires_at = now + expiry_hours * 3600 if expiry_hours else None
    for _ in range(8):
        code = "MYAE-" + "-".join(
            "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
            for _ in range(3)
        )
        digest = _hash_code(code)
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM coin_codes WHERE code_hash=?", (digest,)).fetchone()
            if exists:
                continue
            conn.execute(
                """INSERT INTO coin_codes
                   (code_hash,code_hint,amount,max_uses,created_by,created_at,expires_at,active)
                   VALUES(?,?,?,?,?,?,?,1)""",
                (digest, code[-4:], int(amount), int(uses), int(owner_id), now, expires_at),
            )
        return code
    raise RuntimeError("Could not generate a unique coin code")


def redeem_coin_code(code, user_id):
    normalized = (code or "").strip().upper()
    if len(normalized) < 8 or len(normalized) > 40:
        return {"ok": False, "reason": "invalid"}
    digest = _hash_code(normalized)
    now = time.time()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM coin_codes WHERE code_hash=? FOR UPDATE", (digest,)
        ).fetchone()
        if not row:
            return {"ok": False, "reason": "invalid"}
        if not row["active"]:
            return {"ok": False, "reason": "inactive"}
        if row["expires_at"] is not None and float(row["expires_at"]) <= now:
            return {"ok": False, "reason": "expired"}
        if int(row["used_count"]) >= int(row["max_uses"]):
            return {"ok": False, "reason": "used_up"}
        prior = conn.execute(
            "SELECT 1 FROM coin_code_redemptions WHERE code_hash=? AND user_id=?",
            (digest, int(user_id)),
        ).fetchone()
        if prior:
            return {"ok": False, "reason": "already"}
        # Seed a wallet from the user's previous highest group balance, or the normal
        # 100-Coin starting balance when this is their first game interaction.
        conn.execute(
            """INSERT INTO game_wallets(user_id,coins)
               SELECT user_id,MAX(coins) FROM game_accounts WHERE user_id=?
               GROUP BY user_id ON CONFLICT(user_id) DO NOTHING""",
            (int(user_id),),
        )
        conn.execute(
            "INSERT INTO game_wallets(user_id,coins) VALUES(?,100) ON CONFLICT(user_id) DO NOTHING",
            (int(user_id),),
        )
        conn.execute(
            "INSERT INTO coin_code_redemptions(code_hash,user_id,redeemed_at) VALUES(?,?,?)",
            (digest, int(user_id), now),
        )
        conn.execute(
            "UPDATE coin_codes SET used_count=used_count+1 WHERE code_hash=?",
            (digest,),
        )
        wallet = conn.execute(
            "UPDATE game_wallets SET coins=coins+? WHERE user_id=? RETURNING coins",
            (int(row["amount"]), int(user_id)),
        ).fetchone()
        balance = int(wallet["coins"])
        conn.execute(
            "UPDATE game_accounts SET coins=? WHERE user_id=? AND coins<>?",
            (balance, int(user_id), balance),
        )
        return {
            "ok": True,
            "amount": int(row["amount"]),
            "balance": balance,
            "remaining": int(row["max_uses"]) - int(row["used_count"]) - 1,
        }


def revoke_coin_code(code):
    digest = _hash_code(code)
    with db() as conn:
        cur = conn.execute(
            "UPDATE coin_codes SET active=0 WHERE code_hash=? AND active=1",
            (digest,),
        )
        return cur.rowcount > 0


def coin_code_stats():
    with db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN active=1 AND used_count<max_uses
                                    AND (expires_at IS NULL OR expires_at>?) THEN 1 ELSE 0 END) AS active,
                      COALESCE(SUM(used_count),0) AS redeemed,
                      COALESCE(SUM(amount*used_count),0) AS coins_sent
                 FROM coin_codes""",
            (time.time(),),
        ).fetchone()
    return {key: int(value or 0) for key, value in row.items()}
