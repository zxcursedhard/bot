#!/usr/bin/env python3
"""
TITAN Enterprise v16.0 — GPT UI Mobile Edition
Single-file high-performance Telegram automation platform.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

import aiosqlite
import aiohttp
import psutil
from flask import Flask, abort, jsonify, redirect, render_template_string, request, session, url_for
from telethon import TelegramClient, events, Button
from telethon.errors import (
    FloodWaitError, RPCError, SessionPasswordNeededError,
    ChatWriteForbiddenError, SlowModeWaitError
)
from telethon.sessions import StringSession
from waitress import serve

# ------------------------------- Configuration -------------------------------
APP_NAME, VERSION = "TITAN Enterprise", "16.0 GPT Mobile"
API_ID = int(os.getenv("API_ID", "2040") or "2040")
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8732669492:AAGjBx_kMW4JRpo4DyhCoRwAISSS-lJ1IhE")
FLASK_SECRET = os.getenv("FLASK_SECRET", secrets.token_urlsafe(48))
DB_PATH = os.getenv("DATABASE_PATH", "titan_omnicore.db")
BACKUP_DIR = os.getenv("BACKUP_DIR", "database_backups")
HOST, PORT = os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "10000"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
SUPER_ADMINS = {int(x) for x in os.getenv("SUPER_ADMINS", "8655682794,8773171051").split(",") if x.strip().isdigit()}
COOKIE_DAYS, TOKEN_TTL = 14, 300
MAX_CLIENTS, MAX_TEMPLATES = 20, 5000
os.makedirs(BACKUP_DIR, exist_ok=True)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("titan")

# --------------------------------- Utilities ----------------------------------
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

def jdump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

def jload(value, default):
    try: return json.loads(value or "")
    except (TypeError, ValueError): return default

def bounded_float(value, default=3.0, low=0.0, high=60.0):
    try: value = float(value)
    except (TypeError, ValueError): value = default
    return min(high, max(low, value))

def bounded_int(value, default=0, low=0, high=1_000_000):
    try: value = int(value)
    except (TypeError, ValueError): value = default
    return min(high, max(low, value))

def parse_targets(text):
    out = []
    for line in text.splitlines():
        x = line.strip()
        if not x: continue
        if x.startswith("tg://chat?id="):
            try:
                out.append(int(x.split("=")[1]))
                continue
            except: pass
        m = re.match(r"^(?:https?://)?t\.me/c/(\d+)", x)
        if m: out.append(int("-100" + m.group(1))); continue
        if re.match(r"^-?\d+$", x): out.append(int(x))
        elif x.startswith("@"): out.append(x)
        elif re.match(r"^https?://t\.me/", x): out.append("@" + x.split("/")[-1])
    return out

# ------------------------------- Async SQLite --------------------------------
class Database:
    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()

    async def connect(self):
        conn = await aiosqlite.connect(self.path, timeout=30)
        conn.row_factory = aiosqlite.Row
        for pragma in ["journal_mode=WAL", "foreign_keys=ON", "busy_timeout=30000", "synchronous=NORMAL"]:
            await conn.execute(f"PRAGMA {pragma}")
        return conn

    async def init(self):
        schema = [
            """CREATE TABLE IF NOT EXISTS users(
              user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '',
              first_name TEXT DEFAULT '', subscribed INTEGER NOT NULL DEFAULT 0,
              balance INTEGER NOT NULL DEFAULT 0, plan TEXT NOT NULL DEFAULT 'FREE',
              plan_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS accounts(
              id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
              session TEXT NOT NULL, phone TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
              created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS configs(
              owner_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'AUTO',
              send_gap REAL NOT NULL DEFAULT 3, templates TEXT NOT NULL DEFAULT '[]',
              targets TEXT NOT NULL DEFAULT '[]', randomize INTEGER DEFAULT 0,
              warmup INTEGER DEFAULT 1, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS stats(
              owner_id INTEGER PRIMARY KEY, sent INTEGER NOT NULL DEFAULT 0,
              failed INTEGER NOT NULL DEFAULT 0, broadcasts INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS tokens(
              id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
              token_hash TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
              used INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS sessions(
              id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
              session_id TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
              created_at TEXT NOT NULL, last_seen TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER,
              action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS broadcasts(
              id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, body TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued', total INTEGER DEFAULT 0,
              sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
              created_at TEXT NOT NULL, finished_at TEXT)""",
        ]
        async with self.lock:
            conn = await self.connect()
            try:
                for statement in schema:
                    await conn.execute(statement)
                await conn.commit()
            finally:
                await conn.close()

    async def execute(self, sql, params=()):
        async with self.lock:
            conn = await self.connect()
            try:
                cur = await conn.execute(sql, params)
                await conn.commit()
                return cur.rowcount
            finally:
                await conn.close()

    async def one(self, sql, params=()):
        async with self.lock:
            conn = await self.connect()
            try:
                cur = await conn.execute(sql, params)
                row = await cur.fetchone()
                return dict(row) if row else None
            finally:
                await conn.close()

    async def all(self, sql, params=()):
        async with self.lock:
            conn = await self.connect()
            try:
                cur = await conn.execute(sql, params)
                return [dict(row) for row in await cur.fetchall()]
            finally:
                await conn.close()

    async def ensure_user(self, uid, username="", first_name=""):
        stamp = now()
        await self.execute("""INSERT INTO users(user_id,username,first_name,created_at,updated_at)
          VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
          first_name=excluded.first_name,updated_at=excluded.updated_at""",
          (uid, username or "", first_name or "", stamp, stamp))
        await self.execute("INSERT OR IGNORE INTO configs(owner_id,updated_at) VALUES(?,?)", (uid, stamp))
        await self.execute("INSERT OR IGNORE INTO stats(owner_id,updated_at) VALUES(?,?)", (uid, stamp))

    async def config(self, uid):
        row = await self.one("SELECT * FROM configs WHERE owner_id=?", (uid,))
        if not row:
            await self.execute("INSERT OR IGNORE INTO configs(owner_id,updated_at) VALUES(?,?)", (uid, now()))
            row = await self.one("SELECT * FROM configs WHERE owner_id=?", (uid,))
        row["templates"] = jload(row["templates"], [])
        row["targets"] = jload(row["targets"], [])
        return row

    async def save_config(self, uid, gap, templates, targets, mode, randomize, warmup):
        await self.execute("""UPDATE configs SET send_gap=?,templates=?,targets=?,mode=?,randomize=?,warmup=?,updated_at=?
          WHERE owner_id=?""",
          (bounded_float(gap), jdump(templates), jdump(targets), mode, int(randomize), int(warmup), now(), uid))

database = Database(DB_PATH)

# ------------------------------ Runtime manager ------------------------------
class Runtime:
    def __init__(self):
        self.loop = None
        self.bot = None
        self.http = None
        self.started = time.time()
        self.state = "BOOTING"
        self.logs = deque(maxlen=1000)
        self.semaphore = None
        self.queue = None
        self.stop = None
        self.template_queues = {}
        self.active = 0
        self.active_engines = {}

    def write(self, message, level="INFO"):
        line = f"{datetime.now():%H:%M:%S} [{level}] {message}"
        self.logs.appendleft(line)
        getattr(log, level.lower(), log.info)(message)

rt = Runtime()

def submit(coro):
    if not rt.loop or not rt.loop.is_running():
        raise RuntimeError("Async runtime is not ready")
    return asyncio.run_coroutine_threadsafe(coro, rt.loop)

# ------------------------------ Telegram bot ---------------------------------
def bot_keyboard():
    return [[Button.inline("🌐 Войти в панель", b"panel")]]

async def magic_link(uid):
    if not PUBLIC_BASE_URL:
        raise RuntimeError("Set PUBLIC_BASE_URL, e.g. https://your-domain.example")
    raw = secrets.token_urlsafe(36)
    await database.execute("INSERT INTO tokens(owner_id,token_hash,expires_at,created_at) VALUES(?,?,?,?)",
                           (uid, digest(raw), time.time() + TOKEN_TTL, now()))
    return f"{PUBLIC_BASE_URL}/login?token={raw}"

async def on_command(event):
    uid = int(event.sender_id)
    sender = await event.get_sender()
    await database.ensure_user(uid, getattr(sender, "username", "") or "", getattr(sender, "first_name", "") or "")
    command = (event.raw_text or "").split(maxsplit=1)[0].split("@")[0].lower()
    
    if command == "/start":
        await event.respond(f"**{APP_NAME} // {VERSION}**\nНажми кнопку ниже для входа в панель.", buttons=bot_keyboard(), parse_mode="md")
    elif command == "/panel":
        if uid not in SUPER_ADMINS:
            return await event.respond("Доступ запрещён.")
        try:
            await event.respond("Одноразовая ссылка (5 минут):\n" + await magic_link(uid))
        except Exception as exc:
            await event.respond(f"Ошибка: {exc}")

async def on_callback(event):
    uid = int(event.sender_id)
    action = event.data.decode(errors="ignore")
    if action == "panel":
        if uid not in SUPER_ADMINS:
            return await event.answer("Доступ запрещён", alert=True)
        await event.respond("Одноразовая ссылка:\n" + await magic_link(uid))
        await event.answer("Ссылка отправлена")

async def telegram_start():
    rt.bot = TelegramClient(StringSession(), API_ID, API_HASH, connection_retries=5, request_retries=3, auto_reconnect=True)
    await rt.bot.start(bot_token=BOT_TOKEN)
    rt.bot.add_event_handler(on_command, events.NewMessage(incoming=True, pattern=r"^/(start|panel)(?:@\w+)?"))
    rt.bot.add_event_handler(on_callback, events.CallbackQuery())
    rt.state = "ONLINE"
    rt.write("Telegram bot connected")

# ------------------------------ Broadcast engine -----------------------------
class SpamEngine:
    def __init__(self, uid, config, accounts):
        self.uid = uid
        self.config = config
        self.accounts = accounts
        self.stop_event = asyncio.Event()
        self.templates = config["templates"]
        self.targets = config["targets"]
        self.t_queue = deque(self.templates)

    def _next_tpl(self):
        if not self.t_queue: return "..."
        t = self.t_queue.popleft()
        self.t_queue.append(t)
        if self.config["randomize"]:
            repl = {'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 'х': 'x'}
            return ''.join(repl.get(c, c) for c in t)
        return t

    async def _warmup(self, client):
        if not self.config["warmup"]: return
        rt.write(f"[{self.uid}] Запуск прогрева аккаунта...")
        try:
            dialogs = await client.get_dialogs(limit=5)
            for d in dialogs:
                await client.get_messages(d.id, limit=2)
                await asyncio.sleep(random.uniform(1, 3))
            rt.write(f"[{self.uid}] Прогрев завершен.")
        except Exception as e:
            rt.write(f"[{self.uid}] Ошибка прогрева: {e}", "WARNING")

    async def start(self):
        tasks = [asyncio.create_task(self._worker(acc)) for acc in self.accounts]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self, acc):
        client = TelegramClient(StringSession(acc["session"]), API_ID, API_HASH, connection_retries=1)
        await client.connect()
        await self._warmup(client)
        
        while not self.stop_event.is_set():
            for target in self.targets:
                if self.stop_event.is_set(): break
                msg = self._next_tpl()
                try:
                    entity = target
                    if isinstance(target, int):
                        try: entity = await client.get_entity(target)
                        except: pass
                    await client.send_message(entity, msg)
                    await database.execute("UPDATE stats SET sent=sent+1,updated_at=? WHERE owner_id=?", (now(), self.uid))
                except FloodWaitError as e:
                    rt.write(f"[{self.uid}] FloodWait {e.seconds}s", "WARNING")
                    await asyncio.sleep(e.seconds + 2)
                except Exception as e:
                    await database.execute("UPDATE stats SET failed=failed+1,updated_at=? WHERE owner_id=?", (now(), self.uid))
                    rt.write(f"[{self.uid}] Ошибка: {e}", "WARNING")
                
                delay = 0.05 if self.config["mode"] == "EXTREME" else self.config["send_gap"]
                if delay > 0:
                    try: await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError: pass
        await client.disconnect()

    def stop(self):
        self.stop_event.set()

# -------------------------------- Web dashboard -------------------------------
app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=COOKIE_DAYS * 86400, MAX_CONTENT_LENGTH=1024 * 1024)

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Space+Mono:wght@400;700&display=swap');
:root{--bg:#050505;--panel:#0d1118;--line:#252c38;--red:#ff003c;--cyan:#00f0ff;--green:#35ff9a;--muted:#8994a5;--text:#eef3fa}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 80% 0%,#1b0710,transparent 38%),var(--bg);color:var(--text);font-family:Inter,sans-serif}
a{color:var(--cyan);text-decoration:none}button{border:1px solid #6b1630;background:linear-gradient(135deg,#ff003c,#a0002b);color:#fff;padding:11px 16px;border-radius:8px;font-weight:700;cursor:pointer;width:100%}
input,textarea,select{width:100%;background:#070a0f;border:1px solid var(--line);color:var(--text);padding:12px;border-radius:8px;font:inherit;margin-bottom:10px}textarea{min-height:125px}
.shell{display:flex;min-height:100vh}.side{position:fixed;inset:0 auto 0 0;width:235px;padding:25px 17px;background:#080a0e;border-right:1px solid var(--line)}
.brand{font:700 17px 'Space Mono';letter-spacing:1px}.brand b{color:var(--red)}.tag{font:10px 'Space Mono';color:var(--cyan);margin:8px 0 28px}
.nav a{display:block;padding:12px;border-radius:8px;color:#aeb8c6;margin:5px 0}.nav a:hover{background:#1b1018;color:white;border-left:2px solid var(--red)}
.main{margin-left:235px;width:calc(100% - 235px);padding:30px;max-width:1600px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:25px}
h1{font-size:25px;margin:0 0 5px}h2{font-size:14px;margin:0 0 16px}.muted{color:var(--muted);font-size:13px}.mono{font-family:'Space Mono',monospace}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.card{background:linear-gradient(145deg,#111722,#0a0d12);border:1px solid var(--line);border-radius:13px;padding:19px;box-shadow:0 12px 40px #0005}
.metric{font:700 28px 'Space Mono';margin-top:12px}.red{color:var(--red)}.cyan{color:var(--cyan)}.green{color:var(--green)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}.field{margin:13px 0}.field label{display:block;color:#aeb8c6;font-size:12px;margin-bottom:7px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:11px;border-bottom:1px solid var(--line)}th{color:var(--muted);font:10px 'Space Mono'}
.pill{display:inline-block;padding:5px 9px;border-radius:30px;background:#15251e;color:var(--green);font:10px 'Space Mono'}
.terminal{background:#020604;border:1px solid #164c32;border-radius:10px;padding:17px;color:var(--green);font:12px/1.8 'Space Mono';height:450px;overflow:auto;white-space:pre-wrap}
.notice{padding:12px;border:1px solid #512332;background:#1a0b11;border-radius:9px;color:#ff9eb4;margin:14px 0}.panel{margin-top:17px}.login{max-width:450px;margin:10vh auto;padding:28px}
.footer{margin-top:30px;color:#687487;font:10px 'Space Mono'}
@media(max-width:850px){.side{width:185px}.main{margin-left:185px;width:calc(100% - 185px);padding:20px}.grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.side{position:relative;width:100%;height:auto;border:0;border-bottom:1px solid var(--line)}.nav{display:flex;overflow:auto}.nav a{white-space:nowrap}.main{margin:0;width:100%;padding:16px}.grid,.row{grid-template-columns:1fr}.top{align-items:flex-start;gap:12px;flex-direction:column}}
"""

LAYOUT = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} | TITAN</title><style>""" + CSS + """</style></head><body><div class="shell">
<aside class="side"><div class="brand">TITAN <b>ENTERPRISE</b></div><div class="tag">OMNI-CORE // v16.0</div><nav class="nav">
<a href="/">◈ Дашборд</a><a href="/accounts">▣ Аккаунты</a><a href="/config">⚙ Конфигурация</a><a href="/terminal">⌁ Терминал</a>
{% if admin %}<a href="/admin">♜ Админка</a>{% endif %}<a href="/logout">↗ Выход</a></nav><div class="footer">SECURE CONTROL SURFACE<br>ALL SYSTEMS MONITORED</div></aside>
<main class="main"><header class="top"><div><h1>{{ title }}</h1><div class="muted">TITAN Enterprise / Control Center</div></div>
<div><span class="pill">● {{ state }}</span> <span class="muted mono">UID {{ uid }}</span></div></header>
{% if flash %}<div class="notice">{{ flash }}</div>{% endif %}{{ content|safe }}</main></div></body></html>"""

def csrf_token():
    if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]

def check_csrf():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        given = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf", "")
        if not given or not expected or not hmac.compare_digest(given, expected):
            abort(400, "CSRF validation failed")

def uid():
    value = session.get("user_id")
    return int(value) if value is not None else None

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if uid() is None: return redirect("/login")
        return fn(*args, **kwargs)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if uid() not in SUPER_ADMINS: abort(403)
        return fn(*args, **kwargs)
    return wrapped

def page(title, content):
    return render_template_string(LAYOUT, title=title, content=content,
                                  uid=uid(), admin=uid() in SUPER_ADMINS,
                                  state=rt.state, flash="")

@app.before_request
def before_request(): check_csrf()

@app.get("/login")
def login():
    return render_template_string("""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TITAN Login</title><style>""" + CSS + """</style>
<body><div class="card login"><div class="brand">TITAN <b>ENTERPRISE</b></div><div class="tag">SECURE AUTH GATE</div>
<h1>Вход в панель</h1><p class="muted">Создай одноразовую ссылку командой /panel в Telegram.</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><div class="field"><label>Одноразовый токен</label>
<input name="token" value="{{ token }}" required></div><button>Авторизоваться →</button></form>
{% if error %}<div class="notice">{{ error }}</div>{% endif %}</div></body></html>""",
        csrf=csrf_token(), token=request.args.get("token", ""), error=request.args.get("error", ""))

@app.post("/login")
def login_post():
    raw = request.form.get("token", "").strip()
    if not raw or len(raw) > 256: return redirect("/login?error=Invalid+token")
    record = submit(database.one("SELECT * FROM tokens WHERE token_hash=? AND used=0 AND expires_at>?",
                                 (digest(raw), time.time()))).result(10)
    if not record: return redirect("/login?error=Link+expired+or+invalid")
    submit(database.execute("UPDATE tokens SET used=1 WHERE id=? AND used=0", (record["id"],))).result(10)
    session.clear()
    session.permanent = True
    session["user_id"] = int(record["owner_id"])
    session["session_id"] = secrets.token_urlsafe(32)
    session["csrf"] = secrets.token_urlsafe(32)
    submit(database.execute("INSERT INTO sessions(owner_id,session_id,expires_at,created_at,last_seen) VALUES(?,?,?,?,?)",
                            (record["owner_id"], session["session_id"], time.time()+COOKIE_DAYS*86400, now(), now()))).result(10)
    submit(database.audit(record["owner_id"], "web_login")).result(10)
    return redirect("/")

@app.get("/logout")
def logout():
    sid = session.get("session_id")
    if sid:
        with contextlib.suppress(Exception):
            submit(database.execute("DELETE FROM sessions WHERE session_id=?", (sid,))).result(5)
    session.clear()
    return redirect("/login")

@app.get("/")
@login_required
def dashboard():
    owner = uid()
    stats = submit(database.one("SELECT * FROM stats WHERE owner_id=?", (owner,))).result(10) or {}
    subs = submit(database.one("SELECT COUNT(*) n FROM users WHERE subscribed=1")).result(10)["n"]
    accs = submit(database.one("SELECT COUNT(*) n FROM accounts WHERE owner_id=? AND status='active'", (owner,))).result(10)["n"]
    jobs = submit(database.all("SELECT * FROM broadcasts WHERE owner_id=? ORDER BY created_at DESC LIMIT 8", (owner,))).result(10)
    content = render_template_string("""
<div class="grid"><div class="card"><div class="muted">ОТПРАВЛЕНО</div><div class="metric red">{{ stats.get('sent',0) }}</div></div>
<div class="card"><div class="muted">ОШИБКИ</div><div class="metric">{{ stats.get('failed',0) }}</div></div>
<div class="card"><div class="muted">ПОДПИСЧИКИ</div><div class="metric cyan">{{ subs }}</div></div>
<div class="card"><div class="muted">АККАУНТЫ</div><div class="metric green">{{ accs }}</div></div></div>
<div class="row"><section class="card"><h2>НОВАЯ КАМПАНИЯ</h2><form method="post" action="/broadcast">
<input type="hidden" name="csrf" value="{{ csrf }}"><div class="field"><label>Текст уведомления</label>
<textarea name="body" maxlength="4000" required placeholder="Сообщение для подписавшихся пользователей"></textarea></div>
<button>Запустить кампанию →</button></form><p class="muted">Только opt-in подписчики. Интервал настраивается отдельно.</p></section>
<section class="card"><h2>СОСТОЯНИЕ СИСТЕМЫ</h2><p>Engine: <span class="pill">{{ state }}</span></p>
<p class="muted">CPU {{ cpu }}% · RAM {{ ram }}%</p><p class="muted">Активные кампании: {{ active }}</p>
<p class="muted">Uptime: {{ uptime }} сек.</p></section></div>
<section class="card panel"><h2>ПОСЛЕДНИЕ КАМПАНИИ</h2><table><tr><th>ID</th><th>СТАТУС</th><th>ОХВАТ</th><th>ДОСТАВЛЕНО</th><th>ОШИБКИ</th><th>ДАТА</th></tr>
{% for j in jobs %}<tr><td>{{ j.id }}</td><td>{{ j.status }}</td><td>{{ j.total }}</td><td>{{ j.sent }}</td><td>{{ j.failed }}</td><td>{{ j.created_at }}</td></tr>
{% else %}<tr><td colspan="6">Кампаний пока нет</td></tr>{% endfor %}</table></section>
""", stats=stats, subs=subs, accs=accs, jobs=jobs, csrf=csrf_token(),
        state=rt.state, cpu=psutil.cpu_percent(), ram=psutil.virtual_memory().percent,
        active=rt.active, uptime=int(time.time()-rt.started))
    return page("Дашборд", content)

@app.get("/accounts")
@login_required
def accounts_page():
    rows = submit(database.all("SELECT * FROM accounts WHERE owner_id=? ORDER BY id DESC", (uid(),))).result(10)
    content = render_template_string("""<section class="card"><h2>TELEGRAM ИНТЕГРАЦИИ</h2>
<p class="muted">Эта версия работает через Telegram Bot API. Вход в пользовательские аккаунты по номеру и коду не используется.</p>
<table><tr><th>ID</th><th>НАЗВАНИЕ</th><th>ТИП</th><th>СТАТУС</th><th>ДАТА</th></tr>
{% for r in rows %}<tr><td>{{ r.id }}</td><td>{{ r.label }}</td><td>{{ r.kind }}</td><td>{{ r.status }}</td><td>{{ r.created_at }}</td></tr>
{% else %}<tr><td colspan="5">Интеграции появятся здесь</td></tr>{% endfor %}</table></section>""", rows=rows)
    return page("Аккаунты", content)

@app.get("/config")
@login_required
def config_page():
    cfg = submit(database.config(uid())).result(10)
    content = render_template_string("""<div class="row"><section class="card"><h2>ПАРАМЕТРЫ ДВИЖКА</h2>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<div class="field"><label>Режим</label><select disabled><option selected>AUTO — ограниченная скорость</option></select></div>
<div class="field"><label>Интервал отправки (1–60 секунд)</label><input name="gap" type="number" min="1" max="60" step="0.5" value="{{ gap }}"></div>
<div class="field"><label>Шаблоны (одна строка — один шаблон, строго по очереди)</label>
<textarea name="templates" maxlength="30000">{{ templates }}</textarea></div><button>Сохранить</button></form></section>
<section class="card"><h2>ПОЛИТИКА ДОСТАВКИ</h2><p class="muted">Получатели должны самостоятельно включить подписку через /subscribe или меню бота.</p>
<p class="muted">При FloodWait платформа ждёт срок, указанный Telegram.</p><p class="muted">Маскировка текста и обход антиспам-фильтров не используются.</p></section></div>""",
        csrf=csrf_token(), gap=cfg["send_gap"], templates="\n".join(cfg["templates"]))
    return page("Конфигурация", content)

@app.post("/config")
@login_required
def config_save():
    templates = [x.strip() for x in request.form.get("templates", "").splitlines() if x.strip()]
    if len(templates) > MAX_TEMPLATES: abort(400, "Too many templates")
    gap = bounded_float(request.form.get("gap"))
    submit(database.save_config(uid(), gap, templates, [], "AUTO", 0, 1)).result(10)
    submit(database.audit(uid(), "config_updated", {"gap": gap, "templates": len(templates)})).result(10)
    return redirect("/config")

@app.get("/terminal")
@login_required
def terminal():
    logs = "\n".join(html.escape(line) for line in rt.logs)
    content = render_template_string("""<section class="card"><h2>LIVE SYSTEM LOG</h2>
<div class="terminal">{{ logs|safe }}</div></section><script>setTimeout(()=>location.reload(),5000)</script>""", logs=logs)
    return page("Терминал", content)

@app.get("/admin")
@admin_required
def admin():
    users = submit(database.all("SELECT * FROM users ORDER BY created_at DESC LIMIT 100")).result(10)
    audits = submit(database.all("SELECT * FROM audit ORDER BY id DESC LIMIT 30")).result(10)
    content = render_template_string("""<div class="row"><section class="card"><h2>ВЫДАТЬ БАЛАНС И ТАРИФ</h2>
<form method="post" action="/admin/grant"><input type="hidden" name="csrf" value="{{ csrf }}">
<div class="field"><label>Telegram user ID</label><input name="user_id" type="number" required></div>
<div class="field"><label>Изменение баланса</label><input name="balance" type="number" value="0"></div>
<div class="field"><label>Тариф</label><select name="plan"><option>FREE</option><option>PRO</option><option>ULTIMATE</option></select></div>
<button>Применить</button></form></section><section class="card"><h2>ADMIN CORE</h2>
<p class="muted">Администраторы задаются через SUPER_ADMINS. Все операции записываются в audit.</p></section></div>
<section class="card panel"><h2>ПОЛЬЗОВАТЕЛИ</h2><table><tr><th>ID</th><th>USERNAME</th><th>ИМЯ</th><th>SUB</th><th>PLAN</th><th>BALANCE</th></tr>
{% for u in users %}<tr><td>{{ u.user_id }}</td><td>{{ u.username }}</td><td>{{ u.first_name }}</td><td>{{ u.subscribed }}</td><td>{{ u.plan }}</td><td>{{ u.balance }}</td></tr>{% endfor %}</table></section>
<section class="card panel"><h2>AUDIT TRAIL</h2><table><tr><th>ACTOR</th><th>ACTION</th><th>DETAILS</th><th>DATE</th></tr>
{% for a in audits %}<tr><td>{{ a.actor_id }}</td><td>{{ a.action }}</td><td>{{ a.details }}</td><td>{{ a.created_at }}</td></tr>{% endfor %}</table></section>""",
        users=users, audits=audits, csrf=csrf_token())
    return page("Админка", content)

@app.post("/admin/grant")
@admin_required
def admin_grant():
    target = bounded_int(request.form.get("user_id"), 0, 1)
    delta = bounded_int(request.form.get("balance"), 0, 0, 1_000_000)
    plan = request.form.get("plan", "FREE")
    if plan not in {"FREE", "PRO", "ULTIMATE"}: abort(400)
    exists = submit(database.one("SELECT user_id FROM users WHERE user_id=?", (target,))).result(10)
    if not exists: abort(404, "User not found")
    submit(database.execute("UPDATE users SET balance=balance+?,plan=?,updated_at=? WHERE user_id=?",
                            (delta, plan, now(), target))).result(10)
    submit(database.audit(uid(), "admin_grant", {"target": target, "delta": delta, "plan": plan})).result(10)
    return redirect("/admin")

@app.post("/broadcast")
@admin_required
def create_broadcast():
    try: submit(queue_broadcast(uid(), request.form.get("body", ""))).result(10)
    except Exception as exc: abort(400, str(exc))
    return redirect("/")

@app.get("/health")
def health():
    return jsonify(app=APP_NAME, version=VERSION, state=rt.state, uptime=int(time.time()-rt.started))

@app.errorhandler(400)
def err400(error): return page("Ошибка запроса", f'<div class="notice">{html.escape(str(error))}</div>'), 400

@app.errorhandler(403)
def err403(error): return page("Доступ запрещён", '<div class="notice">403 // ACCESS DENIED</div>'), 403

@app.errorhandler(404)
def err404(error): return page("Не найдено", '<div class="notice">404 // NOT FOUND</div>'), 404

@app.errorhandler(500)
def err500(error):
    session.pop("_flashes", None)
    return page("Ошибка сервера", '<div class="notice">500 // INTERNAL SERVER ERROR</div>'), 500

def run_web():
    try: serve(app, host=HOST, port=PORT, threads=8, connection_limit=100)
    except Exception: log.exception("Waitress server failed")

# ------------------------------ Background tasks -----------------------------
async def worker():
    while not rt.stop.is_set():
        try: job = await asyncio.wait_for(rt.queue.get(), timeout=1)
        except asyncio.TimeoutError: continue
        try: await broadcast(job)
        except Exception as exc: rt.write(f"Worker exception: {exc}", "ERROR")
        finally: rt.queue.task_done()

async def queue_broadcast(uid, body):
    if uid not in SUPER_ADMINS: raise PermissionError("Administrator only")
    if not body.strip(): raise ValueError("Message is empty")
    if rt.queue.full(): raise RuntimeError("Queue is full")
    bid = uuid.uuid4().hex[:16]
    await database.execute("INSERT INTO broadcasts(id,owner_id,body,status,created_at) VALUES(?,?,?,'queued',?)",
                            (bid, uid, body[:4000], now()))
    await rt.queue.put({"id": bid, "owner_id": uid, "body": body})
    await database.audit(uid, "broadcast_queued", {"id": bid})
    return bid

async def broadcast(job):
    uid, bid = int(job["owner_id"]), job["id"]
    recipients = await database.all("SELECT user_id FROM users WHERE subscribed=1")
    total = len(recipients)
    await database.execute("UPDATE broadcasts SET status='running',total=? WHERE id=?", (total, bid))
    sent = failed = 0
    rt.active += 1
    try:
        for row in recipients:
            if rt.stop.is_set(): break
            try:
                await rt.bot.send_message(row["user_id"], job["body"])
                sent += 1
            except: failed += 1
            await asyncio.sleep(0.05)
        await database.execute("UPDATE broadcasts SET status='finished',sent=?,failed=?,finished_at=? WHERE id=?", (sent, failed, now(), bid))
    finally:
        rt.active -= 1

async def maintenance():
    while not rt.stop.is_set():
        try:
            await database.execute("DELETE FROM tokens WHERE expires_at<? OR used=1", (time.time()-3600,))
            await asyncio.sleep(300)
        except asyncio.CancelledError: raise
        except Exception as e: rt.write(f"Maintenance: {e}", "WARNING"); await asyncio.sleep(30)

async def shutdown(tasks):
    rt.state = "SHUTTING_DOWN"
    rt.stop.set()
    for task in tasks: task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception): await task
    if rt.bot:
        with contextlib.suppress(Exception): await rt.bot.disconnect()
    rt.state = "OFFLINE"

async def main():
    rt.loop = asyncio.get_running_loop()
    rt.semaphore = asyncio.Semaphore(MAX_CLIENTS)
    rt.queue = asyncio.Queue(maxsize=100)
    rt.stop = asyncio.Event()
    await database.init()
    rt.http = aiohttp.ClientSession(headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
    await telegram_start()
    threading.Thread(target=run_web, name="titan-waitress", daemon=True).start()
    rt.write(f"{APP_NAME} {VERSION} initialized")
    tasks = [asyncio.create_task(worker()), asyncio.create_task(maintenance())]
    try: await rt.bot.run_until_disconnected()
    except KeyboardInterrupt: rt.write("Shutdown requested", "WARNING")
    except Exception as exc: rt.write(f"Main loop failed: {exc}", "ERROR")
    finally: await shutdown(tasks)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
    except Exception: log.exception("Fatal startup error")
