# TITAN Enterprise v15.1 Omni-Core + Admin Logs
# pip install telethon flask waitress aiosqlite psutil aiohttp

import os
import re
import json
import time
import hmac
import base64
import asyncio
import secrets
import hashlib
import logging
import threading
import datetime as dt
from collections import deque
from contextlib import asynccontextmanager
from functools import wraps

import aiosqlite
import aiohttp
from flask import (
    Flask, request, jsonify, redirect, url_for, session,
    render_template_string
)
from waitress import serve
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, FloodWaitError, PhoneCodeInvalidError,
    PasswordHashInvalidError
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.account import UpdateStatusRequest

try:
    import psutil
except ImportError:
    psutil = None

# =====================================================================
# CONFIG
# =====================================================================

API_ID = int(os.getenv("API_ID", "2040"))
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8732669492:AAGh5vNOpmmzW6qlmlv7WxgiT7UFT2wQgj0")
FLASK_SECRET = os.getenv("FLASK_SECRET", "titan_omnicore_secret_key_2026_001")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "titan_omnicore.db")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("PORT", "10000"))

SUPER_ADMINS = {
    int(x.strip()) for x in os.getenv("SUPER_ADMINS", "8655682794,8773171051").split(",")
    if x.strip().isdigit()
}
MAX_ACTIVE_CLIENTS = max(1, min(50, int(os.getenv("MAX_ACTIVE_CLIENTS", "20"))))
LOGIN_TOKEN_TTL = 24 * 60 * 60
SESSION_DAYS = 14
MAX_TARGETS = 9999
MAX_TEMPLATES = 9999
MAX_LOGS = 500
LOG_TABLE_LIMIT = 5000

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("titan")

app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=bool(PUBLIC_BASE_URL.startswith("https")),
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=dt.timedelta(days=SESSION_DAYS),
    MAX_CONTENT_LENGTH=64 * 1024
)

# =====================================================================
# GLOBAL STATE
# =====================================================================

bot = None
db = None
loop = None
client_manager = None
workers = {}
worker_stops = {}
web_states = {}
web_flows = {}
log_buffer = deque(maxlen=MAX_LOGS)
engine_state = {"mode": "AUTO", "cycles": 0, "last_cycle_at": None}
rate_limit_map = {}
rate_lock = threading.Lock()

# =====================================================================
# HELPERS
# =====================================================================

def utcnow():
    return dt.datetime.now(dt.timezone.utc)

def iso_now():
    return utcnow().isoformat()

def is_admin(uid):
    try:
        return int(uid) in SUPER_ADMINS
    except (TypeError, ValueError):
        return False

def token_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def safe_json_loads(raw, default):
    try:
        val = json.loads(raw) if raw else default
        return val if isinstance(val, (list, dict)) else default
    except (json.JSONDecodeError, TypeError):
        return default

def parse_targets(raw):
    result, seen = [], set()
    for line in raw.splitlines():
        item = line.strip()
        if not item:
            continue
        if item.startswith("tg://chat?id="):
            try:
                target = int(item.split("=")[1])
                if target not in seen:
                    seen.add(target); result.append(target)
                continue
            except (ValueError, IndexError):
                pass
        item = re.sub(r"^https?://t\.me/", "", item).strip("/")
        item = re.sub(r"\?.*$", "", item)
        if item.startswith("c/"):
            parts = item.split("/")
            if len(parts) >= 2 and parts[1].isdigit():
                target = int("-100" + parts[1])
            else:
                continue
        elif item.startswith("@"):
            target = item
        elif re.fullmatch(r"-?\d+", item):
            target = int(item)
        elif re.fullmatch(r"[A-Za-z0-9_]{5,}", item):
            target = "@" + item
        else:
            continue
        if target not in seen:
            seen.add(target); result.append(target)
    return result[:MAX_TARGETS]

def parse_invites(raw):
    result, seen = [], set()
    for line in raw.splitlines():
        m = re.match(r"^(?:https?://)?t\.me/(?:\+|joinchat/)([A-Za-z0-9_\-]+)$", line.strip())
        if m and m.group(1) not in seen:
            seen.add(m.group(1)); result.append(m.group(1))
    return result[:500]

RANDOMIZE_MAP = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y",
    "х": "x", "к": "k", "м": "m", "т": "t", "в": "b", "н": "h",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "У": "Y",
    "Х": "X", "К": "K", "М": "M", "Т": "T", "В": "B", "Н": "H",
}

def randomize_text(text, enabled=True):
    if not enabled or not text:
        return text
    out = []
    at_word_start = True
    last_replaced = False
    for ch in text:
        if ch.isspace():
            out.append(ch)
            at_word_start = True
            last_replaced = False
            continue
        if at_word_start:
            out.append(ch)
            at_word_start = False
            last_replaced = False
            continue
        if not last_replaced and ch in RANDOMIZE_MAP and secrets.randbelow(2) == 0:
            out.append(RANDOMIZE_MAP[ch])
            last_replaced = True
        else:
            out.append(ch)
            last_replaced = False
    return "".join(out)

def check_rate_limit(key, limit=180, window=60):
    now = time.time()
    with rate_lock:
        stamps = rate_limit_map.setdefault(key, [])
        stamps[:] = [t for t in stamps if now - t < window]
        if len(stamps) >= limit:
            return False
        stamps.append(now)
        return True

def tail_log(limit=200):
    return list(log_buffer)[-limit:]

class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            log_buffer.append({
                "ts": dt.datetime.utcnow().isoformat() + "Z",
                "level": record.levelname,
                "msg": record.getMessage()[:600],
            })
        except Exception:
            pass

_bh = _BufferHandler()
_bh.setLevel(logging.INFO)
logging.getLogger().addHandler(_bh)

# =====================================================================
# DATABASE
# =====================================================================

