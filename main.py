# TITAN Enterprise — Ultimate Edition v11.0
# Install: pip install telethon flask waitress aiosqlite psutil aiohttp

import os
import re
import json
import time
import hmac
import uuid
import asyncio
import secrets
import hashlib
import logging
import threading
import datetime as dt
from functools import wraps

import aiosqlite
from flask import Flask, request, jsonify, redirect, url_for, session, render_template_string, Response
from waitress import serve
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, FloodWaitError, PhoneCodeInvalidError, 
    PasswordHashInvalidError, ChatWriteForbiddenError, SlowModeWaitError
)
from telethon.tl.functions.channels import JoinChannelRequest

try:
    import psutil
except ImportError:
    psutil = None

# -------------------- CONFIG --------------------

API_ID = int(os.getenv("API_ID", "2040"))
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8732669492:AAGjBx_kMW4JRpo4DyhCoRwAISSS-lJ1IhE")
FLASK_SECRET = os.getenv("FLASK_SECRET", "titan_secret_key_secure_1234567890")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "titan_enterprise.db")
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.getenv("PORT", "10000"))

SUPER_ADMINS = {
    int(x.strip()) for x in os.getenv("SUPER_ADMINS", "8655682794,8773171051").split(",")
    if x.strip().isdigit()
}
MAX_ACTIVE_CLIENTS = max(1, min(50, int(os.getenv("MAX_ACTIVE_CLIENTS", "10"))))
LOGIN_TOKEN_TTL = 24 * 60 * 60
SESSION_DAYS = 14

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("titan")

app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=SESSION_DAYS),
    MAX_CONTENT_LENGTH=64 * 1024
)

bot = None
db = None
loop = None
client_manager = None
workers = {}
worker_stops = {}
web_flows = {}

def utcnow(): return dt.datetime.now(dt.timezone.utc)
def iso_now(): return utcnow().isoformat()
def is_admin(uid): return int(uid) in SUPER_ADMINS
def token_hash(value): return hashlib.sha256(value.encode("utf-8")).hexdigest()

def parse_targets(raw):
    result = []
    for line in raw.splitlines():
        item = line.strip()
        if not item: continue
        if item.startswith("tg://chat?id="):
            try:
                target = int(item.split("=")[1])
                if target not in result: result.append(target)
                continue
            except: pass
        item = re.sub(r"^https?://t\.me/", "", item).strip("/")
        item = re.sub(r"\?.*$", "", item)
        if item.startswith("c/"):
            parts = item.split("/")
            if len(parts) >= 2 and parts[1].isdigit(): target = int("-100" + parts[1])
            else: continue
        elif item.startswith("@"): target = item
        elif re.fullmatch(r"-?\d+", item): target = int(item)
        elif re.fullmatch(r"[A-Za-z0-9_]{5,}", item): target = "@" + item
        else: continue
        if target not in result: result.append(target)
    return result

# -------------------- DATABASE --------------------

