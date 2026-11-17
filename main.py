#!/usr/bin/env python3
"""
TITAN Enterprise v15.5 — True Omni-Core
Single-file high-performance Telegram automation platform.
Features: Multi-account, Extreme Mode, Warmup, Queue Rotation, Deep Analytics.
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
from flask import Flask, abort, jsonify, redirect, render_template_string, request, session
from telethon import TelegramClient, events, Button
from telethon.errors import (
    FloodWaitError, RPCError, SessionPasswordNeededError,
    ChatWriteForbiddenError, SlowModeWaitError
)
from telethon.sessions import StringSession
from waitress import serve

# ------------------------------- Configuration -------------------------------
APP_NAME, VERSION = "TITAN Enterprise", "15.5 True Omni-Core"
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

    async def backup(self):
        filename = os.path.join(BACKUP_DIR, f"titan_{datetime.now():%Y%m%d_%H%M%S}.db")
        async with self.lock:
            src = await self.connect()
            dst = await aiosqlite.connect(filename)
            try: await src.backup(dst)
            finally: await dst.close(); await src.close()
        return filename

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
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
                  SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=COOKIE_DAYS * 86400,
                  MAX_CONTENT_LENGTH=1024 * 1024)

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Inter:wght@400;600;800&display=swap');
:root{--bg:#050505;--panel:#0f0f12;--line:#222;--txt:#e0e0e0;--muted:#666;--primary:#ff003c;--accent:#00f0ff}
*{box-sizing:border-box;margin:0;padding:0}body{background:var(--bg);color:var(--txt);font:14px 'Inter',sans-serif;display:flex;height:100vh;overflow:hidden}
.sidebar{width:260px;background:var(--panel);border-right:1px solid var(--line);padding:30px;display:flex;flex-direction:column}
.logo{font-family:'Space Mono';font-size:24px;font-weight:700;color:#fff;margin-bottom:40px;border-bottom:1px solid var(--line);padding-bottom:20px}
.logo span{color:var(--primary)}
.nav-item{padding:12px 15px;border-radius:8px;color:var(--muted);cursor:pointer;transition:0.2s;font-weight:600;margin-bottom:5px;text-decoration:none;display:block}
.nav-item:hover,.nav-item.active{background:rgba(255,0,60,0.1);color:#fff;border-left:2px solid var(--primary)}
.main{flex:1;padding:40px;overflow-y:auto}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:30px}
h1{font-size:28px;font-weight:800;letter-spacing:-1px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px;margin-bottom:30px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:24px}
.metric{font-size:32px;font-family:'Space Mono';font-weight:700;margin-top:15px;color:var(--primary)}
.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:1px;font-weight:700}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:25px;margin-bottom:25px}
.panel h3{margin-bottom:15px;font-size:16px;color:var(--accent)}
textarea,input,select{width:100%;background:#000;border:1px solid var(--line);color:#fff;padding:12px;border-radius:8px;font-family:'Space Mono';font-size:12px;margin-bottom:15px}
textarea:focus,input:focus,select:focus{outline:none;border-color:var(--primary)}
button{padding:12px 20px;background:var(--primary);border:none;border-radius:8px;color:#fff;font-weight:700;cursor:pointer;transition:0.2s}
button:hover{opacity:0.8}
button.outline{background:transparent;border:1px solid var(--line);color:var(--txt)}
button.green{background:var(--accent);color:#000}
.hidden{display:none!important}
.terminal{background:#000;color:#00ff00;font-family:'Space Mono';padding:15px;border-radius:8px;height:400px;overflow-y:auto;font-size:12px;line-height:1.5}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:12px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--muted);font-size:11px;text-transform:uppercase}
"""