class Database:
    def __init__(self, path):
        self.path = path

    def connect(self):
        return aiosqlite.connect(self.path, timeout=20)

    async def initialize(self):
        async with self.connect() as c:
            await c.execute("PRAGMA journal_mode=WAL")
            await c.execute("PRAGMA synchronous=NORMAL")
            await c.execute("PRAGMA foreign_keys=ON")
            await c.execute("PRAGMA busy_timeout=20000")
            await c.commit()

        async with self.connect() as c:
            await c.execute("""
                CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'USER',
                    approved INTEGER NOT NULL DEFAULT 0,
                    session_string TEXT NOT NULL DEFAULT '',
                    subscription_until TEXT NOT NULL DEFAULT '',
                    templates TEXT NOT NULL DEFAULT '[]',
                    targets TEXT NOT NULL DEFAULT '[]',
                    invite_links TEXT NOT NULL DEFAULT '[]',
                    delay INTEGER NOT NULL DEFAULT 10,
                    mode TEXT NOT NULL DEFAULT 'AUTO',
                    randomize INTEGER NOT NULL DEFAULT 1,
                    warmup INTEGER NOT NULL DEFAULT 0,
                    autosender_enabled INTEGER NOT NULL DEFAULT 0,
                    webhook_url TEXT NOT NULL DEFAULT '',
                    balance INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS accounts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    session_string TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS configs(
                    user_id INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL DEFAULT 'AUTO',
                    delay INTEGER NOT NULL DEFAULT 10,
                    randomize INTEGER NOT NULL DEFAULT 1,
                    warmup INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS stats(
                    user_id INTEGER PRIMARY KEY,
                    sent INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    floodwaits INTEGER NOT NULL DEFAULT 0,
                    parsed INTEGER NOT NULL DEFAULT 0,
                    invites INTEGER NOT NULL DEFAULT 0,
                    auto_replies INTEGER NOT NULL DEFAULT 0
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS tokens(
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS sessions(
                    sid_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            await c.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit(user_id)")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(created_at)")
            await c.execute("""
                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS parsed_members(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat TEXT NOT NULL,
                    member_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, chat, member_id)
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS runtime_jobs(
                    user_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'stopped',
                    updated_at TEXT NOT NULL
                )
            """)
            await c.commit()

        async with self.connect() as c:
            for uid in SUPER_ADMINS:
                await c.execute(
                    "INSERT INTO users(user_id, role, approved, created_at) "
                    "VALUES(?, 'ADMIN', 1, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET role='ADMIN', approved=1",
                    (uid, iso_now())
                )
                await c.execute("INSERT OR IGNORE INTO stats(user_id) VALUES(?)", (uid,))
                await c.execute(
                    "INSERT OR IGNORE INTO configs(user_id, updated_at) VALUES(?, ?)",
                    (uid, iso_now())
                )
            await c.commit()

    async def ensure_user(self, uid, username=""):
        async with self.connect() as c:
            await c.execute(
                "INSERT INTO users(user_id, username, created_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "username=CASE WHEN excluded.username!='' THEN excluded.username ELSE users.username END",
                (int(uid), username or "", iso_now())
            )
            await c.execute("INSERT OR IGNORE INTO stats(user_id) VALUES(?)", (int(uid),))
            await c.execute(
                "INSERT OR IGNORE INTO configs(user_id, updated_at) VALUES(?, ?)",
                (int(uid), iso_now())
            )
            await c.commit()

    async def get_user(self, uid):
        async with self.connect() as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM users WHERE user_id=?", (int(uid),))
            return await cur.fetchone()

    async def get_config(self, uid):
        async with self.connect() as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM configs WHERE user_id=?", (int(uid),))
            row = await cur.fetchone()
            if row:
                return row
        await self.ensure_user(uid)
        async with self.connect() as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("SELECT * FROM configs WHERE user_id=?", (int(uid),))
            return await cur.fetchone()

    async def get_stats(self, uid):
        async with self.connect() as c:
            cur = await c.execute(
                "SELECT sent,errors,floodwaits,parsed,invites,auto_replies "
                "FROM stats WHERE user_id=?", (int(uid),)
            )
            row = await cur.fetchone()
            if row:
                return {
                    "sent": row[0], "errors": row[1], "floodwaits": row[2],
                    "parsed": row[3], "invites": row[4], "auto_replies": row[5],
                }
        return {"sent": 0, "errors": 0, "floodwaits": 0,
                "parsed": 0, "invites": 0, "auto_replies": 0}

    async def inc_stat(self, uid, column, amount=1):
        allowed = {"sent", "errors", "floodwaits", "parsed", "invites", "auto_replies"}
        if column not in allowed:
            return
        async with self.connect() as c:
            await c.execute("INSERT OR IGNORE INTO stats(user_id) VALUES(?)", (int(uid),))
            await c.execute(
                f"UPDATE stats SET {column}={column}+? WHERE user_id=?",
                (int(amount), int(uid))
            )
            await c.commit()

    async def set_config(self, uid, **kwargs):
        allowed = {"mode", "delay", "randomize", "warmup"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        await self.ensure_user(uid)
        async with self.connect() as c:
            sets = ", ".join(f"{k}=?" for k in updates)
            params = list(updates.values()) + [iso_now(), int(uid)]
            await c.execute(f"UPDATE configs SET {sets}, updated_at=? WHERE user_id=?", params)
            await c.commit()

    async def update_user_lists(self, uid, templates=None, targets=None, invite_links=None,
                                delay=None, mode=None, randomize=None, warmup=None,
                                autosender_enabled=None, webhook_url=None):
        await self.ensure_user(uid)
        async with self.connect() as c:
            fields, params = [], []
            if templates is not None:
                fields.append("templates=?"); params.append(json.dumps(templates, ensure_ascii=False))
            if targets is not None:
                fields.append("targets=?"); params.append(json.dumps(targets, ensure_ascii=False))
            if invite_links is not None:
                fields.append("invite_links=?"); params.append(json.dumps(invite_links, ensure_ascii=False))
            if delay is not None:
                fields.append("delay=?"); params.append(int(delay))
            if mode is not None:
                fields.append("mode=?"); params.append(str(mode)[:16])
            if randomize is not None:
                fields.append("randomize=?"); params.append(1 if randomize else 0)
            if warmup is not None:
                fields.append("warmup=?"); params.append(1 if warmup else 0)
            if autosender_enabled is not None:
                fields.append("autosender_enabled=?"); params.append(1 if autosender_enabled else 0)
            if webhook_url is not None:
                fields.append("webhook_url=?"); params.append(str(webhook_url)[:500])
            if not fields:
                return
            params.append(int(uid))
            await c.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", params)
            await c.commit()

    async def set_session(self, uid, session_string, phone="", label=""):
        await self.ensure_user(uid)
        async with self.connect() as c:
            await c.execute("UPDATE users SET session_string=? WHERE user_id=?",
                            (session_string, int(uid)))
            await c.execute(
                "INSERT INTO accounts(user_id, session_string, phone, label, created_at) "
                "VALUES(?,?,?,?,?)",
                (int(uid), session_string, phone, label, iso_now())
            )
            await c.commit()

    async def list_accounts(self, uid):
        async with self.connect() as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute(
                "SELECT id, phone, label, status, created_at FROM accounts "
                "WHERE user_id=? ORDER BY id DESC", (int(uid),)
            )
            return [dict(r) for r in await cur.fetchall()]

    async def remove_account(self, uid, account_id):
        async with self.connect() as c:
            await c.execute("DELETE FROM accounts WHERE id=? AND user_id=?",
                            (int(account_id), int(uid)))
            await c.commit()

    async def create_login_token(self, uid):
        raw = secrets.token_urlsafe(32)
        async with self.connect() as c:
            await c.execute("DELETE FROM tokens WHERE expires_at<?", (time.time(),))
            await c.execute(
                "INSERT INTO tokens(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (token_hash(raw), int(uid), time.time() + LOGIN_TOKEN_TTL, iso_now())
            )
            await c.commit()
        return raw

    async def consume_login_token(self, raw):
        async with self.connect() as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute(
                "SELECT user_id, expires_at FROM tokens WHERE token_hash=?",
                (token_hash(raw),)
            )
            row = await cur.fetchone()
            if not row or time.time() > row["expires_at"]:
                return None
            await c.execute("DELETE FROM tokens WHERE token_hash=?", (token_hash(raw),))
            await c.commit()
            return int(row["user_id"])

    async def create_web_session(self, uid):
        raw = secrets.token_urlsafe(40)
        async with self.connect() as c:
            await c.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
            await c.execute(
                "INSERT INTO sessions(sid_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (token_hash(raw), int(uid), time.time() + SESSION_DAYS * 86400, iso_now())
            )
            await c.commit()
        return raw

    async def get_web_session(self, raw):
        if not raw:
            return None
        async with self.connect() as c:
            cur = await c.execute(
                "SELECT user_id FROM sessions WHERE sid_hash=? AND expires_at>?",
                (token_hash(raw), time.time())
            )
            row = await cur.fetchone()
            return int(row[0]) if row else None

    async def delete_web_session(self, raw):
        if not raw:
            return
        async with self.connect() as c:
            await c.execute("DELETE FROM sessions WHERE sid_hash=?", (token_hash(raw),))
            await c.commit()

    async def audit(self, uid, action, details=""):
        async with self.connect() as c:
            await c.execute(
                "INSERT INTO audit(user_id,action,details,created_at) VALUES(?,?,?,?)",
                (int(uid), str(action)[:80], str(details)[:800], iso_now())
            )
            await c.commit()

    async def recent_audit(self, limit=200, user_id=None):
        async with self.connect() as c:
            c.row_factory = aiosqlite.Row
            if user_id is not None:
                cur = await c.execute(
                    "SELECT user_id,action,details,created_at FROM audit "
                    "WHERE user_id=? ORDER BY id DESC LIMIT ?",
                    (int(user_id), max(1, min(LOG_TABLE_LIMIT, int(limit))))
                )
            else:
                cur = await c.execute(
                    "SELECT user_id,action,details,created_at FROM audit "
                    "ORDER BY id DESC LIMIT ?",
                    (max(1, min(LOG_TABLE_LIMIT, int(limit))),)
                )
            return [dict(r) for r in await cur.fetchall()]

    async def clear_audit_user(self, user_id):
        async with self.connect() as c:
            await c.execute("DELETE FROM audit WHERE user_id=?", (int(user_id),))
            await c.commit()

    async def all_users(self):
        async with self.connect() as c:
            c.row_factory = aiosqlite.Row
            cur = await c.execute("""
                SELECT u.user_id, u.username, u.role, u.approved, u.subscription_until,
                       u.delay, u.balance,
                       CASE WHEN u.session_string!='' THEN 1 ELSE 0 END AS has_session,
                       s.sent, s.errors, s.floodwaits,
                       COALESCE(j.status,'stopped') AS job_status
                FROM users u
                LEFT JOIN stats s ON s.user_id=u.user_id
                LEFT JOIN runtime_jobs j ON j.user_id=u.user_id
                ORDER BY u.created_at DESC LIMIT 2000
            """)
            return [dict(r) for r in await cur.fetchall()]

    async def admin_stats(self):
        async with self.connect() as c:
            cur = await c.execute(
                "SELECT COUNT(*), SUM(CASE WHEN session_string!='' THEN 1 ELSE 0 END) FROM users"
            )
            users, sessions = await cur.fetchone()
            cur = await c.execute(
                "SELECT COALESCE(SUM(sent),0),COALESCE(SUM(errors),0),"
                "COALESCE(SUM(floodwaits),0) FROM stats"
            )
            sent, errors, fw = await cur.fetchone()
            cur = await c.execute("SELECT COUNT(*) FROM runtime_jobs WHERE status='running'")
            running = (await cur.fetchone())[0]
            return {
                "users": users or 0, "saved_sessions": sessions or 0,
                "sent": sent or 0, "errors": errors or 0,
                "floodwaits": fw or 0, "running_jobs": running or 0,
            }

    async def set_job_status(self, uid, status):
        async with self.connect() as c:
            await c.execute(
                "INSERT INTO runtime_jobs(user_id,status,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                (int(uid), status, iso_now())
            )
            await c.commit()

    async def get_setting(self, key, default=""):
        async with self.connect() as c:
            cur = await c.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_setting(self, key, value):
        async with self.connect() as c:
            await c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
            )
            await c.commit()

    async def save_parsed_members(self, uid, chat, members):
        if not members:
            return 0
        added = 0
        async with self.connect() as c:
            for m in members:
                try:
                    cur = await c.execute(
                        "INSERT OR IGNORE INTO parsed_members(user_id,chat,member_id,username,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (int(uid), str(chat), int(m["id"]), str(m.get("username") or ""), iso_now())
                    )
                    if cur.rowcount > 0:
                        added += 1
                except Exception:
                    pass
            await c.commit()
        return added

    async def grant_subscription(self, uid, days=30):
        await self.ensure_user(uid)
        row = await self.get_user(uid)
        start = utcnow()
        if row and row["subscription_until"]:
            try:
                prev = dt.datetime.fromisoformat(row["subscription_until"])
                if prev > start:
                    start = prev
            except ValueError:
                pass
        expiry = (start + dt.timedelta(days=max(1, min(3650, int(days))))).isoformat()
        async with self.connect() as c:
            await c.execute(
                "UPDATE users SET approved=1, subscription_until=? WHERE user_id=?",
                (expiry, int(uid))
            )
            await c.commit()
        return expiry

    async def set_approved(self, uid, enabled):
        await self.ensure_user(uid)
        async with self.connect() as c:
            await c.execute("UPDATE users SET approved=? WHERE user_id=?",
                            (1 if enabled else 0, int(uid)))
            await c.commit()

    async def update_balance(self, uid, amount):
        await self.ensure_user(uid)
        async with self.connect() as c:
            await c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?",
                            (int(amount), int(uid)))
            await c.commit()

# =====================================================================
# CLIENT MANAGER
# =====================================================================

class ClientManager:
    def __init__(self, database, maximum):
        self.database = database
        self.semaphore = asyncio.Semaphore(maximum)
        self.maximum = maximum
        self.active = 0
        self.waiting = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, session_string):
        self.waiting += 1
        await self.semaphore.acquire()
        self.waiting -= 1
        client = None
        async with self._lock:
            self.active += 1
        try:
            if not session_string:
                raise RuntimeError("Session string is empty")
            client = TelegramClient(
                StringSession(session_string), API_ID, API_HASH,
                connection_retries=1, request_retries=1, timeout=15,
                auto_reconnect=False, sequential_updates=True
            )
            await asyncio.wait_for(client.connect(), timeout=20)
            if not await client.is_user_authorized():
                raise RuntimeError("Сессия недействительна")
            yield client
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    log.warning("Client disconnect failed")
            async with self._lock:
                self.active = max(0, self.active - 1)
            self.semaphore.release()

# =====================================================================
# BOT
# =====================================================================

async def issue_web_login(event):
    uid = int(event.sender_id)
    sender = await event.get_sender()
    await db.ensure_user(uid, getattr(sender, "username", "") or "")
    user = await db.get_user(uid)
    if not user["approved"] and not is_admin(uid):
        await event.respond(
            f"🚫 Доступ не активирован.\nВаш ID: {uid}\nОбратитесь в поддержку."
        )
        return
    raw = await db.create_login_token(uid)
    base = PUBLIC_BASE_URL
    if not base:
        await event.respond("⚠️ PUBLIC_BASE_URL не задан в окружении.")
        return
    link = f"{base}/login?token={raw}"
    await event.respond(
        f"🔐 Ссылка для входа:\n{link}\n\nДействительна 24 часа.",
        link_preview=False
    )
    await db.audit(uid, "WEB_LOGIN_TOKEN_ISSUED")

async def setup_bot():
    global bot
    bot = TelegramClient("titan_omnicore_bot", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)

    @bot.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def start_handler(event):
        sender = await event.get_sender()
        await db.ensure_user(event.sender_id, getattr(sender, "username", "") or "")
        user = await db.get_user(event.sender_id)
        if not user["approved"] and not is_admin(event.sender_id):
            await event.respond(
                f"🚫 Доступ не активирован.\nВаш ID: {event.sender_id}"
            )
            return
        await event.respond(
            "⚡ TITAN Enterprise v15.1 Omni-Core\n\nВойдите в панель:",
            buttons=[[Button.inline("🌐 Открыть панель", b"web_login")]]
        )

    @bot.on(events.CallbackQuery)
    async def callback_handler(event):
        try:
            if event.data.decode("utf-8", "ignore") == "web_login":
                await issue_web_login(event)
                await event.answer("Ссылка отправлена")
            else:
                await event.answer()
        except Exception as e:
            log.warning(f"Callback error: {e}")

    return bot

# =====================================================================
# FLASK AUTH
# =====================================================================

def run_async(coro, timeout=25):
    if loop is None:
        raise RuntimeError("Async loop not ready")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        return None

def current_user_id():
    raw = request.cookies.get("titan_sid", "")
    return run_async(db.get_web_session(raw)) if raw else None

def load_user():
    uid = current_user_id()
    return run_async(db.get_user(uid)) if uid else None

def has_active_subscription(user):
    if not user:
        return False
    if user["role"] == "ADMIN":
        return True
    try:
        until = user["subscription_until"]
        if not until:
            return bool(user["approved"])
        return dt.datetime.fromisoformat(until) > utcnow()
    except ValueError:
        return bool(user["approved"])

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = load_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        if not has_active_subscription(user):
            return jsonify({"error": "access_revoked"}), 403
        request.titan_user = user
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if request.titan_user["role"] != "ADMIN" and not is_admin(request.titan_user["user_id"]):
            return jsonify({"error": "forbidden"}), 403
        return fn(*args, **kwargs)
    return wrapper

def csrf_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = session.get("csrf", "")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            return jsonify({"error": "csrf"}), 400
        return fn(*args, **kwargs)
    return wrapper

# =====================================================================
# UI HTML
# =====================================================================

LOGIN_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN Omni-Core — Вход</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Inter:wght@400;600;900&display=swap" rel="stylesheet">
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;
background:radial-gradient(ellipse at 15% 10%,#ff003c22,transparent 42%),
radial-gradient(ellipse at 90% 85%,#00f0ff14,transparent 45%),#050505;
color:#e8f6ff;font:15px Inter,system-ui,Arial;overflow:hidden}
body::before{content:'';position:fixed;inset:0;pointer-events:none;
background:repeating-linear-gradient(0deg,rgba(0,240,255,.03) 0 1px,transparent 1px 3px);z-index:1}
.glass{position:relative;z-index:2;width:min(460px,100%);padding:38px;
border:1px solid #00f0ff33;border-radius:4px;background:#0a0c10dd;backdrop-filter:blur(14px);
box-shadow:0 0 0 1px #ff003c22,0 24px 90px #0009,inset 0 0 40px #00f0ff08}
.logo{font-family:'Space Mono',monospace;font-weight:700;letter-spacing:3px;
font-size:22px;color:#00f0ff;text-shadow:0 0 12px #00f0ff66}
.logo span{color:#ff003c;text-shadow:0 0 12px #ff003c66}
h1{font-family:'Space Mono',monospace;font-size:26px;margin:30px 0 10px}
.muted{color:#7a8899;line-height:1.7;font-size:14px}
.pill{display:inline-block;margin-top:22px;padding:9px 14px;border-radius:3px;
background:#ff003c11;border:1px solid #ff003c44;color:#ff6b86;
font-family:'Space Mono',monospace;font-size:11px;letter-spacing:2px}
.pill::before{content:'▮ ';color:#ff003c;animation:blink 1.2s steps(2) infinite}
@keyframes blink{50%{opacity:0}}
.scan{position:absolute;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,#00f0ff,#ff003c,transparent);
animation:scan 3.2s linear infinite;opacity:.8}
@keyframes scan{0%{top:0}100%{top:100%}}
</style></head><body><main class="glass"><div class="scan"></div>
<div class="logo">TITAN <span>// OMNICORE</span></div>
<h1>Access Gateway</h1><p class="muted">{{ message }}</p>
<div class="pill">AUTHORIZATION REQUIRED</div></main></body></html>
"""

DASHBOARD_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN Omni-Core v15.1</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Inter:wght@400;500;600;900&display=swap" rel="stylesheet">
<style>
:root{color-scheme:dark;--bg:#050505;--panel:#0a0c10;--line:#00f0ff1f;--muted:#7a8899;
--red:#ff003c;--cyan:#00f0ff;--green:#00ff88;--yellow:#ffd500}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(ellipse at 8% 0%,#ff003c11,transparent 38%),
radial-gradient(ellipse at 95% 90%,#00f0ff0e,transparent 40%),var(--bg);
color:#e8f6ff;font:14px Inter,system-ui,Arial;min-height:100vh}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
background:repeating-linear-gradient(0deg,rgba(0,240,255,.02) 0 1px,transparent 1px 3px)}
.layout{display:flex;min-height:100vh;position:relative;z-index:1}
aside{width:250px;padding:26px 16px;border-right:1px solid var(--line);
background:#07090cdd;backdrop-filter:blur(12px);flex-shrink:0;position:relative}
aside::after{content:'';position:absolute;right:-1px;top:0;bottom:0;width:1px;
background:linear-gradient(180deg,transparent,var(--red),var(--cyan),transparent)}
.brand{font-family:'Space Mono',monospace;font-size:16px;font-weight:700;
letter-spacing:2px;margin:4px 8px 30px;color:var(--cyan);text-shadow:0 0 10px #00f0ff55}
.brand b{color:var(--red);text-shadow:0 0 10px #ff003c55}
.nav{display:block;padding:12px 13px;color:#9aa8ba;text-decoration:none;border-radius:3px;
margin:4px 0;cursor:pointer;font-family:'Space Mono',monospace;font-size:12px;
letter-spacing:1px;border-left:2px solid transparent;transition:.15s}
.nav:hover,.nav.active{background:linear-gradient(90deg,#ff003c11,#00f0ff08);
color:#fff;border-left-color:var(--red)}
main{padding:30px;width:100%;max-width:1500px;margin:auto;min-width:0}
.top{display:flex;align-items:center;justify-content:space-between;gap:15px;flex-wrap:wrap}
h1{font-family:'Space Mono',monospace;font-size:24px;margin:0 0 8px;letter-spacing:1px}
.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:25px 0}
.card{background:linear-gradient(135deg,#0d1016dd,#080a0ecc);border:1px solid var(--line);
border-radius:3px;padding:19px;backdrop-filter:blur(10px);box-shadow:0 12px 40px #0004,inset 0 0 24px #00f0ff05;
position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;width:34px;height:1px;background:var(--red)}
.metric{font-family:'Space Mono',monospace;font-size:27px;font-weight:700;margin-top:12px;
color:var(--cyan);text-shadow:0 0 14px #00f0ff44}
.metric.red{color:var(--red);text-shadow:0 0 14px #ff003c44}
.section{margin:25px 0}.section h2{font-family:'Space Mono',monospace;font-size:16px;letter-spacing:1px}
.section h2::before{content:'// ';color:var(--red)}
.row{display:flex;gap:10px;flex-wrap:wrap}
input,textarea,select{width:100%;background:#04060a99;border:1px solid #00f0ff33;color:#e8f6ff;
padding:12px 13px;border-radius:3px;outline:none;font:inherit}
input:focus,textarea:focus,select:focus{border-color:var(--cyan);box-shadow:0 0 0 1px #00f0ff44}
textarea{min-height:110px;resize:vertical;font-family:'Space Mono',monospace;font-size:13px}
.field{flex:1;min-width:220px}
button{border:0;border-radius:3px;padding:11px 16px;color:#050505;font-family:'Space Mono',monospace;
font-weight:700;font-size:12px;letter-spacing:1px;cursor:pointer;
background:linear-gradient(100deg,var(--cyan),#00a8b8);box-shadow:0 0 16px #00f0ff33;transition:.15s}
button:hover{box-shadow:0 0 24px #00f0ff55}
button.secondary{background:#0d1016;color:var(--cyan);border:1px solid #00f0ff44;box-shadow:none}
button.danger{background:linear-gradient(100deg,var(--red),#a80028);color:#fff;box-shadow:0 0 16px #ff003c33}
button.success{background:linear-gradient(100deg,var(--green),#009955);color:#050505}
button.ghost{background:transparent;color:var(--muted);border:1px solid #ffffff18;box-shadow:none}
table{width:100%;border-collapse:collapse;min-width:650px}
th,td{text-align:left;padding:12px;border-bottom:1px solid #ffffff0a;font-size:13px}
th{color:var(--muted);font-weight:600;font-family:'Space Mono',monospace;font-size:11px;letter-spacing:1px}
.tablewrap{overflow:auto;border:1px solid var(--line);border-radius:3px;background:#080a0ecc}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);
margin-right:6px;box-shadow:0 0 8px var(--green)}
.dot.off{background:var(--red);box-shadow:0 0 8px var(--red)}
.dot.warn{background:var(--yellow);box-shadow:0 0 8px var(--yellow)}
.hidden{display:none!important}
.notice{margin-top:12px;color:var(--cyan);min-height:20px;font-family:'Space Mono',monospace;font-size:12px}
.modal{display:none;position:fixed;inset:0;background:#000000cc;backdrop-filter:blur(6px);z-index:100;place-items:center;padding:16px}
.modal-content{background:#080a0e;border:1px solid #00f0ff44;padding:25px;border-radius:3px;width:100%;max-width:420px;
box-shadow:0 0 40px #00f0ff22}
.help-box{background:#080a0e;border:1px solid var(--line);border-left:2px solid var(--cyan);
border-radius:3px;padding:18px;margin-bottom:14px;line-height:1.7}
.help-box h3{color:var(--cyan);margin-top:0;font-family:'Space Mono',monospace;font-size:14px;letter-spacing:1px}
.terminal{background:#020403;border:1px solid #00ff8833;border-radius:3px;padding:16px;
font-family:'Space Mono',monospace;font-size:12px;color:#00ff88;height:520px;overflow-y:auto;
text-shadow:0 0 6px #00ff8844;line-height:1.6}
.terminal .err{color:var(--red);text-shadow:0 0 6px #ff003c44}
.terminal .warn{color:var(--yellow)}
.terminal .ts{color:#3a5040}
.mode-toggle{display:flex;gap:8px;flex-wrap:wrap}
.mode-toggle button{padding:9px 14px;font-size:11px;flex:1;min-width:100px;background:#0d1016;
color:var(--muted);border:1px solid #ffffff18;box-shadow:none}
.mode-toggle button.active{background:linear-gradient(100deg,#ff003c22,#00f0ff11);
color:#fff;border-color:var(--red);box-shadow:0 0 16px #ff003c22}
.badge{display:inline-block;padding:3px 8px;font-family:'Space Mono',monospace;font-size:10px;
border:1px solid #00f0ff44;color:var(--cyan);border-radius:2px;letter-spacing:1px}
.badge.red{border-color:#ff003c55;color:#ff6b86}
.badge.green{border-color:#00ff8855;color:var(--green)}
.logfilter{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:14px}
.logfilter .field{min-width:180px}
.logtable{font-family:'Space Mono',monospace;font-size:12px}
.logtable .lv-ERROR{color:var(--red)}
.logtable .lv-WARNING{color:var(--yellow)}
.logtable .lv-INFO{color:var(--green)}
@media(max-width:760px){.layout{display:block}aside{width:100%;padding:13px;border-right:0;
border-bottom:1px solid var(--line)}.brand{margin:5px 8px 10px}
.nav{display:inline-block;padding:8px 10px;margin:2px;font-size:11px;border-left:0}
main{padding:18px 14px}h1{font-size:20px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
.card{padding:14px}.metric{font-size:22px}.field{min-width:100%}}
</style></head><body><div class="layout"><aside>
<div class="brand">TITAN<b>//OMNICORE</b></div>
<a class="nav active" onclick="showTab('overview')">[01] ОБЗОР</a>
<a class="nav" onclick="showTab('accounts')">[02] АККАУНТЫ</a>
<a class="nav" onclick="showTab('config')">[03] КОНФИГУРАЦИЯ</a>
<a class="nav" onclick="showTab('parser')">[04] ПАРСИНГ</a>
<a class="nav" onclick="showTab('inviter')">[05] ИНВАЙТИНГ</a>
<a class="nav" onclick="showTab('autoresponder')">[06] АВТООТВЕТЧИК</a>
<a class="nav" onclick="showTab('terminal')">[07] ТЕРМИНАЛ</a>
<a class="nav admin-only hidden" onclick="showTab('admin')">[08] АДМИНКА</a>
<a class="nav admin-only hidden" onclick="showTab('adminlogs')">[09] ЛОГИ ЮЗЕРОВ</a>
<a class="nav" href="/logout">[X] ВЫХОД</a>
</aside><main><div class="top"><div><h1 id="heading">CONTROL PANEL</h1>
<div class="muted" id="identity">Загрузка профиля…</div></div>
<button class="secondary" onclick="load()">↻ ОБНОВИТЬ</button></div>

<!-- 01: ОБЗОР -->
<div id="tab-overview" class="tab-content">
<div id="overview" class="grid">
<div class="card">ПОДПИСКА<div class="metric" id="subscription">—</div><div class="muted" id="expiry" style="font-size:12px"></div></div>
<div class="card">БАЛАНС<div class="metric" id="balance">—</div><div class="muted" style="font-size:12px">рублей</div></div>
<div class="card">ОТПРАВЛЕНО<div class="metric" id="sent">—</div></div>
<div class="card">ОШИБКИ<div class="metric red" id="errors">—</div></div>
<div class="card">FLOODWAIT<div class="metric red" id="floodwaits">—</div></div>
<div class="card">ДВИЖОК<div class="metric" id="engineState">—</div><div class="muted" style="font-size:12px" id="engineMode"></div></div>
</div></div>

<!-- 02: АККАУНТЫ -->
<div id="tab-accounts" class="tab-content hidden">
<div class="section"><h2>Подключенные аккаунты</h2>
<div class="row" style="margin-bottom:14px">
<button onclick="openAccModal()">+ ДОБАВИТЬ АККАУНТ</button>
<button class="secondary" onclick="loadAccounts()">↻ ОБНОВИТЬ</button></div>
<div class="tablewrap"><table><thead><tr><th>ID</th><th>Телефон</th><th>Метка</th><th>Статус</th><th>Действие</th></tr></thead>
<tbody id="accountRows"><tr><td colspan="5" class="muted">Загрузка...</td></tr></tbody></table></div></div></div>

<!-- 03: КОНФИГУРАЦИЯ -->
<div id="tab-config" class="tab-content hidden">
<div class="section"><h2>Режим работы</h2>
<div class="mode-toggle">
<button id="modeAuto" onclick="setMode('AUTO')">AUTO // БЕЗОПАСНЫЙ</button>
<button id="modeExtreme" onclick="setMode('EXTREME')">EXTREME // ЭКСТРИМ</button></div>
<p class="muted" style="font-size:12px;margin-top:10px">AUTO: плавная отправка. EXTREME: минимальная задержка, спит ровно по требованию Telegram.</p></div>
<div class="section"><h2>Шаблоны (строго по очереди)</h2>
<textarea id="templates" placeholder="Шаблон #1&#10;Шаблон #2"></textarea>
<div class="row" style="margin-top:10px"><button onclick="saveTemplates()">СОХРАНИТЬ ШАБЛОНЫ</button>
<label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)">
<input type="checkbox" id="randomize" style="width:auto">Рандомизация букв</label></div></div>
<div class="section"><h2>Цели рассылки</h2>
<textarea id="targets" placeholder="@username&#10;-1001234567890"></textarea>
<div class="row" style="margin-top:10px"><button onclick="saveTargets()">СОХРАНИТЬ ЦЕЛИ</button></div></div>
<div class="section"><h2>Параметры двигателя</h2>
<div class="row">
<div class="field"><label class="muted" style="font-size:12px">Задержка (сек)</label>
<input type="number" id="delay" min="0" max="3600" value="10"></div>
<div class="field"><label class="muted" style="font-size:12px">Webhook URL</label>
<input type="text" id="webhook_url" placeholder="https://example.com/webhook"></div></div>
<div class="row" style="margin-top:12px">
<label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)">
<input type="checkbox" id="warmup" style="width:auto">Прогрев</label>
<label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)">
<input type="checkbox" id="autosender_enabled" style="width:auto">Автоответчик</label></div>
<div class="row" style="margin-top:12px"><button onclick="saveConfig()">СОХРАНИТЬ</button></div></div>
<div class="section"><h2>Управление движком</h2>
<div class="row">
<button onclick="jobAction('start')">▶ ЗАПУСТИТЬ</button>
<button class="danger" onclick="jobAction('stop')">■ ОСТАНОВИТЬ</button></div>
<div class="notice" id="notice"></div></div></div>

<!-- 04: ПАРСИНГ -->
<div id="tab-parser" class="tab-content hidden">
<div class="section"><h2>Парсинг участников</h2>
<div class="field"><label class="muted" style="font-size:12px">Чат</label>
<input type="text" id="parseChat" placeholder="@chatname"></div>
<div class="row" style="margin-top:10px">
<div class="field"><label class="muted" style="font-size:12px">Лимит</label>
<input type="number" id="parseLimit" value="500"></div>
<button onclick="runParser()" style="align-self:flex-end">▶ ПАРСИТЬ</button></div>
<div class="notice" id="parseNotice"></div></div></div>

<!-- 05: ИНВАЙТИНГ -->
<div id="tab-inviter" class="tab-content hidden">
<div class="section"><h2>Инвайтинг</h2>
<textarea id="inviteLinks" placeholder="https://t.me/+AbCdEf123"></textarea>
<div class="row" style="margin-top:10px">
<button onclick="saveInvites()">СОХРАНИТЬ</button>
<button onclick="runInviter()">▶ ВСТУПИТЬ</button></div>
<div class="notice" id="inviteNotice"></div></div></div>

<!-- 06: АВТООТВЕТЧИК -->
<div id="tab-autoresponder" class="tab-content hidden">
<div class="section"><h2>Автоответчик</h2>
<div class="card"><span id="autoresponderStatus" class="badge">ВЫКЛ</span>
<div class="muted" style="font-size:12px;margin-top:10px">Отвечает на входящие в ЛС первым шаблоном.</div></div></div></div>

<!-- 07: ТЕРМИНАЛ -->
<div id="tab-terminal" class="tab-content hidden">
<div class="section"><h2>Терминал логов сервера</h2>
<div class="terminal" id="terminal">[ init ] waiting for logs...\n</div>
<div class="row" style="margin-top:12px"><button class="secondary" onclick="clearTerminal()">ОЧИСТИТЬ</button></div></div></div>

<!-- 08: АДМИНКА -->
<div id="tab-admin" class="tab-content hidden admin-only">
<div class="section"><h2>Состояние сервера</h2>
<div class="grid">
<div class="card">RAM<div class="metric" id="ram">—</div></div>
<div class="card">CPU<div class="metric" id="cpu">—</div></div>
<div class="card">АКТИВНЫЕ КЛИЕНТЫ<div class="metric" id="activeClients">—</div></div>
<div class="card">ОЧЕРЕДЬ<div class="metric" id="waitingClients">—</div></div></div></div>
<div class="section"><h2>Пользователи</h2>
<div class="tablewrap"><table>
<thead><tr><th>Пользователь</th><th>Роль</th><th>Доступ</th><th>Подписка</th>
<th>Баланс</th><th>Отправки</th><th>Сессия</th><th>Действия</th></tr></thead>
<tbody id="userRows"><tr><td colspan="8" class="muted">Загрузка...</td></tr></tbody></table></div></div></div>

<!-- 09: ЛОГИ ЮЗЕРОВ (NEW) -->
<div id="tab-adminlogs" class="tab-content hidden admin-only">
<div class="section"><h2>Логи по пользователям</h2>
<div class="logfilter">
<div class="field"><label class="muted" style="font-size:12px">User ID (пусто = все)</label>
<input type="text" id="logUserId" placeholder="8655682794"></div>
<div class="field"><label class="muted" style="font-size:12px">Лимит</label>
<input type="number" id="logLimit" value="200" min="10" max="2000"></div>
<button onclick="loadUserLogs()">↻ ЗАГРУЗИТЬ</button>
<button class="danger" onclick="clearUserLogs()">🗑 ОЧИСТИТЬ ЛОГИ ЮЗЕРА</button></div>
<div class="muted" style="font-size:12px;margin-bottom:10px">
Показываются события: NOTIFY_ERROR, FLOODWAIT, PARSER_RUN, INVITER_RUN, ADMIN_*, WEB_LOGIN и др.
Автообновление каждые 4 сек, пока открыта вкладка.</div>
<div class="tablewrap"><table class="logtable">
<thead><tr><th style="width:160px">Время</th><th style="width:110px">User ID</th>
<th style="width:180px">Действие</th><th>Детали</th></tr></thead>
<tbody id="userLogRows"><tr><td colspan="4" class="muted">Нет данных</td></tr></tbody></table></div></div></div>

<!-- МОДАЛКА АККАУНТА -->
<div class="modal" id="accModal"><div class="modal-content">
<h3 style="margin-top:0;font-family:'Space Mono',monospace">// ПОДКЛЮЧЕНИЕ АККАУНТА</h3>
<div id="accStep1">
<p class="muted" style="font-size:13px">Введите номер телефона (+7999...)</p>
<input type="text" id="phoneInput" placeholder="+79991234567" style="margin-bottom:10px">
<div class="row"><button onclick="sendCode()">ПОЛУЧИТЬ КОД</button>
<button class="secondary" onclick="closeAccModal()">ОТМЕНА</button></div>
<p class="notice" id="accNotice"></p></div>
<div id="accStep2" class="hidden">
<p class="muted" style="font-size:13px">Введите код</p>
<input type="text" id="codeInput" placeholder="12345" style="margin-bottom:10px">
<div class="row"><button onclick="verifyCode()">ВОЙТИ</button>
<button class="secondary" onclick="closeAccModal()">ОТМЕНА</button></div></div>
<div id="accStep3" class="hidden">
<p class="muted" style="font-size:13px">Пароль 2FA</p>
<input type="password" id="passInput" placeholder="Пароль" style="margin-bottom:10px">
<div class="row"><button onclick="verify2FA()">ВОЙТИ</button>
<button class="secondary" onclick="closeAccModal()">ОТМЕНА</button></div></div>
</div></div>

</main></div>
<script>
let csrf='', role='USER', currentTab='overview';
let configDirty = false; // FIX: флаг, что юзер менял чекбоксы вручную
const esc = v => String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function showTab(name){
  currentTab=name;
  document.querySelectorAll('.tab-content').forEach(el=>el.classList.add('hidden'));
  const t=document.getElementById('tab-'+name);
  if(t) t.classList.remove('hidden');
  document.querySelectorAll('aside .nav').forEach(el=>el.classList.remove('active'));
  const clicked=Array.from(document.querySelectorAll('aside .nav'))
    .find(el=>el.getAttribute('onclick')===`showTab('${name}')`);
  if(clicked) clicked.classList.add('active');
  if(name==='terminal') refreshTerminal();
  if(name==='admin') loadAdmin();
  if(name==='adminlogs') loadUserLogs();
  if(name==='accounts') loadAccounts();
}

async function api(path,opts={}){
  const r=await fetch(path,{...opts,headers:{
    'Content-Type':'application/json','X-CSRF-Token':csrf,...(opts.headers||{})}});
  if(r.status===401){location.href='/';return null}
  const d=await r.json();
  if(!r.ok) throw Error(d.error||'Ошибка запроса');
  return d;
}
function msg(s){const el=document.getElementById('notice'); if(el)el.textContent=s;}

async function load(){
  try{
    const p=await api('/api/me'); if(!p) return;
    csrf=p.csrf; role=p.role;
    document.getElementById('identity').textContent=
      (p.username?'@'+p.username+' · ':'')+'ID '+p.user_id+' · '+p.role;
    document.querySelectorAll('.admin-only').forEach(x=>x.classList.toggle('hidden',role!=='ADMIN'));
    document.getElementById('subscription').textContent=p.subscription_active?'АКТИВНА':'НЕТ';
    document.getElementById('expiry').textContent=p.subscription_until||'—';
    document.getElementById('balance').textContent=p.balance+' ₽';
    document.getElementById('sent').textContent=p.stats.sent;
    document.getElementById('errors').textContent=p.stats.errors;
    document.getElementById('floodwaits').textContent=p.stats.floodwaits;
    document.getElementById('engineState').textContent=p.job_status.toUpperCase();
    document.getElementById('engineMode').textContent='MODE: '+p.mode;
    document.getElementById('templates').value=(p.templates||[]).join('\\n');
    document.getElementById('targets').value=(p.targets||[]).join('\\n');
    // FIX: не перезаписываем контролы, если юзер их менял, но ещё не сохранил
    if(!configDirty){
      document.getElementById('delay').value=p.delay;
      document.getElementById('randomize').checked=!!p.randomize;
      document.getElementById('warmup').checked=!!p.warmup;
      document.getElementById('autosender_enabled').checked=!!p.autosender_enabled;
      document.getElementById('webhook_url').value=p.webhook_url||'';
    }
    document.getElementById('autoresponderStatus').textContent=p.autosender_enabled?'ВКЛ':'ВЫКЛ';
    document.getElementById('autoresponderStatus').className='badge '+(p.autosender_enabled?'green':'');
    setModeUI(p.mode);
    if(role==='ADMIN') await loadAdmin();
  }catch(e){console.warn(e.message)}
}

function setModeUI(mode){
  document.getElementById('modeAuto').classList.toggle('active',mode==='AUTO');
  document.getElementById('modeExtreme').classList.toggle('active',mode==='EXTREME');
}

async function saveTemplates(){
  try{await api('/api/templates',{method:'POST',
    body:JSON.stringify({text:document.getElementById('templates').value})});
    msg('Шаблоны сохранены')}catch(e){msg(e.message)}
}
async function saveTargets(){
  try{await api('/api/targets',{method:'POST',
    body:JSON.stringify({text:document.getElementById('targets').value})});
    msg('Цели сохранены')}catch(e){msg(e.message)}
}
async function saveConfig(){
  const randEl = document.getElementById('randomize');
  const wanted = randEl.checked; // что хочет пользователь СЕЙЧАС
  try{
    const res = await api('/api/config',{method:'POST',body:JSON.stringify({
      delay:parseInt(document.getElementById('delay').value)||10,
      randomize:wanted,
      warmup:document.getElementById('warmup').checked,
      autosender_enabled:document.getElementById('autosender_enabled').checked,
      webhook_url:document.getElementById('webhook_url').value||''
    })});
    // FIX: после сохранения принудительно выставляем чекбокс по ответу сервера,
    // чтобы UI не «откатывался» на старое значение при следующем load().
    if (res && typeof res.randomize === 'boolean') {
      randEl.checked = res.randomize;
    } else {
      randEl.checked = wanted; // fallback
    }
    configDirty = false; // FIX
    msg('Конфигурация сохранена');
  }catch(e){ msg(e.message); }
}
async function setMode(mode){
  try{await api('/api/mode',{method:'POST',body:JSON.stringify({mode})});
    setModeUI(mode); msg('Режим: '+mode)}catch(e){msg(e.message)}
}
async function jobAction(action){
  try{const r=await api('/api/job/'+action,{method:'POST',body:'{}'});
    msg(r.message); await load()}catch(e){msg(e.message)}
}

async function loadAccounts(){
  try{const d=await api('/api/accounts'); if(!d) return;
  document.getElementById('accountRows').innerHTML=d.accounts.length?
    d.accounts.map(a=>`<tr><td>${a.id}</td><td>${esc(a.phone||'—')}</td>
      <td>${esc(a.label||'—')}</td><td><span class="dot ${a.status==='active'?'':'off'}"></span>${esc(a.status)}</td>
      <td><button class="danger" onclick="delAccount(${a.id})">УДАЛИТЬ</button></td></tr>`).join('')
    : '<tr><td colspan="5" class="muted">Аккаунтов нет</td></tr>';
  }catch(e){console.warn(e.message)}
}
async function delAccount(id){
  if(!confirm('Удалить #'+id+'?'))return;
  try{await api('/api/accounts/delete',{method:'POST',body:JSON.stringify({id})});
    await loadAccounts()}catch(e){alert(e.message)}
}

function openAccModal(){
  document.getElementById('accModal').style.display='grid';
  document.getElementById('accStep1').classList.remove('hidden');
  document.getElementById('accStep2').classList.add('hidden');
  document.getElementById('accStep3').classList.add('hidden');
  document.getElementById('accNotice').textContent='';
}
function closeAccModal(){document.getElementById('accModal').style.display='none'}
async function sendCode(){
  try{await api('/api/auth/send_code',{method:'POST',
    body:JSON.stringify({phone:document.getElementById('phoneInput').value})});
    document.getElementById('accStep1').classList.add('hidden');
    document.getElementById('accStep2').classList.remove('hidden');
  }catch(e){document.getElementById('accNotice').textContent=e.message}
}
async function verifyCode(){
  try{const r=await api('/api/auth/verify_code',{method:'POST',
    body:JSON.stringify({code:document.getElementById('codeInput').value})});
    if(r.need_2fa){
      document.getElementById('accStep2').classList.add('hidden');
      document.getElementById('accStep3').classList.remove('hidden');
    }else{closeAccModal(); await load(); await loadAccounts();}
  }catch(e){document.getElementById('accNotice').textContent=e.message}
}
async function verify2FA(){
  try{await api('/api/auth/verify_2fa',{method:'POST',
    body:JSON.stringify({pass:document.getElementById('passInput').value})});
    closeAccModal(); await load(); await loadAccounts()
  }catch(e){document.getElementById('accNotice').textContent=e.message}
}

async function runParser(){
  try{const r=await api('/api/parser/run',{method:'POST',body:JSON.stringify({
    chat:document.getElementById('parseChat').value,
    limit:parseInt(document.getElementById('parseLimit').value)||500
  })});
    document.getElementById('parseNotice').textContent='Добавлено: '+(r.added||0)
  }catch(e){document.getElementById('parseNotice').textContent=e.message}
}
async function saveInvites(){
  try{await api('/api/invites/save',{method:'POST',
    body:JSON.stringify({text:document.getElementById('inviteLinks').value})});
    document.getElementById('inviteNotice').textContent='Сохранено'}catch(e){
    document.getElementById('inviteNotice').textContent=e.message}
}
async function runInviter(){
  try{const r=await api('/api/inviter/run',{method:'POST',body:'{}'});
    document.getElementById('inviteNotice').textContent=
      'Вступил: '+(r.ok||0)+', ошибок: '+(r.failed||0)
  }catch(e){document.getElementById('inviteNotice').textContent=e.message}
}

async function loadAdmin(){
  try{
    const s=await api('/api/admin/stats'); if(!s) return;
    document.getElementById('ram').textContent=(s.ram_mb??'—')+' MB';
    document.getElementById('cpu').textContent=(s.cpu_percent??'—')+' %';
    document.getElementById('activeClients').textContent=s.active_clients+' / '+s.client_limit;
    document.getElementById('waitingClients').textContent=s.waiting_clients;
    const u=await api('/api/admin/users');
    if(u) document.getElementById('userRows').innerHTML=u.users.map(x=>{
      const approved = x.approved == 1;
      const btnLabel = approved ? 'ЗАБРАТЬ' : 'ВЫДАТЬ';
      const btnCls = approved ? 'danger' : 'success';
      return `<tr>
        <td>${esc(x.username||x.user_id)}<br><small class="muted">${x.user_id}</small></td>
        <td>${esc(x.role)}</td>
        <td><span class="dot ${approved?'':'off'}"></span>${approved?'ON':'OFF'}</td>
        <td>${esc(x.subscription_until||'—')}</td>
        <td>${x.balance} ₽</td><td>${x.sent}/${x.errors}</td>
        <td>${x.has_session?'✅':'❌'}</td>
        <td><button class="${btnCls}" onclick="toggleApprove(${x.user_id},${approved})">${btnLabel}</button>
        <button onclick="grantSub(${x.user_id})">+ПОДП</button>
        <button onclick="addBal(${x.user_id})">+БАЛ</button>
        <button class="secondary" onclick="jumpToLogs(${x.user_id})">ЛОГИ</button></td>
      </tr>`;
    }).join('');
  }catch(e){console.warn(e.message)}
}
async function toggleApprove(id, currentlyApproved){
  if(!confirm((currentlyApproved?'Забрать доступ у ':'Выдать доступ ')+id+'?')) return;
  try{await api('/api/admin/approve',{method:'POST',
    body:JSON.stringify({user_id:id,enabled:!currentlyApproved})});
    await loadAdmin();
  }catch(e){alert(e.message)}
}
async function grantSub(id){
  const days=parseInt(prompt('Дней подписки:','30')||'0');
  if(!days) return;
  try{await api('/api/admin/grant',{method:'POST',body:JSON.stringify({user_id:id,days})});
    await loadAdmin()}catch(e){alert(e.message)}
}
async function addBal(id){
  const amount=parseInt(prompt('Сумма (₽):','100')||'0');
  if(!amount) return;
  try{await api('/api/admin/balance',{method:'POST',body:JSON.stringify({user_id:id,amount})});
    await loadAdmin()}catch(e){alert(e.message)}
}
function jumpToLogs(id){
  document.getElementById('logUserId').value=id;
  showTab('adminlogs');
}

async function loadUserLogs(){
  if(role!=='ADMIN') return;
  try{
    const uid=document.getElementById('logUserId').value.trim();
    const limit=parseInt(document.getElementById('logLimit').value)||200;
    const qs=new URLSearchParams();
    if(uid) qs.set('user_id',uid);
    qs.set('limit',limit);
    const d=await api('/api/admin/logs?'+qs.toString());
    if(!d) return;
    document.getElementById('userLogRows').innerHTML=d.entries.length?
      d.entries.map(e=>{
        const lv = e.action.includes('ERROR')?'ERROR':
                   e.action.includes('FLOODWAIT')?'WARNING':'INFO';
        return `<tr><td>${esc(e.created_at)}</td>
          <td>${e.user_id}</td>
          <td class="lv-${lv}">${esc(e.action)}</td>
          <td>${esc(e.details)}</td></tr>`;
      }).join('')
      : '<tr><td colspan="4" class="muted">Пусто</td></tr>';
  }catch(e){console.warn('logs:',e.message)}
}
async function clearUserLogs(){
  const uid=document.getElementById('logUserId').value.trim();
  if(!uid){alert('Укажите User ID'); return;}
  if(!confirm('Удалить ВСЕ логи для '+uid+'?')) return;
  try{await api('/api/admin/logs/clear',{method:'POST',
    body:JSON.stringify({user_id:parseInt(uid)})});
    await loadUserLogs();
  }catch(e){alert(e.message)}
}

let terminalTicker=null;
async function refreshTerminal(){
  try{const d=await api('/api/terminal'); if(!d) return;
    const term=document.getElementById('terminal');
    term.innerHTML=d.lines.map(l=>{
      const cls=l.level==='ERROR'?'err':l.level==='WARNING'?'warn':'';
      return `<div class="${cls}"><span class="ts">${esc(l.ts)}</span> ${esc(l.msg)}</div>`;
    }).join('');
    term.scrollTop=term.scrollHeight;
  }catch(e){}
}
function clearTerminal(){document.getElementById('terminal').innerHTML=''}

// FIX: помечаем конфиг грязным при ручном изменении чекбоксов
['randomize','warmup','autosender_enabled'].forEach(id=>{
  const el = document.getElementById(id);
  if (el) el.addEventListener('change', ()=>{ configDirty = true; });
});

setInterval(()=>{if(currentTab==='terminal') refreshTerminal()},2000);
setInterval(()=>{if(currentTab==='adminlogs') loadUserLogs()},4000);
setInterval(load,30000);
load();
</script></body></html>
"""

# =====================================================================
# ROUTES
# =====================================================================

@app.get("/")
def login_page():
    return render_template_string(
        LOGIN_HTML,
        message="Откройте бота TITAN Omni-Core и нажмите «🌐 Открыть панель»."
    )

@app.get("/login")
def consume_login():
    raw = request.args.get("token", "")
    if not raw or len(raw) > 200:
        return render_template_string(LOGIN_HTML, message="Ссылка недействительна."), 400
    uid = run_async(db.consume_login_token(raw))
    if not uid:
        return render_template_string(LOGIN_HTML, message="Ссылка истекла."), 401
    user = run_async(db.get_user(uid))
    if not user or (not has_active_subscription(user)):
        return render_template_string(LOGIN_HTML, message="Доступ не активирован."), 403
    sid = run_async(db.create_web_session(uid))
    run_async(db.audit(uid, "WEB_LOGIN", "token"))
    resp = redirect(url_for("dashboard"))
    resp.set_cookie("titan_sid", sid, max_age=SESSION_DAYS * 86400,
                    httponly=True, secure=bool(PUBLIC_BASE_URL.startswith("https")),
                    samesite="Lax", path="/")
    return resp

@app.get("/dashboard")
def dashboard():
    if not load_user():
        return redirect(url_for("login_page"))
    return render_template_string(DASHBOARD_HTML)

@app.get("/logout")
def logout():
    raw = request.cookies.get("titan_sid", "")
    if raw:
        run_async(db.delete_web_session(raw))
    resp = redirect(url_for("login_page"))
    resp.delete_cookie("titan_sid", path="/")
    session.clear()
    return resp

@app.get("/api/me")
@login_required
def api_me():
    u = request.titan_user
    uid = int(u["user_id"])
    stats = run_async(db.get_stats(uid))
    cfg = run_async(db.get_config(uid))
    active = has_active_subscription(u)
    templates = safe_json_loads(u["templates"], [])
    targets = safe_json_loads(u["targets"], [])
    task = workers.get(uid)
    job_status = "running" if task and not task.done() else "stopped"
    session["csrf"] = session.get("csrf") or secrets.token_urlsafe(24)
    return jsonify({
        "user_id": uid, "username": u["username"], "role": u["role"],
        "subscription_active": active, "subscription_until": u["subscription_until"],
        "stats": stats, "templates": templates, "targets": targets,
        "delay": (cfg["delay"] if cfg else u["delay"]),
        "mode": (cfg["mode"] if cfg else u["mode"]),
        "randomize": bool((cfg["randomize"] if cfg else u["randomize"])),
        "warmup": bool((cfg["warmup"] if cfg else u["warmup"])),
        "autosender_enabled": bool(u["autosender_enabled"]),
        "webhook_url": u["webhook_url"],
        "balance": u["balance"],
        "has_session": bool(u["session_string"]),
        "job_status": job_status,
        "csrf": session["csrf"]
    })

@app.post("/api/templates")
@login_required
@csrf_required
def api_templates():
    uid = int(request.titan_user["user_id"])
    raw = (request.get_json(silent=True) or {}).get("text", "")

    # FIX: просто режем по строкам. Пользователь сам разделяет шаблоны Enter-ом.
    chunks = []
    for line in raw.split("\n"):
        s = line.strip()
        if s:
            chunks.append(s)

    values = list(dict.fromkeys(chunks))[:MAX_TEMPLATES]
    run_async(db.update_user_lists(uid, templates=values))
    return jsonify({"ok": True, "count": len(values), "templates": values})

@app.post("/api/targets")
@login_required
@csrf_required
def api_targets():
    uid = int(request.titan_user["user_id"])
    raw = (request.get_json(silent=True) or {}).get("text", "")
    values = parse_targets(raw)
    run_async(db.update_user_lists(uid, targets=values))
    return jsonify({"ok": True, "count": len(values)})

@app.post("/api/config")
@login_required
@csrf_required
def api_config():
    uid = int(request.titan_user["user_id"])
    payload = request.get_json(silent=True) or {}
    delay = max(0, min(3600, int(payload.get("delay", 10))))
    randomize = bool(payload.get("randomize"))
    run_async(db.update_user_lists(
        uid, delay=delay, randomize=randomize,
        warmup=bool(payload.get("warmup")),
        autosender_enabled=bool(payload.get("autosender_enabled")),
        webhook_url=str(payload.get("webhook_url") or "")[:500]
    ))
    run_async(db.set_config(uid, delay=delay,
                             randomize=1 if randomize else 0,
                             warmup=1 if payload.get("warmup") else 0))
    # FIX: возвращаем randomize, чтобы фронт синхронизировал чекбокс
    return jsonify({"ok": True, "randomize": randomize})

@app.post("/api/mode")
@login_required
@csrf_required
def api_mode():
    uid = int(request.titan_user["user_id"])
    mode = str((request.get_json(silent=True) or {}).get("mode", "AUTO")).upper()
    if mode not in ("AUTO", "EXTREME"):
        return jsonify({"error": "invalid_mode"}), 400
    run_async(db.update_user_lists(uid, mode=mode))
    run_async(db.set_config(uid, mode=mode))
    return jsonify({"ok": True, "mode": mode})

@app.post("/api/job/start")
@login_required
@csrf_required
def api_job_start():
    uid = int(request.titan_user["user_id"])
    u = run_async(db.get_user(uid))
    if not u or not u["session_string"]:
        return jsonify({"error": "Подключите аккаунт"}), 400
    templates = safe_json_loads(u["templates"], [])
    targets = safe_json_loads(u["targets"], [])
    if not templates or not targets:
        return jsonify({"error": "Добавьте шаблоны и цели"}), 400
    old = workers.get(uid)
    if old and not old.done():
        return jsonify({"error": "Уже запущено"}), 409
    stop_event = asyncio.Event()
    worker_stops[uid] = stop_event
    run_async(db.set_job_status(uid, "running"))
    workers[uid] = asyncio.run_coroutine_threadsafe(notification_worker(uid, stop_event), loop)
    return jsonify({"ok": True, "message": "Движок запущен"})

@app.post("/api/job/stop")
@login_required
@csrf_required
def api_job_stop():
    uid = int(request.titan_user["user_id"])
    stop_event = worker_stops.get(uid)
    if stop_event:
        loop.call_soon_threadsafe(stop_event.set)
    run_async(db.set_job_status(uid, "stopped"))
    return jsonify({"ok": True, "message": "Остановлено"})

@app.get("/api/accounts")
@login_required
def api_accounts():
    uid = int(request.titan_user["user_id"])
    return jsonify({"accounts": run_async(db.list_accounts(uid))})

@app.post("/api/accounts/delete")
@login_required
@csrf_required
def api_accounts_delete():
    uid = int(request.titan_user["user_id"])
    account_id = int((request.get_json(silent=True) or {}).get("id", 0))
    run_async(db.remove_account(uid, account_id))
    return jsonify({"ok": True})

# --- AUTH TELEGRAM ---
@app.post("/api/auth/send_code")
@login_required
@csrf_required
def api_auth_send_code():
    uid = int(request.titan_user["user_id"])
    phone = (request.get_json(silent=True) or {}).get("phone", "")
    if not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
        return jsonify({"error": "Неверный формат телефона"}), 400

    async def _send():
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
            sent = await client.send_code_request(phone)
            web_flows[uid] = {"client": client, "phone": phone,
                              "hash": sent.phone_code_hash, "ts": time.time()}
            return True
        except Exception:
            try: await client.disconnect()
            except Exception: pass
            raise

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=25)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 400

@app.post("/api/auth/verify_code")
@login_required
@csrf_required
def api_auth_verify_code():
    uid = int(request.titan_user["user_id"])
    code = (request.get_json(silent=True) or {}).get("code", "")
    flow = web_flows.get(uid)
    if not flow:
        return jsonify({"error": "Сессия истекла"}), 400
    if time.time() - flow.get("ts", 0) > 900:
        web_flows.pop(uid, None)
        return jsonify({"error": "Код устарел"}), 400

    async def _verify():
        client = flow["client"]
        try:
            await client.sign_in(phone=flow["phone"], code=code,
                                 phone_code_hash=flow["hash"])
            await db.set_session(uid, client.session.save(), flow["phone"], "auto")
            await client.disconnect()
            return {"ok": True, "need_2fa": False}
        except SessionPasswordNeededError:
            return {"ok": True, "need_2fa": True}
        except PhoneCodeInvalidError:
            return {"error": "Неверный код"}
        except Exception as e:
            try: await client.disconnect()
            except Exception: pass
            return {"error": str(e)[:120]}

    try:
        res = asyncio.run_coroutine_threadsafe(_verify(), loop).result(timeout=25)
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 400
    if res.get("error"):
        return jsonify(res), 400
    if not res.get("need_2fa"):
        web_flows.pop(uid, None)
    return jsonify(res)

@app.post("/api/auth/verify_2fa")
@login_required
@csrf_required
def api_auth_verify_2fa():
    uid = int(request.titan_user["user_id"])
    passwd = (request.get_json(silent=True) or {}).get("pass", "")
    flow = web_flows.get(uid)
    if not flow:
        return jsonify({"error": "Сессия истекла"}), 400

    async def _verify():
        client = flow["client"]
        try:
            await client.sign_in(password=passwd)
            await db.set_session(uid, client.session.save(), flow["phone"], "auto")
            await client.disconnect()
            return {"ok": True}
        except PasswordHashInvalidError:
            return {"error": "Неверный пароль"}
        except Exception as e:
            return {"error": str(e)[:120]}

    try:
        res = asyncio.run_coroutine_threadsafe(_verify(), loop).result(timeout=25)
    except Exception as e:
        return jsonify({"error": str(e)[:120]}), 400
    web_flows.pop(uid, None)
    return jsonify(res)

# --- PARSER ---
@app.post("/api/parser/run")
@login_required
@csrf_required
def api_parser_run():
    uid = int(request.titan_user["user_id"])
    payload = request.get_json(silent=True) or {}
    chat = str(payload.get("chat") or "").strip()
    limit = max(10, min(10000, int(payload.get("limit", 500))))
    if not chat:
        return jsonify({"error": "Укажите чат"}), 400
    u = run_async(db.get_user(uid))
    if not u or not u["session_string"]:
        return jsonify({"error": "Нет аккаунта"}), 400

    async def _parse():
        async with client_manager.acquire(u["session_string"]) as cli:
            try:
                entity = await cli.get_entity(chat)
            except Exception as e:
                raise RuntimeError(f"Не открыть чат: {e}")
            members = []
            try:
                async for user in cli.iter_participants(entity, limit=limit):
                    if user.bot: continue
                    members.append({"id": user.id, "username": user.username or ""})
                    if len(members) >= limit: break
            except Exception as e:
                raise RuntimeError(f"Ошибка парсинга: {e}")
            added = await db.save_parsed_members(uid, chat, members)
            await db.inc_stat(uid, "parsed", added)
            await db.audit(uid, "PARSER_RUN", f"{chat} → {added}")
            return added

    try:
        added = asyncio.run_coroutine_threadsafe(_parse(), loop).result(timeout=120)
        return jsonify({"ok": True, "added": added})
    except Exception as e:
        return jsonify({"error": str(e)[:180]}), 400

# --- INVITER ---
@app.post("/api/invites/save")
@login_required
@csrf_required
def api_invites_save():
    uid = int(request.titan_user["user_id"])
    raw = (request.get_json(silent=True) or {}).get("text", "")
    links = parse_invites(raw)
    run_async(db.update_user_lists(uid, invite_links=links))
    return jsonify({"ok": True, "count": len(links)})

@app.post("/api/inviter/run")
@login_required
@csrf_required
def api_inviter_run():
    uid = int(request.titan_user["user_id"])
    u = run_async(db.get_user(uid))
    if not u or not u["session_string"]:
        return jsonify({"error": "Нет аккаунта"}), 400
    links = safe_json_loads(u["invite_links"], [])
    if not links:
        return jsonify({"error": "Сохраните инвайт-ссылки"}), 400

    async def _invite():
        ok, failed = 0, 0
        async with client_manager.acquire(u["session_string"]) as cli:
            for h in links:
                try:
                    await cli(ImportChatInviteRequest(h))
                    ok += 1
                    await asyncio.sleep(2)
                except Exception:
                    failed += 1
        await db.inc_stat(uid, "invites", ok)
        await db.audit(uid, "INVITER_RUN", f"ok={ok}, failed={failed}")
        return ok, failed

    try:
        ok, failed = asyncio.run_coroutine_threadsafe(_invite(), loop).result(timeout=180)
        return jsonify({"ok": ok, "failed": failed})
    except Exception as e:
        return jsonify({"error": str(e)[:180]}), 400

# --- TERMINAL ---
@app.get("/api/terminal")
@login_required
def api_terminal():
    return jsonify({"lines": tail_log(200)})

# --- ADMIN ---
@app.get("/api/admin/users")
@admin_required
def api_admin_users():
    return jsonify({"users": run_async(db.all_users())})

@app.get("/api/admin/stats")
@admin_required
def api_admin_stats():
    res = run_async(db.admin_stats()) or {}
    if psutil:
        try:
            p = psutil.Process(os.getpid())
            res["ram_mb"] = round(p.memory_info().rss / 1024 / 1024, 1)
            res["cpu_percent"] = psutil.cpu_percent(interval=None)
        except Exception:
            res["ram_mb"], res["cpu_percent"] = None, None
    else:
        res["ram_mb"], res["cpu_percent"] = None, None
    if client_manager:
        res["active_clients"] = client_manager.active
        res["waiting_clients"] = client_manager.waiting
        res["client_limit"] = client_manager.maximum
    else:
        res["active_clients"] = 0
        res["waiting_clients"] = 0
        res["client_limit"] = MAX_ACTIVE_CLIENTS
    return jsonify(res)

@app.post("/api/admin/approve")
@admin_required
@csrf_required
def api_admin_approve():
    payload = request.get_json(silent=True) or {}
    uid = int(payload.get("user_id", 0))
    enabled = bool(payload.get("enabled", True))
    if not uid:
        return jsonify({"error": "user_id required"}), 400
    if is_admin(uid) and not enabled:
        return jsonify({"error": "Нельзя снять доступ с SUPER_ADMIN"}), 400
    run_async(db.set_approved(uid, enabled))
    run_async(db.audit(request.titan_user["user_id"],
                        "ADMIN_APPROVE", f"{uid} → {'ON' if enabled else 'OFF'}"))
    return jsonify({"ok": True, "approved": enabled})

@app.post("/api/admin/grant")
@admin_required
@csrf_required
def api_admin_grant():
    payload = request.get_json(silent=True) or {}
    uid = int(payload.get("user_id", 0))
    days = int(payload.get("days", 30))
    expiry = run_async(db.grant_subscription(uid, days))
    run_async(db.audit(request.titan_user["user_id"],
                        "ADMIN_GRANT_SUB", f"{uid} → {expiry}"))
    return jsonify({"ok": True, "until": expiry})

@app.post("/api/admin/balance")
@admin_required
@csrf_required
def api_admin_balance():
    payload = request.get_json(silent=True) or {}
    uid = int(payload.get("user_id", 0))
    amount = int(payload.get("amount", 0))
    run_async(db.update_balance(uid, amount))
    run_async(db.audit(request.titan_user["user_id"],
                        "ADMIN_BALANCE", f"{uid} {amount:+}"))
    return jsonify({"ok": True})

@app.get("/api/admin/logs")
@admin_required
def api_admin_logs():
    try:
        uid_raw = request.args.get("user_id", "").strip()
        uid = int(uid_raw) if uid_raw else None
        limit = int(request.args.get("limit", "200"))
        entries = run_async(db.recent_audit(limit=limit, user_id=uid)) or []
        return jsonify({"entries": entries})
    except ValueError:
        return jsonify({"error": "invalid_params"}), 400

@app.post("/api/admin/logs/clear")
@admin_required
@csrf_required
def api_admin_logs_clear():
    payload = request.get_json(silent=True) or {}
    uid = int(payload.get("user_id", 0))
    if not uid:
        return jsonify({"error": "user_id required"}), 400
    run_async(db.clear_audit_user(uid))
    run_async(db.audit(request.titan_user["user_id"],
                        "ADMIN_LOG_CLEAR", f"user={uid}"))
    return jsonify({"ok": True})

# =====================================================================
# WORKER + WARMUP
# =====================================================================

async def warmup_client(client):
    try:
        await client(UpdateStatusRequest(offline=False))
        dialogs = await client.get_dialogs(limit=5)
        for d in dialogs:
            if not d.is_user and not d.is_group:
                continue
            try:
                await client.get_messages(d.entity, limit=3)
                await asyncio.sleep(1.5)
            except Exception:
                continue
    except Exception as e:
        log.warning(f"Warmup skipped: {e}")

async def send_to_target(uid, session_str, target, message, delay, stop_event, mode):
    entity = None
    try:
        async with client_manager.acquire(session_str) as client:
            try:
                if isinstance(target, int):
                    try:
                        entity = await client.get_entity(target)
                    except ValueError:
                        try:
                            await client(JoinChannelRequest(target))
                            entity = await client.get_entity(target)
                        except Exception:
                            raise ValueError(f"Не найден чат {target}")
                else:
                    entity = await client.get_entity(target)
                await client.send_message(entity, message)
            except FloodWaitError as fw:
                await db.inc_stat(uid, "floodwaits", 1)
                await db.audit(uid, "FLOODWAIT", f"{target}: {fw.seconds}s")
                await asyncio.sleep(fw.seconds + 1)
                if entity is None:
                    try:
                        entity = await client.get_entity(target)
                    except Exception as e:
                        raise e
                try:
                    await client.send_message(entity, message)
                    await db.inc_stat(uid, "sent", 1)
                    return
                except Exception as e:
                    raise e
        await db.inc_stat(uid, "sent", 1)
    except Exception as exc:
        await db.inc_stat(uid, "errors", 1)
        await db.audit(uid, "NOTIFY_ERROR", f"{target}: {str(exc)[:300]}")

    actual_delay = max(1, delay) if mode == "AUTO" else 0
    if actual_delay > 0:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=actual_delay)
        except asyncio.TimeoutError:
            pass

async def notification_worker(uid, stop_event):
    log.info(f"Engine started for user {uid}")
    sent_before = (await db.get_stats(uid))["sent"]
    errors_before = (await db.get_stats(uid))["errors"]
    
    # FIX: глобальный счётчик шаблонов. Растёт с КАЖДОЙ отправкой,
    # независимо от количества целей. Шаблоны идут строго по кругу.
    tpl_idx = 0 
    
    try:
        while not stop_event.is_set():
            user = await db.get_user(uid)
            cfg = await db.get_config(uid)
            if not user or not user["session_string"]:
                break
            templates = safe_json_loads(user["templates"], [])
            targets = safe_json_loads(user["targets"], [])
            if not templates or not targets:
                break
            mode = (cfg["mode"] if cfg else "AUTO").upper()
            delay = max(0, int(cfg["delay"] if cfg else 10))
            randomize = bool(cfg["randomize"] if cfg else True)
            do_warmup = bool(cfg["warmup"] if cfg else False)

            if do_warmup and mode == "AUTO":
                try:
                    async with client_manager.acquire(user["session_string"]) as wcli:
                        await warmup_client(wcli)
                except Exception as e:
                    log.warning(f"Warmup failed: {e}")

            tasks = []
            for target in targets:
                if stop_event.is_set():
                    break
                
                # FIX: берем шаблон по глобальному счётчику (tpl_idx), 
                # а не по индексу цели (i).
                tpl_raw = templates[tpl_idx % len(templates)]
                tpl_idx += 1
                
                tpl = randomize_text(tpl_raw, randomize)
                if len(tpl) > 4000:
                    tpl = tpl[:4000]
                tasks.append(asyncio.create_task(
                    send_to_target(uid, user["session_string"], target,
                                   tpl, delay, stop_event, mode)
                ))
            await asyncio.gather(*tasks, return_exceptions=True)
            engine_state["cycles"] += 1
            engine_state["last_cycle_at"] = iso_now()
            engine_state["mode"] = mode
            if stop_event.is_set():
                break

        await db.set_job_status(uid, "stopped")
        user = await db.get_user(uid)
        wh = (user["webhook_url"] or "").strip() if user else ""
        if wh.startswith("http"):
            stats = await db.get_stats(uid)
            report = {
                "user_id": uid, "sent_total": stats["sent"],
                "errors_total": stats["errors"], "floodwaits": stats["floodwaits"],
                "sent_in_session": stats["sent"] - sent_before,
                "errors_in_session": stats["errors"] - errors_before,
                "finished_at": iso_now(), "mode": engine_state["mode"],
            }
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                    await s.post(wh, json=report)
            except Exception as e:
                log.warning(f"Webhook failed: {e}")
        log.info(f"Engine stopped for user {uid}")
    except asyncio.CancelledError:
        await db.set_job_status(uid, "stopped")
        raise
    finally:
        worker_stops.pop(uid, None)
        workers.pop(uid, None)

# =====================================================================
# STARTUP
# =====================================================================

def run_web():
    log.info(f"Web server starting on {WEB_HOST}:{WEB_PORT}")
    serve(app, host=WEB_HOST, port=WEB_PORT, threads=8)

async def main():
    global db, loop, client_manager
    loop = asyncio.get_running_loop()
    db = Database(DB_PATH)
    await db.initialize()
    client_manager = ClientManager(db, MAX_ACTIVE_CLIENTS)
    threading.Thread(target=run_web, daemon=True, name="titan-web").start()
    await setup_bot()
    log.info("TITAN Omni-Core v15.1 online")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("TITAN Omni-Core stopped")