class Database:
    def __init__(self, path):
        self.path = path

    def connect(self):
        return aiosqlite.connect(self.path, timeout=15)

    async def initialize(self):
        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=15000")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'USER', approved INTEGER NOT NULL DEFAULT 0,
                    session_string TEXT NOT NULL DEFAULT '', subscription_until TEXT NOT NULL DEFAULT '',
                    templates TEXT NOT NULL DEFAULT '[]', targets TEXT NOT NULL DEFAULT '[]', 
                    delay INTEGER NOT NULL DEFAULT 10, balance INTEGER NOT NULL DEFAULT 0, 
                    created_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS login_tokens(
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS web_sessions(
                    sid_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at REAL NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_stats(
                    user_id INTEGER PRIMARY KEY, sent INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS shop_orders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    item_id TEXT, country TEXT, price_buy INTEGER, price_sell INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending', data TEXT, created_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY, value TEXT
                )
            """)
            await db.commit()

        async with self.connect() as db:
            for uid in SUPER_ADMINS:
                await db.execute("""
                    INSERT INTO users(user_id, role, approved, created_at)
                    VALUES(?, 'ADMIN', 1, ?) ON CONFLICT(user_id) DO UPDATE SET role='ADMIN', approved=1
                """, (uid, iso_now()))
                await db.execute("INSERT OR IGNORE INTO user_stats(user_id) VALUES(?)", (uid,))
            await db.commit()

    async def get_user(self, uid):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE user_id=?", (int(uid),))
            return await cur.fetchone()

    async def ensure_user(self, uid, username=""):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO users(user_id, username, created_at) VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
            """, (int(uid), username or "", iso_now()))
            await db.execute("INSERT OR IGNORE INTO user_stats(user_id) VALUES(?)", (int(uid),))
            await db.commit()

    async def all_users(self):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT u.user_id,u.username,u.role,u.approved,u.subscription_until,u.balance,
                       CASE WHEN u.session_string!='' THEN 1 ELSE 0 END AS has_session,
                       s.sent,s.errors
                FROM users u LEFT JOIN user_stats s ON s.user_id=u.user_id
                ORDER BY u.created_at DESC LIMIT 500
            """)
            return await cur.fetchall()

    async def create_login_token(self, uid):
        raw = secrets.token_urlsafe(32)
        async with self.connect() as db:
            await db.execute("DELETE FROM login_tokens WHERE expires_at<?", (time.time(),))
            await db.execute("INSERT INTO login_tokens VALUES(?,?,?)", (token_hash(raw), int(uid), time.time() + LOGIN_TOKEN_TTL))
            await db.commit()
        return raw

    async def consume_login_token(self, raw):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT user_id, expires_at FROM login_tokens WHERE token_hash=?", (token_hash(raw),)) as cur:
                row = await cur.fetchone()
            if not row or time.time() > row["expires_at"]: return None
            return int(row["user_id"])

    async def create_web_session(self, uid):
        raw = secrets.token_urlsafe(40)
        async with self.connect() as db:
            await db.execute("DELETE FROM web_sessions WHERE expires_at<?", (time.time(),))
            await db.execute("INSERT INTO web_sessions VALUES(?,?,?)", (token_hash(raw), int(uid), time.time() + SESSION_DAYS * 86400))
            await db.commit()
        return raw

    async def get_web_session(self, raw):
        if not raw: return None
        async with self.connect() as db:
            cur = await db.execute("SELECT user_id FROM web_sessions WHERE sid_hash=? AND expires_at>?", (token_hash(raw), time.time()))
            row = await cur.fetchone()
            return int(row[0]) if row else None

    async def delete_web_session(self, raw):
        if raw:
            async with self.connect() as db:
                await db.execute("DELETE FROM web_sessions WHERE sid_hash=?", (token_hash(raw),))
                await db.commit()

    async def audit(self, uid, action, details=""):
        async with self.connect() as db:
            await db.execute("INSERT INTO audit(user_id,action,details,created_at) VALUES(?,?,?,?)",
                (int(uid), action[:80], str(details)[:500], iso_now()))
            await db.commit()

    async def update_user(self, uid, **kwargs):
        allowed = {"templates", "targets", "delay", "session_string", "subscription_until", "approved", "balance"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields: return
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for v in fields.values()]
        async with self.connect() as db:
            await db.execute(f"UPDATE users SET {cols} WHERE user_id=?", (*vals, int(uid)))
            await db.commit()

    async def stats(self, uid):
        async with self.connect() as db:
            cur = await db.execute("SELECT sent,errors FROM user_stats WHERE user_id=?", (int(uid),))
            row = await cur.fetchone()
            return {"sent": row[0], "errors": row[1]} if row else {"sent": 0, "errors": 0}

    async def increment_stat(self, uid, column):
        if column not in ("sent", "errors"): return
        async with self.connect() as db:
            await db.execute("INSERT OR IGNORE INTO user_stats(user_id) VALUES(?)", (int(uid),))
            await db.execute(f"UPDATE user_stats SET {column}={column}+1 WHERE user_id=?", (int(uid),))
            await db.commit()

    async def sub_active(self, uid):
        u = await self.get_user(uid)
        if not u or not u["subscription_until"]: return False
        try: return dt.datetime.fromisoformat(u["subscription_until"]) > utcnow()
        except: return False

    async def grant_sub(self, uid, days):
        u = await self.get_user(uid)
        start = utcnow()
        if u and u["subscription_until"]:
            try:
                old = dt.datetime.fromisoformat(u["subscription_until"])
                if old > start: start = old
            except: pass
        expiry = (start + dt.timedelta(days=days)).isoformat()
        await self.update_user(uid, subscription_until=expiry, approved=1)
        return expiry

    async def get_setting(self, key, default=""):
        async with self.connect() as db:
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_setting(self, key, value):
        async with self.connect() as db:
            await db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))
            await db.commit()

    async def get_orders(self):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM shop_orders ORDER BY id DESC LIMIT 50")
            return await cur.fetchall()

# -------------------- CLIENT MANAGER --------------------

class ClientManager:
    def __init__(self, db, max_clients):
        self.db = db
        self.semaphore = asyncio.Semaphore(max_clients)
        self.active = 0

    async def acquire(self, uid):
        await self.semaphore.acquire()
        self.active += 1
        client = None
        try:
            u = await self.db.get_user(uid)
            if not u or not u["session_string"]: raise RuntimeError("Нет сессии")
            client = TelegramClient(StringSession(u["session_string"]), API_ID, API_HASH,
                                    connection_retries=1, request_retries=1, timeout=15, auto_reconnect=False)
            await client.connect()
            if not await client.is_user_authorized(): raise RuntimeError("Сессия мертва")
            yield client
        finally:
            if client: await client.disconnect()
            self.active -= 1
            self.semaphore.release()

# -------------------- BOT --------------------

async def issue_web_login(event):
    uid = int(event.sender_id)
    sender = await event.get_sender()
    await db.ensure_user(uid, getattr(sender, "username", "") or "")
    if not (await db.get_user(uid))["approved"] and not is_admin(uid):
        return await event.respond(f"Доступ закрыт. ID: {uid}")
    raw = await db.create_login_token(uid)
    link = f"{PUBLIC_BASE_URL}/login?token={raw}"
    await event.respond(f"🔐 Ссылка:\n{link}")
    await db.audit(uid, "WEB_LOGIN")

async def setup_bot():
    global bot
    bot = TelegramClient("titan_bot", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)

    @bot.on(events.NewMessage(pattern=r"^/start$"))
    async def start_handler(event):
        uid = event.sender_id
        await db.ensure_user(uid)
        if not (await db.get_user(uid))["approved"] and not is_admin(uid):
            return await event.respond(f"Доступ закрыт. ID: {uid}")
        await event.respond("TITAN Enterprise", buttons=[[Button.inline("🌐 Войти в панель", b"web_login")]])

    @bot.on(events.CallbackQuery)
    async def callback_handler(event):
        if event.data == b"web_login":
            await issue_web_login(event)
            await event.answer("Ссылка отправлена")
        else:
            await event.answer()

# -------------------- FLASK HELPERS --------------------

def run_async(coro):
    if not loop: raise RuntimeError("Loop is not ready")
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=20)

def load_user():
    raw = request.cookies.get("titan_sid", "")
    uid = run_async(db.get_web_session(raw)) if raw else None
    return run_async(db.get_user(uid)) if uid else None

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = load_user()
        if not u: return jsonify({"error": "unauthorized"}), 401
        if not u["approved"] and u["role"] != "ADMIN": return jsonify({"error": "access_revoked"}), 403
        request.titan_user = u
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if request.titan_user["role"] != "ADMIN": return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper

def csrf_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not hmac.compare_digest(session.get("csrf", ""), request.headers.get("X-CSRF-Token", "")):
            return jsonify({"error": "csrf"}), 400
        return f(*args, **kwargs)
    return wrapper

# -------------------- WEB UI (НОВЫЙ УНИКАЛЬНЫЙ ДИЗАЙН) --------------------

LOGIN_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN / Auth</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
body{margin:0;height:100vh;display:grid;place-items:center;background:#050505;color:#fff;font-family:'Syne',sans-serif}
.box{background:#111;border:1px solid #222;padding:40px;border-radius:24px;text-align:center;max-width:400px}
h1{font-size:32px;margin:0 0 10px;letter-spacing:-1px;background:linear-gradient(90deg,#ff0055,#ff9900);-webkit-background-clip:text;color:transparent}
p{color:#777;font-size:14px;line-height:1.5}
</style></head><body><div class="box"><h1>TITAN ENTERPRISE</h1><p>{{ message }}</p></div></body></html>
"""

DASHBOARD_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN / Control</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#050505;--card:#0f0f0f;--line:#222;--txt:#fff;--muted:#777;--accent:#ff0055;--accent2:#ff9900}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px 'DM Sans',sans-serif;min-height:100vh}
h1,h2,h3,b,strong{font-family:'Syne',sans-serif}
.layout{display:flex;min-height:100vh}
aside{width:260px;background:#080808;border-right:1px solid var(--line);padding:30px 20px;display:flex;flex-direction:column;position:fixed;height:100vh}
.logo{font-family:'Syne';font-weight:800;font-size:20px;letter-spacing:1px;margin-bottom:40px}
.logo span{color:var(--accent)}
.nav{display:flex;flex-direction:column;gap:5px;flex:1}
.nav a{color:var(--muted);text-decoration:none;padding:14px 16px;border-radius:12px;font-weight:500;transition:0.2s;cursor:pointer;display:flex;align-items:center;gap:10px}
.nav a:hover,.nav a.active{background:linear-gradient(90deg,#1a1a1a,#111);color:#fff;border:1px solid var(--line)}
.nav a.active{border-left:2px solid var(--accent)}
main{margin-left:260px;padding:40px;flex:1;max-width:1200px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:40px}
.top h1{font-size:32px;letter-spacing:-1px}
.btn{padding:12px 20px;border-radius:12px;background:#fff;color:#000;font-weight:700;border:0;cursor:pointer;transition:0.2s;font-family:'Syne'}
.btn:hover{transform:scale(0.98)}
.btn.grad{background:linear-gradient(90deg,var(--accent),var(--accent2));color:#fff}
.btn.outline{background:transparent;border:1px solid var(--line);color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;margin-bottom:40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:24px;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;width:100%;height:2px;background:linear-gradient(90deg,transparent,var(--line),transparent)}
.metric{font-size:32px;font-weight:800;font-family:'Syne';margin-top:15px}
.metric.accent{background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;color:transparent}
.kicker{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:1.5px}
.section{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:30px;margin-bottom:24px}
.section h2{font-size:20px;margin-bottom:20px}
input,textarea{width:100%;background:#080808;border:1px solid var(--line);color:#fff;padding:15px;border-radius:12px;outline:none;font:14px 'DM Sans'}
input:focus,textarea:focus{border-color:var(--accent)}
.field{margin-bottom:20px}
.field label{display:block;margin-bottom:8px;color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:15px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:1px}
.tag{background:#1a1a1a;padding:4px 10px;border-radius:6px;font-size:12px;border:1px solid var(--line)}
.tag.ok{color:#00ff88;border-color:#00ff8833}
.tag.bad{color:var(--accent);border-color:#ff005533}
.hidden{display:none!important}
.disabled{opacity:0.5;pointer-events:none}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.8);backdrop-filter:blur(10px);place-items:center;z-index:100}
.modal-content{background:var(--card);border:1px solid var(--line);border-radius:24px;padding:30px;width:100%;max-width:420px}
@media(max-width:900px){.layout{display:block}aside{position:static;width:100%;height:auto;border-right:0;border-bottom:1px solid var(--line);padding:15px}
main{margin-left:0;padding:20px}.nav{flex-direction:row;overflow-x:auto;padding-bottom:10px}.nav a{white-space:nowrap;padding:10px 15px}.top h1{font-size:24px}}
</style></head><body>
<div class="layout">
<aside>
<div class="logo">TITAN<span>.</span></div>
<div class="nav">
<a class="active" onclick="showTab('overview')">⚡ Обзор</a>
<a onclick="showTab('settings')">⚙️ Настройки</a>
<a onclick="showTab('shop')">🛍 Магазин</a>
<a onclick="showTab('orders')">📦 Заказы</a>
<a onclick="showTab('help')">📚 Помощь</a>
<a class="admin-only hidden" onclick="showTab('admin')">👑 Админка</a>
<a href="/logout" style="margin-top:auto;color:var(--accent)">Выйти</a>
</div>
</aside>
<main>
<div class="top">
<div><h1 id="page-title">Панель управления</h1><div class="kicker" id="page-sub">Личный кабинет</div></div>
<button class="btn outline" onclick="load()">↻ Обновить</button>
</div>

<!-- ВКЛАДКА: ОБЗОР -->
<div id="tab-overview" class="tab-content">
<div class="grid">
<div class="card"><div class="kicker">Подписка</div><div class="metric accent" id="ui-sub">—</div></div>
<div class="card"><div class="kicker">Баланс</div><div class="metric" id="ui-bal">—</div><button class="btn outline" style="margin-top:10px;font-size:12px;padding:8px" onclick="showPayment()">Пополнить</button></div>
<div class="card"><div class="kicker">Отправлено</div><div class="metric" id="ui-sent">0</div></div>
<div class="card"><div class="kicker">Telegram</div><div class="metric" id="ui-acc">—</div><button class="btn outline" style="margin-top:10px;font-size:12px;padding:8px" onclick="openAccModal()">Подключить</button></div>
</div>
</div>

<!-- ВКЛАДКА: НАСТРОЙКИ -->
<div id="tab-settings" class="tab-content hidden">
<div class="section">
<h2>Шаблоны</h2>
<textarea id="ui-templates" placeholder="Каждая строка — новый шаблон" style="height:150px"></textarea>
<button class="btn grad" style="margin-top:15px" onclick="saveTemplates()">Сохранить</button>
</div>
<div class="section">
<h2>Цели (ID чатов)</h2>
<textarea id="ui-targets" placeholder="@channel&#10;-100123456789" style="height:120px"></textarea>
<button class="btn grad" style="margin-top:15px" onclick="saveTargets()">Сохранить</button>
</div>
<div class="section">
<h2>Задержка</h2>
<div class="field"><input type="number" id="ui-delay" min="0" max="3600" step="0.1" placeholder="0 = экстрим"></div>
<button class="btn grad" onclick="saveDelay()">Сохранить</button>
</div>
<div class="section" style="display:flex;gap:15px">
<button class="btn grad" onclick="jobAction('start')" style="flex:1">▶ Запустить</button>
<button class="btn outline" onclick="jobAction('stop')" style="flex:1;color:var(--accent)">⏹ Остановить</button>
</div>
</div>

<!-- ВКЛАДКА: МАГАЗИН (НЕДОСТУПНО) -->
<div id="tab-shop" class="tab-content hidden">
<div class="section" style="text-align:center;padding:60px">
<h2 style="color:var(--accent)">Магазин временно недоступен</h2>
<p style="color:var(--muted);margin-top:10px">Мы обновляем систему. Заходите позже.</p>
</div>
</div>

<!-- ВКЛАДКА: ЗАКАЗЫ (НЕДОСТУПНО) -->
<div id="tab-orders" class="tab-content hidden">
<div class="section" style="text-align:center;padding:60px">
<h2 style="color:var(--accent)">Заказы временно недоступны</h2>
<p style="color:var(--muted);margin-top:10px">Мы обновляем систему. Заходите позже.</p>
</div>
</div>

<!-- ВКЛАДКА: ПОМОЩЬ -->
<div id="tab-help" class="tab-content hidden">
<div class="section"><h2>Как подключить аккаунт</h2><p style="color:var(--muted);margin-top:10px">Нажмите "Подключить" на вкладке Обзор. Введите номер и код.</p></div>
<div class="section"><h2>Формат целей</h2><p style="color:var(--muted);margin-top:10px">Публичные: <code>@username</code>. Закрытые: <code>-100123...</code> (аккаунт должен быть в чате).</p></div>
<div class="section"><h2>Поддержка</h2><p style="color:var(--muted);margin-top:10px">Владелец: <a href="https://t.me/apemiru" style="color:var(--accent)">@apemiru</a></p></div>
</div>

<!-- ВКЛАДКА: АДМИНКА -->
<div id="tab-admin" class="tab-content hidden admin-only">
<div class="section">
<h2>Пользователи</h2>
<table><thead><tr><th>ID</th><th>Роль</th><th>Баланс</th><th>Подписка</th><th>Действия</th></tr></thead>
<tbody id="ui-users"></tbody></table>
</div>
</div>

<!-- МОДАЛКИ -->
<div class="modal" id="m-acc">
<div class="modal-content">
<h2>Подключение</h2>
<div id="acc-1" style="margin-top:20px">
<input type="text" id="phone" placeholder="+79990000000" style="margin-bottom:15px">
<button class="btn grad" style="width:100%" onclick="sendCode()">Получить код</button>
</div>
<div id="acc-2" class="hidden" style="margin-top:20px">
<input type="text" id="code" placeholder="Код из телеграма" style="margin-bottom:15px">
<button class="btn grad" style="width:100%" onclick="verifyCode()">Войти</button>
</div>
<div id="acc-3" class="hidden" style="margin-top:20px">
<input type="password" id="pass" placeholder="Пароль 2FA" style="margin-bottom:15px">
<button class="btn grad" style="width:100%" onclick="verify2FA()">Войти</button>
</div>
<button class="btn outline" style="width:100%;margin-top:10px" onclick="document.getElementById('m-acc').style.display='none'">Закрыть</button>
</div>
</div>

</main>
</div>
<script>
let csrf='';
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...opts.headers}});if(r.status===401){location.href='/';return null}const t=await r.text();try{const d=JSON.parse(t);if(!r.ok)throw new Error(d.error||r.status);return d}catch(e){alert(t);throw e}}
function showTab(t){document.querySelectorAll('.tab-content').forEach(x=>x.classList.add('hidden'));document.querySelectorAll('.nav a').forEach(x=>x.classList.remove('active'));const target=document.getElementById('tab-'+t);if(target)target.classList.remove('hidden');event.target.classList.add('active')}
function closeM(id){document.getElementById(id).style.display='none'}

async function load(){
  try {
    const p=await api('/api/me'); if(!p)return; csrf=p.csrf;
    document.querySelectorAll('.admin-only').forEach(x=>x.classList.toggle('hidden',p.role!=='ADMIN'));
    document.getElementById('ui-sub').textContent=p.sub_active?'АКТИВНА':'НЕТ';
    document.getElementById('ui-bal').textContent=p.balance+' ₽';
    document.getElementById('ui-sent').textContent=p.stats.sent;
    document.getElementById('ui-acc').textContent=p.has_session?'ВКЛ':'ВЫКЛ';
    document.getElementById('ui-templates').value=p.templates.join('\n');
    document.getElementById('ui-targets').value=p.targets.join('\n');
    document.getElementById('ui-delay').value=p.delay;
    if(p.role==='ADMIN'){
      const u=await api('/api/admin/users');
      document.getElementById('ui-users').innerHTML=u.users.map(x=>`<tr><td>${x.user_id}</td><td>${x.role}</td><td>${x.balance}₽</td><td>${x.subscription_until||'—'}</td><td><button class="btn outline" style="padding:5px 10px;font-size:11px" onclick="addBal(${x.user_id})">+Бал</button><button class="btn outline" style="padding:5px 10px;font-size:11px" onclick="addSub(${x.user_id})">+30д</button></td></tr>`).join('');
    }
  } catch(e){console.error(e)}
}
async function saveTemplates(){await api('/api/templates',{method:'POST',body:JSON.stringify({text:document.getElementById('ui-templates').value})});alert('Сохранено')}
async function saveTargets(){await api('/api/targets',{method:'POST',body:JSON.stringify({text:document.getElementById('ui-targets').value})});alert('Сохранено')}
async function saveDelay(){await api('/api/delay',{method:'POST',body:JSON.stringify({delay:parseFloat(document.getElementById('ui-delay').value)})});alert('Сохранено')}
async function jobAction(a){await api('/api/job/'+a,{method:'POST'});alert('Ок');load()}
async function addBal(id){const a=prompt("Сумма:");if(!a)return;await api('/api/admin/add_bal',{method:'POST',body:JSON.stringify({user_id:id,amount:parseInt(a)})});load()}
async function addSub(id){if(!confirm('Выдать 30 дней?'))return;await api('/api/admin/add_sub',{method:'POST',body:JSON.stringify({user_id:id})});load()}
function openAccModal(){document.getElementById('m-acc').style.display='grid'}
async function sendCode(){try{await api('/api/auth/send',{method:'POST',body:JSON.stringify({phone:document.getElementById('phone').value})});document.getElementById('acc-1').classList.add('hidden');document.getElementById('acc-2').classList.remove('hidden')}catch(e){alert(e.message)}}
async function verifyCode(){try{const r=await api('/api/auth/verify',{method:'POST',body:JSON.stringify({code:document.getElementById('code').value})});if(r.need_2fa){document.getElementById('acc-2').classList.add('hidden');document.getElementById('acc-3').classList.remove('hidden')}else{closeM('m-acc');load()}}catch(e){alert(e.message)}}
async function verify2FA(){try{await api('/api/auth/2fa',{method:'POST',body:JSON.stringify({pass:document.getElementById('pass').value})});closeM('m-acc');load()}catch(e){alert(e.message)}}
async function showPayment(){const r=await api('/api/pay');alert('Реквизиты:\n'+r.details)}
load();setInterval(load,15000);
</script></body></html>
"""

# -------------------- ROUTES --------------------

@app.get("/")
def index(): return render_template_string(LOGIN_HTML, message="Ссылка недействительна или истекла.")

@app.get("/login")
def login():
    raw = request.args.get("token", "")
    uid = run_async(db.consume_login_token(raw))
    if not uid: return redirect("/")
    if not (await db.get_user(uid))["approved"] and not is_admin(uid): return redirect("/")
    sid = run_async(db.create_web_session(uid))
    resp = redirect("/dashboard")
    resp.set_cookie("titan_sid", sid, max_age=SESSION_DAYS*86400, secure=False, httponly=True)
    return resp

@app.get("/dashboard")
def dashboard():
    if not load_user(): return redirect("/")
    return render_template_string(DASHBOARD_HTML)

@app.get("/logout")
def logout():
    raw = request.cookies.get("titan_sid")
    if raw: run_async(db.delete_web_session(raw))
    resp = redirect("/")
    resp.delete_cookie("titan_sid")
    return resp

# --- API ---

@app.get("/api/me")
@login_required
def api_me():
    u = request.titan_user
    uid = u["user_id"]
    session["csrf"] = session.get("csrf") or secrets.token_urlsafe(24)
    return jsonify({
        "user_id": uid, "role": u["role"], "balance": u["balance"],
        "sub_active": run_async(db.sub_active(uid)),
        "has_session": bool(u["session_string"]),
        "templates": json.loads(u["templates"] or "[]"),
        "targets": json.loads(u["targets"] or "[]"),
        "delay": u["delay"], "stats": run_async(db.stats(uid)),
        "csrf": session["csrf"]
    })

@app.post("/api/templates")
@login_required
@csrf_required
def api_tpl():
    vals = list(dict.fromkeys(x.strip() for x in request.json.get("text","").splitlines() if x.strip()))
    run_async(db.update_user(request.titan_user["user_id"], templates=vals))
    return jsonify({"ok":True})

@app.post("/api/targets")
@login_required
@csrf_required
def api_tgt():
    vals = parse_targets(request.json.get("text",""))
    run_async(db.update_user(request.titan_user["user_id"], targets=vals))
    return jsonify({"ok":True})

@app.post("/api/delay")
@login_required
@csrf_required
def api_delay():
    d = float(request.json.get("delay", 10))
    if d < 0: d = 0
    run_async(db.update_user(request.titan_user["user_id"], delay=int(d*10)))
    return jsonify({"ok":True})

@app.post("/api/job/<action>")
@login_required
@csrf_required
def api_job(action):
    uid = request.titan_user["user_id"]
    if action == "start":
        if not request.titan_user["role"]=="ADMIN" and not run_async(db.sub_active(uid)):
            return jsonify({"error":"Подписка истекла"}), 403
        if not request.titan_user["session_string"]: return jsonify({"error":"Нет аккаунта"}), 400
        if uid in workers and not workers[uid].done(): return jsonify({"error":"Уже запущено"}), 409
        stop = asyncio.Event()
        worker_stops[uid] = stop
        workers[uid] = asyncio.run_coroutine_threadsafe(worker(uid, stop), loop)
    else:
        if uid in worker_stops: loop.call_soon_threadsafe(worker_stops[uid].set)
    return jsonify({"ok":True})

@app.get("/api/pay")
@login_required
def api_pay(): return jsonify({"details": run_async(db.get_setting("payment_details", "Не заданы"))})

# --- AUTH ---
@app.post("/api/auth/send")
@login_required
@csrf_required
def api_auth_send():
    uid = request.titan_user["user_id"]
    phone = request.json.get("phone")
    if not re.match(r"^\+\d{10,15}$", phone): return jsonify({"error":"Формат +7999..."}), 400
    async def _s():
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        s = await c.send_code_request(phone)
        web_flows[uid] = {"c": c, "phone": phone, "hash": s.phone_code_hash}
        return True
    try: asyncio.run_coroutine_threadsafe(_s(), loop).result(20); return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)[:50]}), 400

@app.post("/api/auth/verify")
@login_required
@csrf_required
def api_auth_verify():
    uid = request.titan_user["user_id"]
    code = request.json.get("code")
    f = web_flows.get(uid)
    if not f: return jsonify({"error":"Истекло"}), 400
    async def _v():
        try:
            await f["c"].sign_in(phone=f["phone"], code=code, phone_code_hash=f["hash"])
            await db.update_user(uid, session_string=f["c"].session.save())
            await f["c"].disconnect()
            return {"ok":True, "need_2fa":False}
        except SessionPasswordNeededError: return {"need_2fa":True}
        except Exception as e: return {"error":str(e)[:50]}
    res = asyncio.run_coroutine_threadsafe(_v(), loop).result(20)
    if not res.get("need_2fa"): web_flows.pop(uid,None)
    return jsonify(res)

@app.post("/api/auth/2fa")
@login_required
@csrf_required
def api_auth_2fa():
    uid = request.titan_user["user_id"]
    f = web_flows.get(uid)
    if not f: return jsonify({"error":"Истекло"}), 400
    async def _v():
        try:
            await f["c"].sign_in(password=request.json.get("pass"))
            await db.update_user(uid, session_string=f["c"].session.save())
            await f["c"].disconnect()
            return {"ok":True}
        except Exception as e: return {"error":str(e)[:50]}
    res = asyncio.run_coroutine_threadsafe(_v(), loop).result(20)
    web_flows.pop(uid,None)
    return jsonify(res)

# --- ADMIN ---
@app.get("/api/admin/users")
@admin_required
def api_admin_users(): return jsonify({"users": [dict(x) for x in run_async(db.all_users())]})

@app.post("/api/admin/add_bal")
@admin_required
@csrf_required
def api_admin_bal():
    u = request.titan_user
    data = request.json
    async def _b():
        target = await db.get_user(data["user_id"])
        await db.update_user(data["user_id"], balance=target["balance"]+data["amount"])
    asyncio.run_coroutine_threadsafe(_b(), loop).result(10)
    return jsonify({"ok":True})

@app.post("/api/admin/add_sub")
@admin_required
@csrf_required
def api_admin_sub():
    run_async(db.grant_sub(request.json["user_id"], 30))
    return jsonify({"ok":True})

# -------------------- WORKER (ТОЧНЫЙ ТАЙМИНГ + ОБХОД ФЛУДА) --------------------

async def worker(uid, stop):
    idx = 0
    try:
        while not stop.is_set():
            u = await db.get_user(uid)
            if not u: break
            templates = json.loads(u["templates"] or "[]")
            targets = json.loads(u["targets"] or "[]")
            if not templates or not targets: break
            
            # Задержка в секундах (0 = экстрим)
            delay = max(0, int(u["delay"])) / 10.0
            
            tasks = []
            for t in targets:
                if stop.is_set(): break
                # Берем шаблон по очереди (без повторов)
                msg = templates[idx % len(templates)]
                idx += 1
                tasks.append(asyncio.create_task(send_task(uid, t, msg, delay, stop)))
            
            await asyncio.gather(*tasks, return_exceptions=True)
            
            # Если все цели обработаны, ждем задержку перед новым кругом
            if delay > 0:
                try: await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError: pass
            else:
                await asyncio.sleep(0.1)
                
    except Exception as e: log.error(f"Worker: {e}")
    finally:
        workers.pop(uid, None)

async def send_task(uid, target, msg, delay, stop):
    try:
        async with client_manager.acquire(uid) as c:
            entity = target
            if isinstance(target, int):
                try: entity = await c.get_entity(target)
                except: pass
            try:
                await c.send_message(entity, msg)
                await db.increment_stat(uid, "sent")
            except FloodWaitError as e:
                # ОБХОД ФЛУДА: ждем и продолжаем
                log.warning(f"FloodWait {e.seconds}s for {target}")
                await asyncio.sleep(e.seconds + 2)
            except SlowModeWaitError as e:
                await asyncio.sleep(e.seconds + 2)
            except Exception as e:
                await db.increment_stat(uid, "errors")
                log.warning(f"Err {target}: {e}")
    except Exception: pass
    if delay > 0:
        try: await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError: pass

# -------------------- START --------------------

def web_thread():
    serve(app, host=WEB_HOST, port=WEB_PORT, threads=4)

async def main():
    global db, loop, client_manager, bot
    loop = asyncio.get_running_loop()
    db = Database(DB_PATH)
    await db.initialize()
    client_manager = ClientManager(db, MAX_ACTIVE_CLIENTS)
    
    threading.Thread(target=web_thread, daemon=True).start()
    await setup_bot()
    await bot.run_until_disconnected()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
