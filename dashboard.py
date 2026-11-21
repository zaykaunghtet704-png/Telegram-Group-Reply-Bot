"""Owner-only web dashboard. Run as a separate Render Web Service."""
import hmac
import os
import secrets
from functools import wraps

from flask import Flask, abort, redirect, render_template_string, request, session, url_for
from database import connect

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not DASHBOARD_PASSWORD or not SECRET_KEY:
    raise RuntimeError("Set DASHBOARD_PASSWORD and SECRET_KEY for the dashboard service.")

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=3600,
)

LOGIN_PAGE = """
<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PRO Reply Bot — Owner Login</title>
<style>
body{font-family:system-ui;background:#101827;color:#eef2ff;display:grid;place-items:center;min-height:100vh;margin:0}
main{width:min(90%,390px);background:#1c293d;padding:28px;border-radius:16px}
input,button{box-sizing:border-box;width:100%;padding:13px;margin-top:12px;border-radius:9px;border:0}
button{background:#4d79e6;color:white;font-weight:700}small{color:#b8c3d8}
</style><main><h2>PRO Reply Bot</h2><p>Owner Dashboard</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<input type="password" name="password" placeholder="Dashboard password" required>
<button type="submit">Sign in</button></form><small>{{ error }}</small></main></html>
"""

DASHBOARD_PAGE = """
<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PRO Reply Bot Dashboard</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui;background:#101827;color:#eef2ff;margin:0;padding:20px}
main{max-width:1000px;margin:auto}header,.stats,.group{background:#1c293d;border-radius:14px;padding:16px;margin-bottom:14px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.stats{display:flex;gap:24px;flex-wrap:wrap}.stat strong{display:block;font-size:25px}
.group{display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap}
button{border:0;border-radius:8px;padding:10px 14px;background:#4d79e6;color:white;font-weight:700}
a{color:#9bb8ff}.muted{color:#b8c3d8;font-size:13px}.tag{padding:4px 9px;border-radius:20px;background:#33435e}
</style><main>
<header><div><h2>PRO Reply Bot — Owner Dashboard</h2><div class="muted">PostgreSQL connected · Owner-only</div></div>
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf }}"><button>Logout</button></form></header>
<section class="stats">
<div class="stat"><strong>{{ groups|length }}</strong><span>Known groups</span></div>
<div class="stat"><strong>{{ active }}</strong><span>Active groups</span></div>
<div class="stat"><strong>{{ users }}</strong><span>Private users</span></div>
<div class="stat"><strong>{{ replies }}</strong><span>Saved replies</span></div>
</section>
<h3>Group management</h3>
{% for g in groups %}
<section class="group"><div><strong>{{ g.title or 'Untitled group' }}</strong>
<div class="muted">ID: {{ g.chat_id }} · Replies: {{ g.reply_count }} · {{ 'Active' if g.active else 'Inactive' }}</div>
<span class="tag">Auto Reply: {{ 'ON' if g.enabled else 'OFF' }}</span></div>
<form method="post" action="/groups/{{ g.chat_id }}/toggle">
<input type="hidden" name="csrf" value="{{ csrf }}">
<button>{{ 'Turn OFF' if g.enabled else 'Turn ON' }} Auto Reply</button></form></section>
{% else %}<p>No groups have been recorded yet.</p>{% endfor %}
<p class="muted">Changes apply to the selected group only. This dashboard does not expose bot tokens or database credentials.</p>
</main></html>
"""


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def check_csrf():
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        abort(400, "Invalid form token")


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("owner_authenticated"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


@app.get("/health")
def health():
    return {"ok": True, "service": "pro-reply-owner-dashboard"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        check_csrf()
        supplied = request.form.get("password", "")
        if hmac.compare_digest(supplied, DASHBOARD_PASSWORD):
            session.clear()
            session["owner_authenticated"] = True
            session.permanent = True
            csrf_token()
            return redirect(url_for("home"))
        error = "Incorrect password"
    return render_template_string(LOGIN_PAGE, csrf=csrf_token(), error=error)


@app.get("/")
@login_required
def home():
    with connect() as conn:
        groups = conn.execute(
            """SELECT g.chat_id,g.title,g.active,
                      COALESCE(s.enabled,1) AS enabled,
                      COUNT(r.id) AS reply_count
               FROM known_groups g
               LEFT JOIN settings s ON s.chat_id=g.chat_id
               LEFT JOIN replies r ON r.chat_id=g.chat_id
               GROUP BY g.chat_id,g.title,g.active,s.enabled
               ORDER BY g.first_seen DESC"""
        ).fetchall()
        users = conn.execute("SELECT COUNT(*) AS n FROM known_users WHERE ads_enabled=1").fetchone()["n"]
        replies = conn.execute("SELECT COUNT(*) AS n FROM replies").fetchone()["n"]
    active = sum(1 for group in groups if group["active"])
    return render_template_string(
        DASHBOARD_PAGE, groups=groups, active=active, users=users,
        replies=replies, csrf=csrf_token(),
    )


@app.post("/groups/<int:chat_id>/toggle")
@login_required
def toggle_group(chat_id):
    check_csrf()
    with connect() as conn:
        row = conn.execute("SELECT enabled FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        current = int(row["enabled"]) if row else 1
        next_value = 0 if current else 1
        conn.execute("INSERT INTO settings(chat_id) VALUES(?) ON CONFLICT(chat_id) DO NOTHING", (chat_id,))
        conn.execute("UPDATE settings SET enabled=? WHERE chat_id=?", (next_value, chat_id))
    return redirect(url_for("home"))


@app.post("/logout")
@login_required
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
