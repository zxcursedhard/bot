# TITAN Enterprise — compact Flask + Telethon + SQLite (FULL FIXED v8.7)
# Install: pip install telethon flask waitress aiosqlite psutil

import os
import re
import json
import time
import hmac
import csv
import io
import asyncio
import secrets
import hashlib
import logging
import threading
import datetime as dt
from contextlib import asynccontextmanager
from functools import wraps

import aiosqlite
from flask import (
    Flask, request, jsonify, redirect, url_for, session,
    render_template_string, Response
)
from waitress import serve
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (SessionPasswordNeededError, FloodWaitError, PhoneCodeInvalidError, PasswordHashInvalidError, ChatWriteForbiddenError)

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
MAX_ACTIVE_CLIENTS = max(1, min(50, int(os.getenv("MAX_ACTIVE_CLIENTS", "10")))) # Больше клиентов для скорости
LOGIN_TOKEN_TTL = 24 * 60 * 60
SESSION_DAYS = 14
MAX_TARGETS = 9999 # Безлимит целей
MAX_TEMPLATES = 9999 # Безлимит шаблонов

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
bot_states = {}
phone_flows = {}

def utcnow(): return dt.datetime.now(dt.timezone.utc)
def iso_now(): return utcnow().isoformat()
def is_admin(uid): return int(uid) in SUPER_ADMINS
def token_hash(value): return hashlib.sha256(value.encode("utf-8")).hexdigest()

def parse_targets(raw):
    result = []
    for line in raw.splitlines():
        item = line.strip()
        if not item: continue
        # Поддержка ссылок t.me, tg://chat?id=, и обычных ID
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
            if len(parts) >= 2 and parts[1].isdigit():
                target = int("-100" + parts[1])
            else: continue
        elif item.startswith("@"): target = item
        elif re.fullmatch(r"[A-Za-z0-9_]{5,}", item): target = "@" + item
        elif re.fullmatch(r"-?\d+", item): target = int(item)
        else: continue
        
        if target not in result: result.append(target)
    return result[:MAX_TARGETS]

# -------------------- DATABASE --------------------