UI_HTML = r"""
<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN // Omni-Core</title><style>""" + CSS + r"""</style></head><body>
<div class="sidebar">
<div class="logo">TITAN<span>//15</span></div>
<a class="nav-item active" onclick="tab('dash')">Обзор</a>
<a class="nav-item" onclick="tab('acc')">Аккаунты</a>
<a class="nav-item" onclick="tab('cfg')">Конфигурация</a>
<a class="nav-item" onclick="tab('term')">Терминал</a>
{% if admin %}<a class="nav-item" onclick="tab('adm')">Админка</a>{% endif %}
<div style="margin-top:auto"><a class="nav-item" href="/logout">Выход</a></div>
</div>
<div class="main">
<div class="header"><h1 id="title">Обзор системы</h1><span class="label" style="color:var(--accent)">● ONLINE</span></div>

<div id="tab-dash" class="tab">
<div class="grid">
<div class="card"><div class="label">Отправлено</div><div class="metric" id="m-sent">0</div></div>
<div class="card"><div class="label">Ошибки</div><div class="metric" id="m-err" style="color:var(--accent)">0</div></div>
<div class="card"><div class="label">Аккаунтов</div><div class="metric" id="m-acc">0</div></div>
</div>
<div class="panel"><h3>Статус движка</h3><div id="eng-status">ОСТАНОВЛЕН</div></div>
</div>

<div id="tab-acc" class="tab hidden">
<div class="panel">
<h3>Добавить аккаунт</h3>
<input type="text" id="acc-phone" placeholder="+79990000000">
<button onclick="startAuth()">Получить код</button>
<div id="auth-code-div" class="hidden" style="margin-top:15px">
<input type="text" id="acc-code" placeholder="Код из Telegram">
<button onclick="verifyAuth()">Войти</button>
</div>
</div>
</div>

<div id="tab-cfg" class="tab hidden">
<div class="panel">
<h3>Режим</h3>
<select id="cfg-mode"><option value="AUTO">AUTO (Безопасный)</option><option value="EXTREME">EXTREME (Максимальная скорость)</option></select>
<h3>Задержка (сек)</h3><input type="number" id="cfg-delay" value="3" step="0.1">
<h3>Рандомизация букв</h3><select id="cfg-rand"><option value="0">Выкл</option><option value="1">Вкл</option></select>
<h3>Прогрев (Анти-бан)</h3><select id="cfg-warmup"><option value="1">Вкл</option><option value="0">Выкл</option></select>
<button onclick="saveCfg()">Сохранить</button>
</div>
<div class="panel"><h3>Шаблоны</h3><textarea id="cfg-tpl" rows="4"></textarea><button onclick="saveTpl()">Сохранить</button></div>
<div class="panel"><h3>Цели</h3><textarea id="cfg-tgt" rows="4"></textarea><button onclick="saveTgt()">Сохранить</button></div>
<button class="green" onclick="engine('start')" style="width:100%;margin-bottom:10px">🚀 ЗАПУСК</button>
<button onclick="engine('stop')" style="width:100%">🛑 СТОП</button>
</div>

<div id="tab-term" class="tab hidden">
<div class="panel"><h3>Логи</h3><div class="terminal" id="term-out"></div></div>
</div>

{% if admin %}
<div id="tab-adm" class="tab hidden">
<div class="panel">
<h3>Пользователи</h3>
<table><thead><tr><th>ID</th><th>Роль</th><th>Баланс</th><th>Подписка</th><th>Действия</th></tr></thead>
<tbody id="adm-rows"></tbody></table>
</div>
</div>
{% endif %}

</div>
<script>
let csrf="";
function tab(id){document.querySelectorAll('.tab').forEach(x=>x.classList.add('hidden'));document.getElementById('tab-'+id).classList.remove('hidden');document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('active'));event.target.classList.add('active');document.getElementById('title').textContent=event.target.textContent}
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...opts.headers}});if(r.status===401){location.href='/';return}const t=await r.text();try{const d=JSON.parse(t);if(!r.ok)throw new Error(d.error||r.status);return d}catch(e){alert(t);throw e}}
async function load(){
    try{
        const u=await api('/api/me'); csrf=u.csrf;
        document.getElementById('m-sent').textContent=u.stats.sent;
        document.getElementById('m-err').textContent=u.stats.failed;
        document.getElementById('m-acc').textContent=u.accounts;
        document.getElementById('eng-status').textContent=u.running?'РАБОТАЕТ':'ОСТАНОВЛЕН';
        document.getElementById('eng-status').style.color=u.running?'var(--accent)':'var(--muted)';
        document.getElementById('cfg-mode').value=u.mode;
        document.getElementById('cfg-delay').value=u.gap;
        document.getElementById('cfg-rand').value=u.randomize;
        document.getElementById('cfg-warmup').value=u.warmup;
        document.getElementById('cfg-tpl').value=u.templates.join('\n');
        document.getElementById('cfg-tgt').value=u.targets.join('\n');
        
        const logs=await api('/api/logs');
        document.getElementById('term-out').innerHTML=logs.logs.map(l=>`<div>${l}</div>`).join('');
        
        if(u.admin){
            const d=await api('/api/admin/users');
            document.getElementById('adm-rows').innerHTML=d.users.map(x=>`<tr><td>${x.user_id}</td><td>${x.plan}</td><td>${x.balance}₽</td><td>${x.subscribed?'Да':'Нет'}</td><td><button onclick="addBal(${x.user_id})">+Бал</button> <button onclick="addSub(${x.user_id})">+30д</button></td></tr>`).join('');
        }
    }catch(e){}
}
async function startAuth(){
    const phone=document.getElementById('acc-phone').value;
    const r=await api('/api/auth/start',{method:'POST',body:JSON.stringify({phone})});
    if(r.ok) document.getElementById('auth-code-div').classList.remove('hidden');
}
async function verifyAuth(){
    const code=document.getElementById('acc-code').value;
    const r=await api('/api/auth/verify',{method:'POST',body:JSON.stringify({code})});
    if(r.ok) alert('Аккаунт добавлен!'); load();
}
async function saveCfg(){await api('/api/cfg',{method:'POST',body:JSON.stringify({mode:document.getElementById('cfg-mode').value,delay:parseFloat(document.getElementById('cfg-delay').value),randomize:document.getElementById('cfg-rand').value,warmup:document.getElementById('cfg-warmup').value})});alert('Сохранено')}
async function saveTpl(){await api('/api/tpl',{method:'POST',body:JSON.stringify({text:document.getElementById('cfg-tpl').value})});alert('Сохранено')}
async function saveTgt(){await api('/api/tgt',{method:'POST',body:JSON.stringify({text:document.getElementById('cfg-tgt').value})});alert('Сохранено')}
async function engine(a){await api('/api/engine/'+a,{method:'POST'});load()}
async function addBal(id){const a=prompt("Сумма для начисления:");if(!a)return;await api('/api/admin/bal',{method:'POST',body:JSON.stringify({user_id:id,amount:parseInt(a)})});load()}
async function addSub(id){await api('/api/admin/sub',{method:'POST',body:JSON.stringify({user_id:id})});load()}
load();setInterval(load,5000);
</script></body></html>
"""

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