class Database:
    def __init__(self, path):
        self.path = path

    def connect(self):
        return aiosqlite.connect(self.path, timeout=15)

    async def initialize(self):
        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=15000")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'USER', approved INTEGER NOT NULL DEFAULT 0,
                    session_string TEXT NOT NULL DEFAULT '', subscription_until TEXT NOT NULL DEFAULT '',
                    templates TEXT NOT NULL DEFAULT '[]', targets TEXT NOT NULL DEFAULT '[]', 
                    delay INTEGER NOT NULL DEFAULT 10, created_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS login_tokens(
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL, created_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS web_sessions(
                    sid_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL, created_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS runtime_jobs(
                    user_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'stopped', updated_at TEXT NOT NULL
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

    async def ensure_user(self, uid, username=""):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO users(user_id, username, created_at) VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET username=CASE WHEN excluded.username!='' THEN excluded.username ELSE users.username END
            """, (int(uid), username or "", iso_now()))
            await db.execute("INSERT OR IGNORE INTO user_stats(user_id) VALUES(?)", (int(uid),))
            await db.commit()

    async def get_user(self, uid):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE user_id=?", (int(uid),))
            return await cur.fetchone()

    async def all_users(self):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT u.user_id,u.username,u.role,u.approved,u.subscription_until,u.delay,
                       CASE WHEN u.session_string!='' THEN 1 ELSE 0 END AS has_session,
                       s.sent,s.errors,COALESCE(j.status,'stopped') AS job_status
                FROM users u LEFT JOIN user_stats s ON s.user_id=u.user_id
                LEFT JOIN runtime_jobs j ON j.user_id=u.user_id
                ORDER BY u.created_at DESC LIMIT 1000
            """)
            return await cur.fetchall()

    async def create_login_token(self, uid):
        raw = secrets.token_urlsafe(32)
        async with self.connect() as db:
            await db.execute("DELETE FROM login_tokens WHERE expires_at<?", (time.time(),))
            await db.execute("INSERT INTO login_tokens(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (token_hash(raw), int(uid), time.time() + LOGIN_TOKEN_TTL, iso_now()))
            await db.commit()
        return raw

    async def consume_login_token(self, raw):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT user_id, expires_at FROM login_tokens WHERE token_hash=?", (token_hash(raw),)) as cur:
                row = await cur.fetchone()
            if not row: return None
            if time.time() > row["expires_at"]: return None
            return int(row["user_id"])

    async def create_web_session(self, uid):
        raw = secrets.token_urlsafe(40)
        async with self.connect() as db:
            await db.execute("DELETE FROM web_sessions WHERE expires_at<?", (time.time(),))
            await db.execute("INSERT INTO web_sessions(sid_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (token_hash(raw), int(uid), time.time() + SESSION_DAYS * 86400, iso_now()))
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

    async def update_user_lists(self, uid, templates=None, targets=None, delay=None):
        async with self.connect() as db:
            if templates is not None:
                await db.execute("UPDATE users SET templates=? WHERE user_id=?", (json.dumps(templates, ensure_ascii=False), int(uid)))
            if targets is not None:
                await db.execute("UPDATE users SET targets=? WHERE user_id=?", (json.dumps(targets, ensure_ascii=False), int(uid)))
            if delay is not None:
                await db.execute("UPDATE users SET delay=? WHERE user_id=?", (int(delay), int(uid)))
            await db.commit()

    async def set_session(self, uid, value):
        async with self.connect() as db:
            await db.execute("UPDATE users SET session_string=? WHERE user_id=?", (value, int(uid)))
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

    async def subscription_active(self, uid):
        row = await self.get_user(uid)
        if not row or not row["subscription_until"]: return False
        try: return dt.datetime.fromisoformat(row["subscription_until"]) > utcnow()
        except ValueError: return False

    async def grant_subscription(self, uid, days=30):
        await self.ensure_user(uid)
        row = await self.get_user(uid)
        start = utcnow()
        if row and row["subscription_until"]:
            try:
                previous = dt.datetime.fromisoformat(row["subscription_until"])
                if previous > start: start = previous
            except ValueError: pass
        expiry = (start + dt.timedelta(days=max(1, min(3650, int(days))))).isoformat()
        async with self.connect() as db:
            await db.execute("UPDATE users SET approved=1,subscription_until=? WHERE user_id=?", (expiry, int(uid)))
            await db.commit()
        return expiry

    async def set_approved(self, uid, enabled):
        await self.ensure_user(uid)
        async with self.connect() as db:
            await db.execute("UPDATE users SET approved=? WHERE user_id=?", (int(enabled), int(uid)))
            await db.commit()

    async def admin_stats(self):
        async with self.connect() as db:
            cur = await db.execute("SELECT COUNT(*), SUM(CASE WHEN session_string!='' THEN 1 ELSE 0 END) FROM users")
            users, sessions = await cur.fetchone()
            cur = await db.execute("SELECT COALESCE(SUM(sent),0),COALESCE(SUM(errors),0) FROM user_stats")
            sent, errors = await cur.fetchone()
            cur = await db.execute("SELECT COUNT(*) FROM runtime_jobs WHERE status='running'")
            running = (await cur.fetchone())[0]
            return {"users": users or 0, "saved_sessions": sessions or 0, "sent": sent or 0, "errors": errors or 0, "running_jobs": running}

    async def recent_audit(self, limit=100):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT user_id,action,details,created_at FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(300, int(limit))),))
            return [dict(x) for x in await cur.fetchall()]

    async def set_job_status(self, uid, status):
        async with self.connect() as db:
            await db.execute("""
                INSERT INTO runtime_jobs(user_id,status,updated_at) VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at
            """, (int(uid), status, iso_now()))
            await db.commit()

# -------------------- LIMITED TELETHON CLIENT POOL --------------------

class ClientManager:
    def __init__(self, database, maximum):
        self.database = database
        self.semaphore = asyncio.Semaphore(maximum)
        self.maximum = maximum
        self.active = 0
        self.waiting = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, uid):
        self.waiting += 1
        await self.semaphore.acquire()
        self.waiting -= 1
        client = None
        async with self._lock: self.active += 1
        try:
            row = await self.database.get_user(uid)
            if not row or not row["session_string"]: raise RuntimeError("Telegram-аккаунт не подключён")
            client = TelegramClient(
                StringSession(row["session_string"]), API_ID, API_HASH,
                connection_retries=1, request_retries=1, timeout=15, auto_reconnect=False
            )
            await client.connect()
            if not await client.is_user_authorized(): raise RuntimeError("Сессия Telegram недействительна")
            yield client
        finally:
            if client is not None:
                try: await client.disconnect()
                except Exception: log.warning("Client disconnect failed for user %s", uid)
            async with self._lock: self.active = max(0, self.active - 1)
            self.semaphore.release()

# -------------------- BOT AUTH / ACCOUNT LOGIN --------------------

def bot_menu(uid):
    return [
        [Button.inline("🌐 Войти в панель", b"web_login")],
        [Button.inline("📱 Подключить Telegram-аккаунт", b"connect_account")]
    ]