@app.before_request
def before_request(): check_csrf()

@app.get("/login")
def login():
    return render_template_string("""<!doctype html><html><head><title>TITAN Login</title><style>""" + CSS + """</style></head>
<body style="display:grid;place-items:center"><div class="card" style="width:400px;margin-top:20vh">
<h1>Вход в панель</h1><p class="label">Введите токен из бота</p>
<form method="post" action="/login"><input type="hidden" name="csrf" value="{{ csrf }}"><input name="token" placeholder="Токен" required><button>Войти</button></form>
</div></body></html>""", csrf=csrf_token())

@app.post("/login")
def login_post():
    raw = request.form.get("token", "").strip()
    record = submit(database.one("SELECT * FROM tokens WHERE token_hash=? AND used=0 AND expires_at>?", (digest(raw), time.time()))).result(10)
    if not record: return "Токен недействителен"
    submit(database.execute("UPDATE tokens SET used=1 WHERE id=?", (record["id"],))).result(10)
    session.clear()
    session.permanent = True
    session["user_id"] = int(record["owner_id"])
    session["csrf"] = secrets.token_urlsafe(32)
    submit(database.execute("INSERT INTO sessions(owner_id,session_id,expires_at,created_at,last_seen) VALUES(?,?,?,?,?)",
                            (record["owner_id"], session["csrf"], time.time()+COOKIE_DAYS*86400, now(), now()))).result(10)
    return redirect("/")

@app.get("/logout")
def logout():
    session.clear(); return redirect("/login")

@app.get("/")
@login_required
def dashboard():
    return render_template_string(UI_HTML, admin=uid() in SUPER_ADMINS)

# --- API ---
def get_u():
    value = session.get("user_id")
    if value is None: return None
    return submit(database.one("SELECT * FROM users WHERE user_id=?", (int(value),))).result(5)

@app.get("/api/me")
@login_required
def api_me():
    u = get_u()
    cfg = submit(database.config(uid())).result(5)
    accs = submit(database.all("SELECT id FROM accounts WHERE owner_id=?", (uid(),))).result(5)
    stats = submit(database.one("SELECT sent,failed FROM stats WHERE owner_id=?", (uid(),))).result(5) or {}
    return jsonify({
        "admin": uid() in SUPER_ADMINS, "accounts": len(accs),
        "running": uid() in rt.active_engines,
        "mode": cfg["mode"], "gap": cfg["send_gap"], "randomize": cfg["randomize"], "warmup": cfg["warmup"],
        "templates": cfg["templates"], "targets": cfg["targets"],
        "stats": stats, "csrf": csrf_token()
    })

@app.get("/api/logs")
@login_required
def api_logs():
    return jsonify({"logs": list(rt.logs)})

@app.post("/api/auth/start")
@login_required
def api_auth_start():
    data = request.get_json() or {}
    phone = data.get("phone", "")
    if not re.match(r"^\+\d{10,15}$", phone): return jsonify({"error":"Формат +7999..."}), 400
    async def _s():
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        s = await client.send_code_request(phone)
        rt.template_queues[f"auth_{uid()}"] = {"client": client, "phone": phone, "hash": s.phone_code_hash}
        return True
    try: submit(_s()).result(20); return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)[:50]}), 400

@app.post("/api/auth/verify")
@login_required
def api_auth_verify():
    code = (request.get_json() or {}).get("code", "")
    f = rt.template_queues.get(f"auth_{uid()}")
    if not f: return jsonify({"error":"Истекло"}), 400
    async def _v():
        try:
            await f["client"].sign_in(phone=f["phone"], code=code, phone_code_hash=f["hash"])
            sess = f["client"].session.save()
            await database.execute("INSERT INTO accounts(owner_id, session, phone, created_at) VALUES(?,?,?,?)", (uid(), sess, f["phone"], now()))
            await f["client"].disconnect()
            return {"ok": True}
        except Exception as e: return {"error":str(e)[:50]}
    res = submit(_v()).result(20)
    rt.template_queues.pop(f"auth_{uid()}", None)
    return jsonify(res)

@app.post("/api/cfg")
@login_required
def api_cfg():
    d = request.get_json() or {}
    cfg = submit(database.config(uid())).result(5)
    submit(database.save_config(uid(), d.get("delay", 3), cfg["templates"], cfg["targets"], d.get("mode", "AUTO"), d.get("randomize", 0), d.get("warmup", 1))).result(5)
    return jsonify({"ok":True})

@app.post("/api/tpl")
@login_required
def api_tpl():
    v = list(dict.fromkeys(x.strip() for x in (request.get_json() or {}).get("text","").splitlines() if x.strip()))
    cfg = submit(database.config(uid())).result(5)
    submit(database.save_config(uid(), cfg["send_gap"], v, cfg["targets"], cfg["mode"], cfg["randomize"], cfg["warmup"])).result(5)
    return jsonify({"ok":True})

@app.post("/api/tgt")
@login_required
def api_tgt():
    v = parse_targets((request.get_json() or {}).get("text",""))
    cfg = submit(database.config(uid())).result(5)
    submit(database.save_config(uid(), cfg["send_gap"], cfg["templates"], v, cfg["mode"], cfg["randomize"], cfg["warmup"])).result(5)
    return jsonify({"ok":True})

@app.post("/api/engine/<action>")
@login_required
def api_engine(action):
    if action == "start":
        if uid() in rt.active_engines: return jsonify({"error":"Уже запущено"})
        accs = submit(database.all("SELECT * FROM accounts WHERE owner_id=?", (uid(),))).result(5)
        if not accs: return jsonify({"error":"Нет аккаунтов"}), 400
        cfg = submit(database.config(uid())).result(5)
        engine = SpamEngine(uid(), cfg, accs)
        rt.active_engines[uid()] = engine
        submit(engine.start())
    else:
        if uid() in rt.active_engines:
            rt.active_engines[uid()].stop()
            del rt.active_engines[uid()]
    return jsonify({"ok":True})

@app.get("/api/admin/users")
@admin_required
def api_admin_users():
    users = submit(database.all("SELECT user_id,plan,balance,subscribed FROM users LIMIT 100")).result(5)
    return jsonify({"users": users})

@app.post("/api/admin/bal")
@admin_required
def api_admin_bal():
    d = request.get_json() or {}
    submit(database.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (d.get("amount",0), d.get("user_id",0)))).result(5)
    return jsonify({"ok":True})

@app.post("/api/admin/sub")
@admin_required
def api_admin_sub():
    d = request.get_json() or {}
    submit(database.execute("UPDATE users SET subscribed=1 WHERE user_id=?", (d.get("user_id",0),))).result(5)
    return jsonify({"ok":True})

# ------------------------------ Background tasks -----------------------------
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
    rt.queue = asyncio.Queue()
    rt.stop = asyncio.Event()
    await database.init()
    await telegram_start()
    threading.Thread(target=lambda: serve(app, host=HOST, port=PORT, threads=8), name="titan-waitress", daemon=True).start()
    rt.write(f"{APP_NAME} {VERSION} initialized")
    tasks = [asyncio.create_task(maintenance())]
    try:
        await rt.bot.run_until_disconnected()
    except KeyboardInterrupt: rt.write("Shutdown requested", "WARNING")
    except Exception as exc: rt.write(f"Main loop failed: {exc}", "ERROR")
    finally: await shutdown(tasks)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
    except Exception: log.exception("Fatal startup error")