async def issue_web_login(event):
    uid = int(event.sender_id)
    sender = await event.get_sender()
    await db.ensure_user(uid, getattr(sender, "username", "") or "")
    user = await db.get_user(uid)
    if not user["approved"] and not is_admin(uid):
        await event.respond(f"Доступ ещё не активирован.\nВаш Telegram ID: {uid}\nОбратитесь в поддержку.")
        return
    raw = await db.create_login_token(uid)
    base = PUBLIC_BASE_URL
    if not base:
        await event.respond("Не задан PUBLIC_BASE_URL в переменных Render.")
        return
    link = f"{base}/login?token={raw}"
    await event.respond(f"🔐 Ссылка для входа:\n{link}")
    await db.audit(uid, "WEB_LOGIN_TOKEN_ISSUED")

async def setup_bot():
    global bot
    bot = TelegramClient("titan_bot", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)

    @bot.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def start_handler(event):
        sender = await event.get_sender()
        await db.ensure_user(event.sender_id, getattr(sender, "username", "") or "")
        user = await db.get_user(event.sender_id)
        if not user["approved"] and not is_admin(event.sender_id):
            await event.respond(f"Доступ не активирован.\nВаш ID: {event.sender_id}")
            return
        await event.respond("TITAN Enterprise\nЛичный кабинет и управление уведомлениями.", buttons=bot_menu(event.sender_id))

    @bot.on(events.CallbackQuery)
    async def callback_handler(event):
        uid = int(event.sender_id)
        data = event.data.decode("utf-8", "ignore")
        if data == "web_login":
            await issue_web_login(event)
            await event.answer("Ссылка отправлена")
        elif data == "connect_account":
            bot_states[uid] = "PHONE"
            await event.answer()
            await event.respond("Отправьте номер телефона в формате (+...).")
        else:
            await event.answer()

    @bot.on(events.NewMessage)
    async def account_login_handler(event):
        if not event.is_private: return
        uid = int(event.sender_id)
        state = bot_states.get(uid)
        if state not in ("PHONE", "CODE", "2FA"): return
        value = (event.raw_text or "").strip()
        if not value or value.startswith("/"): return

        if state == "PHONE":
            if not re.fullmatch(r"\+[1-9]\d{6,14}", value):
                await event.respond("Неверный формат.")
                return
            client = TelegramClient(StringSession(), API_ID, API_HASH, connection_retries=1, request_retries=1)
            try:
                await client.connect()
                sent = await client.send_code_request(value)
                phone_flows[uid] = {"client": client, "phone": value, "phone_code_hash": sent.phone_code_hash}
                bot_states[uid] = "CODE"
                await event.respond("Введите код подтверждения.")
            except Exception as exc:
                await client.disconnect()
                bot_states.pop(uid, None)
                await event.respond(f"Ошибка: {str(exc)[:180]}")
            return

        flow = phone_flows.get(uid)
        if not flow:
            bot_states.pop(uid, None)
            await event.respond("Авторизация истекла.")
            return

        client = flow["client"]
        try:
            if state == "CODE":
                await client.sign_in(phone=flow["phone"], code=value, phone_code_hash=flow["phone_code_hash"])
            else:
                await client.sign_in(password=value)
            saved = client.session.save()
            await db.set_session(uid, saved)
            await client.disconnect()
            phone_flows.pop(uid, None)
            bot_states.pop(uid, None)
            await event.respond("Аккаунт подключён. Сессия сохранена.")
        except SessionPasswordNeededError:
            bot_states[uid] = "2FA"
            await event.respond("Введите пароль 2FA.")
        except Exception as exc:
            await client.disconnect()
            phone_flows.pop(uid, None)
            bot_states.pop(uid, None)
            await event.respond(f"Ошибка: {str(exc)[:180]}")
    return bot

# -------------------- FLASK AUTH HELPERS --------------------

def run_async(coro, timeout=25):
    if loop is None: raise RuntimeError("Async loop is not ready")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try: return future.result(timeout=timeout)
    except TimeoutError: return None

def current_user_id():
    raw = request.cookies.get("titan_sid", "")
    return run_async(db.get_web_session(raw)) if raw else None

def load_user():
    uid = current_user_id()
    if not uid: return None
    return run_async(db.get_user(uid))

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = load_user()
        if not user: return jsonify({"error": "unauthorized"}), 401
        if not user["approved"] and user["role"] != "ADMIN": return jsonify({"error": "access_revoked"}), 403
        request.titan_user = user
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if request.titan_user["role"] != "ADMIN":
            if request.path.startswith("/api/"): return jsonify({"error": "forbidden"}), 403
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapper

def csrf_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = session.get("csrf")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not expected or not hmac.compare_digest(expected, supplied): return jsonify({"error": "csrf"}), 400
        return fn(*args, **kwargs)
    return wrapper

# -------------------- WEB UI --------------------

LOGIN_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN Enterprise — Вход</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;min-height:100vh;
display:grid;place-items:center;padding:20px;background:radial-gradient(ellipse at 15% 10%,#54208a55,transparent 38%),
radial-gradient(ellipse at 90% 85%,#c0266d33,transparent 40%),#09090b;color:#f4f1fa;
font:15px Inter,system-ui,Arial}.glass{width:min(440px,100%);padding:34px;border:1px solid #ffffff20;
border-radius:26px;background:#18181bd9;backdrop-filter:blur(14px);box-shadow:0 24px 90px #0009}
.logo{font-weight:900;letter-spacing:2px;font-size:22px}.logo span{color:#d946ef}
h1{font-size:27px;margin:28px 0 8px}.muted{color:#a1a1aa;line-height:1.6}
.pill{display:inline-block;margin-top:20px;padding:9px 13px;border-radius:99px;background:#a855f722;color:#e9a8ff}
</style></head><body><main class="glass"><div class="logo">TITAN <span>ENTERPRISE</span></div>
<h1>Вход по Telegram</h1><p class="muted">{{ message }}</p>
<div class="pill">Авторизация</div></main></body></html>
"""

DASHBOARD_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN Enterprise</title>
<style>
:root{color-scheme:dark;--bg:#09090b;--panel:#18181b;--line:#ffffff15;--muted:#a1a1aa;
--pink:#ec4899;--purple:#a855f7;--green:#4ade80;--red:#fb7185}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 8% 0%,#6b21a833,transparent 36%),
radial-gradient(ellipse at 95% 90%,#be185d22,transparent 38%),var(--bg);color:#f4f4f5;
font:14px Inter,system-ui,Arial;min-height:100vh}.layout{display:flex;min-height:100vh}
aside{width:240px;padding:25px 17px;border-right:1px solid var(--line);background:#111113bb;
backdrop-filter:blur(12px);flex-shrink:0}.brand{font-size:19px;font-weight:900;letter-spacing:1.5px;margin:5px 8px 32px}
.brand b{color:#e879f9}.nav{display:block;padding:12px 13px;color:#c4c4cc;text-decoration:none;
border-radius:12px;margin:5px 0}.nav:hover,.nav.active{background:linear-gradient(100deg,#9333ea33,#ec489922);color:white}
main{padding:30px;width:100%;max-width:1500px;margin:auto;min-width:0}.top{display:flex;align-items:center;
justify-content:space-between;gap:15px;flex-wrap:wrap}h1{font-size:27px;margin:0 0 7px}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:14px;margin:25px 0}
.card{background:linear-gradient(135deg,#27272acc,#18181bd9);border:1px solid var(--line);border-radius:19px;
padding:19px;backdrop-filter:blur(10px);box-shadow:0 12px 40px #0003}.metric{font-size:28px;font-weight:850;
margin-top:12px;background:linear-gradient(90deg,#e879f9,#f472b6);color:transparent;background-clip:text}
.section{margin:25px 0}.section h2{font-size:18px}.row{display:flex;gap:10px;flex-wrap:wrap}
input,textarea{width:100%;background:#09090b99;border:1px solid #ffffff20;color:#fff;padding:12px 13px;
border-radius:11px;outline:none;font:inherit}textarea{min-height:110px;resize:vertical}.field{flex:1;min-width:220px}
button{border:0;border-radius:11px;padding:11px 15px;color:white;font-weight:700;cursor:pointer;
background:linear-gradient(100deg,#9333ea,#db2777);box-shadow:0 6px 20px #a21caf30}
button.secondary{background:#ffffff12;border:1px solid var(--line);box-shadow:none}
button.danger{background:linear-gradient(100deg,#be123c,#e11d48)}
table{width:100%;border-collapse:collapse;min-width:650px}th,td{text-align:left;padding:13px 12px;
border-bottom:1px solid var(--line)}th{color:#a1a1aa;font-weight:600}.tablewrap{overflow:auto;border:1px solid var(--line);
border-radius:16px;background:#18181bd9}.avatar{display:inline-grid;place-items:center;width:30px;height:30px;
border-radius:50%;background:linear-gradient(135deg,#9333ea,#ec4899);font-weight:800;margin-right:8px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px}
.dot.off{background:var(--red)}.hidden{display:none!important}.notice{margin-top:12px;color:#f0abfc;min-height:20px}
.actions{display:flex;gap:7px;flex-wrap:wrap}.status{color:var(--green)}.status.off{color:var(--red)}
@media(max-width:760px){.layout{display:block}aside{width:100%;padding:13px;border-right:0;border-bottom:1px solid var(--line)}
.brand{margin:5px 8px 10px}.nav{display:inline-block;padding:9px 10px;margin:2px;font-size:13px}
main{padding:19px 14px}h1{font-size:23px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
.card{padding:14px}.metric{font-size:24px}.field{min-width:100%}}
</style></head><body><div class="layout"><aside>
<div class="brand">TITAN <b>ENTERPRISE</b></div>
<a class="nav active" href="#overview">Обзор</a>
<a class="nav" href="#settings">Мои настройки</a>
<a class="nav admin-only hidden" href="#users">Пользователи</a>
<a class="nav admin-only hidden" href="#audit">Журнал</a>
<a class="nav" href="/logout">Выйти</a>
</aside><main><div class="top"><div><h1 id="heading">Личный кабинет</h1>
<div class="muted" id="identity">Загрузка профиля…</div></div><button class="secondary" onclick="load()">↻ Обновить</button></div>
<div id="overview" class="grid">
<div class="card">Подписка<div class="metric" id="subscription">—</div><div class="muted" id="expiry"></div></div>
<div class="card">Отправлено<div class="metric" id="sent">—</div></div>
<div class="card">Ошибки<div class="metric" id="errors">—</div></div>
<div class="card">Процесс<div class="metric" id="job">—</div><div class="muted" id="queue"></div></div>
<div class="card">Telegram-аккаунт<div class="metric" id="account">—</div><div class="muted">Статус привязки</div></div>
</div>
<div class="section" id="settings"><h2>Мои настройки</h2><div class="grid">
<div class="card field"><b>Шаблоны уведомлений</b><p class="muted">Каждая строка — отдельный шаблон. Только для разрешённых получателей.</p>
<textarea id="templates" placeholder="Текст уведомления"></textarea><br><br><button onclick="saveTemplates()">Сохранить шаблоны</button></div>
<div class="card field"><b>Цели уведомлений</b><p class="muted">Указывайте только чаты, где у аккаунта есть право публикации.</p>
<textarea id="targets" placeholder="@company_channel&#10;-100123456789"></textarea><br><br><button onclick="saveTargets()">Сохранить цели</button></div>
</div>
<div class="card field" style="margin-top:14px">
<b>Задержка (в секундах)</b><p class="muted">Сколько ждать между отправками (от 1 до 3600 сек).</p>
<div class="row"><input type="number" id="delay" min="1" max="3600" style="width:100px; flex:none;"> <button onclick="saveDelay()">Сохранить задержку</button></div>
</div>
<div class="row"><button onclick="jobAction('start')">Запустить</button>
<button class="danger" onclick="jobAction('stop')">Остановить</button></div><div class="notice" id="notice"></div></div>
<div class="section admin-only hidden" id="serverStats"><h2>Состояние сервера</h2>
<div class="grid"><div class="card">RAM<div class="metric" id="ram">—</div></div>
<div class="card">CPU<div class="metric" id="cpu">—</div></div>
<div class="card">Активные клиенты<div class="metric" id="activeClients">—</div></div>
<div class="card">В очереди<div class="metric" id="waitingClients">—</div></div></div></div>
<div class="section admin-only hidden" id="users"><h2>Пользователи</h2><div class="tablewrap"><table>
<thead><tr><th>Пользователь</th><th>Роль</th><th>Подписка</th><th>Отправки</th><th>Сессия</th><th>Процесс</th><th>Действия</th></tr></thead>
<tbody id="userRows"></tbody></table></div></div>
<div class="section admin-only hidden" id="audit"><h2>Журнал активности</h2><div class="tablewrap"><table>
<thead><tr><th>Дата</th><th>ID</th><th>Действие</th><th>Детали</th></tr></thead><tbody id="auditRows"></tbody></table></div></div>
</main></div>
<script>
let csrf='', role='USER';
async function api(path,opts={}){const r=await fetch(path,{...opts,headers:{
'Content-Type':'application/json','X-CSRF-Token':csrf,...(opts.headers||{})}});
if(r.status===401){location.href='/';return null}const d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка запроса');return d}
function msg(s){document.getElementById('notice').textContent=s}
async function load(){try{
const p=await api('/api/me');if(!p)return;csrf=p.csrf;role=p.role;
document.getElementById('identity').textContent=(p.username?'@'+p.username+' · ':'')+'ID '+p.user_id+' · '+p.role;
document.getElementById('heading').textContent=role==='ADMIN'?'Панель администратора':'Личный кабинет';
document.querySelectorAll('.admin-only').forEach(x=>x.classList.toggle('hidden',role!=='ADMIN'));
document.getElementById('subscription').textContent=p.subscription_active?'АКТИВНА':'НЕ АКТИВНА';
document.getElementById('expiry').textContent=p.subscription_until||'Срок не задан';
document.getElementById('sent').textContent=p.stats.sent;
document.getElementById('errors').textContent=p.stats.errors;
document.getElementById('job').textContent=p.job_status;
document.getElementById('account').textContent=p.has_session?'ПОДКЛЮЧЁН':'НЕ ПОДКЛЮЧЁН';
document.getElementById('queue').textContent='Активно: '+p.pool.active+' / '+p.pool.maximum+' · ожидают: '+p.pool.waiting;
document.getElementById('templates').value=p.templates.join('\\n');
document.getElementById('targets').value=p.targets.join('\\n');
document.getElementById('delay').value=p.delay;
if(role==='ADMIN')await loadAdmin();
}catch(e){msg(e.message)}}
async function saveTemplates(){try{await api('/api/templates',{method:'POST',body:JSON.stringify({text:document.getElementById('templates').value})});msg('Шаблоны сохранены')}catch(e){msg(e.message)}}
async function saveTargets(){try{await api('/api/targets',{method:'POST',body:JSON.stringify({text:document.getElementById('targets').value})});msg('Цели сохранены')}catch(e){msg(e.message)}}
async function saveDelay(){try{await api('/api/delay',{method:'POST',body:JSON.stringify({delay:parseInt(document.getElementById('delay').value)})});msg('Задержка сохранена')}catch(e){msg(e.message)}}
async function jobAction(action){try{const r=await api('/api/job/'+action,{method:'POST',body:'{}'});msg(r.message);await load()}catch(e){msg(e.message)}}
async function loadAdmin(){const stats=await api('/api/admin/stats');if(stats){
document.getElementById('ram').textContent=stats.ram_mb==null?'н/д':stats.ram_mb+' MB';
document.getElementById('cpu').textContent=stats.cpu_percent==null?'н/д':stats.cpu_percent+'%';
document.getElementById('activeClients').textContent=stats.active_clients+' / '+stats.client_limit;
document.getElementById('waitingClients').textContent=stats.waiting_clients;
}
const d=await api('/api/admin/users');if(!d)return;
document.getElementById('userRows').innerHTML=d.users.map(u=>`<tr><td><span class="avatar">${esc((u.username||String(u.user_id))[0].toUpperCase())}</span>${esc(u.username||u.user_id)}<br><small>${u.user_id}</small></td><td>${u.role}</td><td>${esc(u.subscription_until||'—')}</td><td>${u.sent} / ${u.errors}</td><td><span class="dot ${u.has_session?'':'off'}"></span>${u.has_session?'Есть':'Нет'}</td><td>${esc(u.job_status)}</td><td><div class="actions"><button onclick="grant(${u.user_id},30)">+30 дней</button><button class="secondary" onclick="revoke(${u.user_id})">Закрыть</button></div></td></tr>`).join('');
const a=await api('/api/admin/audit');document.getElementById('auditRows').innerHTML=a.items.map(x=>`<tr><td>${esc(x.created_at)}</td><td>${x.user_id}</td><td>${esc(x.action)}</td><td>${esc(x.details)}</td></tr>`).join('')}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function grant(id,days){if(!confirm('Выдать подписку на '+days+' дней?'))return;try{await api('/api/admin/grant',{method:'POST',body:JSON.stringify({user_id:id,days})});await load()}catch(e){msg(e.message)}}
async function revoke(id){if(!confirm('Закрыть доступ пользователю '+id+'?'))return;try{await api('/api/admin/revoke',{method:'POST',body:JSON.stringify({user_id:id})});await load()}catch(e){msg(e.message)}}
load();setInterval(load,30000);
</script></body></html>
"""

# -------------------- ROUTES --------------------

@app.get("/")
def login_page():
    return render_template_string(LOGIN_HTML, message="Откройте бота TITAN Enterprise, нажмите «Войти в панель» и перейдите по ссылке из личного чата.")

@app.get("/login")
def consume_login():
    raw = request.args.get("token", "")
    if not raw or len(raw) > 200:
        return render_template_string(LOGIN_HTML, message="Ссылка недействительна."), 400
    uid = run_async(db.consume_login_token(raw))
    if not uid:
        return render_template_string(LOGIN_HTML, message="Ссылка истекла."), 401
    user = run_async(db.get_user(uid))
    if not user or (not user["approved"] and user["role"] != "ADMIN"):
        return render_template_string(LOGIN_HTML, message="Доступ не активирован."), 403
    sid = run_async(db.create_web_session(uid))
    run_async(db.audit(uid, "WEB_LOGIN", "token"))
    response = redirect(url_for("dashboard"))
    response.set_cookie("titan_sid", sid, max_age=SESSION_DAYS * 86400, httponly=True, secure=False, samesite="Lax", path="/")
    return response

@app.get("/dashboard")
def dashboard():
    if not load_user(): return redirect(url_for("login_page"))
    return render_template_string(DASHBOARD_HTML)

@app.get("/admin")
def admin_page():
    user = load_user()
    if not user: return redirect(url_for("login_page"))
    if user["role"] != "ADMIN": return redirect(url_for("dashboard"))
    return render_template_string(DASHBOARD_HTML)

@app.get("/logout")
def logout():
    raw = request.cookies.get("titan_sid", "")
    if raw: run_async(db.delete_web_session(raw))
    response = redirect(url_for("login_page"))
    response.delete_cookie("titan_sid", path="/")
    session.clear()
    return response

@app.get("/api/me")
@login_required
def api_me():
    u = request.titan_user
    uid = int(u["user_id"])
    stats = run_async(db.stats(uid))
    active = run_async(db.subscription_active(uid))
    templates = json.loads(u["templates"] or "[]")
    targets = json.loads(u["targets"] or "[]")
    task = workers.get(uid)
    job_status = "running" if task and not task.done() else "stopped"
    session["csrf"] = session.get("csrf") or secrets.token_urlsafe(24)
    return jsonify({
        "user_id": uid, "username": u["username"], "role": u["role"],
        "subscription_active": active, "subscription_until": u["subscription_until"],
        "stats": stats, "templates": templates, "targets": targets, "delay": u["delay"],
        "has_session": bool(u["session_string"]), "job_status": job_status,
        "pool": {"active": client_manager.active, "waiting": client_manager.waiting, "maximum": client_manager.maximum},
        "csrf": session["csrf"]
    })

@app.post("/api/templates")
@login_required
@csrf_required
def api_templates():
    uid = int(request.titan_user["user_id"])
    raw = (request.get_json(silent=True) or {}).get("text", "")
    if not isinstance(raw, str) or len(raw) > 30000: return jsonify({"error": "invalid_text"}), 400
    values = list(dict.fromkeys(x.strip() for x in raw.splitlines() if x.strip()))
    run_async(db.update_user_lists(uid, templates=values))
    run_async(db.audit(uid, "TEMPLATES_UPDATED", str(len(values))))
    return jsonify({"ok": True, "count": len(values)})

@app.post("/api/targets")
@login_required
@csrf_required
def api_targets():
    uid = int(request.titan_user["user_id"])
    raw = (request.get_json(silent=True) or {}).get("text", "")
    if not isinstance(raw, str) or len(raw) > 10000: return jsonify({"error": "invalid_text"}), 400
    values = parse_targets(raw)
    if not values: return jsonify({"error": "no_valid_targets"}), 400
    run_async(db.update_user_lists(uid, targets=values))
    run_async(db.audit(uid, "TARGETS_UPDATED", str(len(values))))
    return jsonify({"ok": True, "count": len(values)})

@app.post("/api/delay")
@login_required
@csrf_required
def api_delay():
    uid = int(request.titan_user["user_id"])
    delay = int((request.get_json(silent=True) or {}).get("delay", 10))
    if delay < 1 or delay > 3600: return jsonify({"error": "invalid_delay"}), 400
    run_async(db.update_user_lists(uid, delay=delay))
    return jsonify({"ok": True})

@app.post("/api/job/start")
@login_required
@csrf_required
def api_job_start():
    uid = int(request.titan_user["user_id"])
    u = run_async(db.get_user(uid))
    if not is_admin(uid) and not run_async(db.subscription_active(uid)): return jsonify({"error": "subscription_inactive"}), 403
    if not u["session_string"]: return jsonify({"error": "telegram_account_not_connected"}), 400
    templates = json.loads(u["templates"] or "[]")
    targets = json.loads(u["targets"] or "[]")
    if not templates or not targets: return jsonify({"error": "add_templates_and_targets_first"}), 400
    old = workers.get(uid)
    if old and not old.done(): return jsonify({"error": "already_running"}), 409
    stop_event = asyncio.Event()
    worker_stops[uid] = stop_event
    run_async(db.set_job_status(uid, "running"))
    future = asyncio.run_coroutine_threadsafe(notification_worker(uid, stop_event), loop)
    workers[uid] = future
    run_async(db.audit(uid, "JOB_STARTED"))
    return jsonify({"ok": True, "message": "Задача поставлена в очередь клиентов."})

@app.post("/api/job/stop")
@login_required
@csrf_required
def api_job_stop():
    uid = int(request.titan_user["user_id"])
    stop_event = worker_stops.get(uid)
    if stop_event: loop.call_soon_threadsafe(stop_event.set)
    run_async(db.audit(uid, "JOB_STOP_REQUESTED"))
    return jsonify({"ok": True, "message": "Остановка запрошена."})

@app.get("/api/admin/users")
@admin_required
def api_admin_users(): return jsonify({"users": [dict(x) for x in run_async(db.all_users())]})

@app.get("/api/admin/audit")
@admin_required
def api_admin_audit(): return jsonify({"items": run_async(db.recent_audit(150))})

@app.get("/api/admin/stats")
@admin_required
def api_admin_stats():
    result = run_async(db.admin_stats())
    result.update({"active_clients": client_manager.active, "waiting_clients": client_manager.waiting, "client_limit": client_manager.maximum})
    if psutil:
        try:
            result["ram_mb"] = round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
            result["cpu_percent"] = psutil.cpu_percent(interval=None)
        except Exception: result["ram_mb"], result["cpu_percent"] = None, None
    else: result["ram_mb"], result["cpu_percent"] = None, None
    return jsonify(result)

@app.post("/api/admin/grant")
@admin_required
@csrf_required
def api_admin_grant():
    payload = request.get_json(silent=True) or {}
    try: uid, days = int(payload["user_id"]), int(payload.get("days", 30))
    except (KeyError, ValueError, TypeError): return jsonify({"error": "invalid_parameters"}), 400
    expiry = run_async(db.grant_subscription(uid, days))
    run_async(db.audit(request.titan_user["user_id"], "SUBSCRIPTION_GRANTED", f"{uid}:{expiry}"))
    return jsonify({"ok": True, "subscription_until": expiry})

@app.post("/api/admin/revoke")
@admin_required
@csrf_required
def api_admin_revoke():
    payload = request.get_json(silent=True) or {}
    try: uid = int(payload["user_id"])
    except (KeyError, ValueError, TypeError): return jsonify({"error": "invalid_user_id"}), 400
    if uid in SUPER_ADMINS: return jsonify({"error": "cannot_revoke_super_admin"}), 403
    run_async(db.set_approved(uid, False))
    stop_event = worker_stops.get(uid)
    if stop_event: loop.call_soon_threadsafe(stop_event.set)
    run_async(db.audit(request.titan_user["user_id"], "USER_REVOKED", str(uid)))
    return jsonify({"ok": True})

# -------------------- NOTIFICATION WORKER (SMART) --------------------

async def notification_worker(uid, stop_event):
    try:
        while not stop_event.is_set():
            user = await db.get_user(uid)
            if not user or (not user["approved"] and user["role"] != "ADMIN"): break
            if not is_admin(uid) and not await db.subscription_active(uid): break
            
            templates = json.loads(user["templates"] or "[]")
            targets = json.loads(user["targets"] or "[]")
            delay = max(1, int(user["delay"]))
            
            if not templates or not targets or not user["session_string"]: break

            for target in targets:
                if stop_event.is_set(): break
                message = templates[int(time.time()) % len(templates)]
                
                try:
                    async with client_manager.acquire(uid) as client:
                        # Пытаемся получить entity (для закрытых чатов)
                        try:
                            entity = await client.get_entity(target)
                        except Exception:
                            entity = target # Если не вышло, шлем как есть
                            
                        await client.send_message(entity, message)
                        
                    await db.increment_stat(uid, "sent")
                    await db.audit(uid, "NOTIFICATION_SENT", str(target))
                    
                except FloodWaitError as exc:
                    await db.audit(uid, "TELEGRAM_FLOOD_WAIT", f"{exc.seconds}s")
                    await asyncio.sleep(min(max(exc.seconds, 30), 3600))
                    stop_event.set()
                    break
                except ChatWriteForbiddenError:
                    await db.increment_stat(uid, "errors")
                    await db.audit(uid, "FORBIDDEN_MUTE", str(target))
                    await asyncio.sleep(300) # Ждем 5 минут и идем к следующей цели
                except asyncio.CancelledError: raise
                except Exception as exc:
                    await db.increment_stat(uid, "errors")
                    await db.audit(uid, "NOTIFICATION_ERROR", f"{target}: {str(exc)[:150]}")

                try: await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError: pass
                
        await db.set_job_status(uid, "stopped")
    except asyncio.CancelledError:
        await db.set_job_status(uid, "stopped")
        raise
    except Exception:
        log.exception("Worker failed for user %s", uid)
        await db.set_job_status(uid, "error")
    finally:
        worker_stops.pop(uid, None)
        workers.pop(uid, None)

# -------------------- STARTUP --------------------

def run_web():
    serve(app, host=WEB_HOST, port=WEB_PORT, threads=4)

async def main():
    global db, loop, client_manager
    loop = asyncio.get_running_loop()
    db = Database(DB_PATH)
    await db.initialize()
    client_manager = ClientManager(db, MAX_ACTIVE_CLIENTS)

    threading.Thread(target=run_web, daemon=True, name="titan-web").start()
    await setup_bot()
    log.info("TITAN Enterprise started on port %s; client limit=%s", WEB_PORT, MAX_ACTIVE_CLIENTS)
    await bot.run_until_disconnected()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("TITAN Enterprise stopped")
