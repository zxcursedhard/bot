#!/usr/bin/env python3
"""
TITAN Enterprise v20.0 — Secure Auth + Chat Picker
Single-file high-performance Telegram automation platform.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
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

import aiosqlite
import aiohttp
import psutil
from flask import Flask, abort, jsonify, request, session, Response, redirect
from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession
from waitress import serve

# ------------------------------- Configuration -------------------------------
API_ID = int(os.getenv("API_ID", "2040") or "2040")
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8732669492:AAGjBx_kMW4JRpo4DyhCoRwAISSS-lJ1IhE")
FLASK_SECRET = os.getenv("FLASK_SECRET", secrets.token_urlsafe(48))
DB_PATH = os.getenv("DATABASE_PATH", "titan_omnicore.db")
HOST, PORT = os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "10000"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
SUPER_ADMINS = {int(x) for x in os.getenv("SUPER_ADMINS", "8655682794,8773171051").split(",") if x.strip().isdigit()}
COOKIE_DAYS = 14
TOKEN_TTL = 86400  # 24 часа

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("titan")

# --------------------------------- Utilities ----------------------------------
def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def digest(v): return hashlib.sha256(v.encode()).hexdigest()
def jdump(v): return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
def jload(v, d):
    try: return json.loads(v or "")
    except: return d

def parse_targets(text):
    out = []
    for line in text.splitlines():
        x = line.strip()
        if not x: continue
        if x.startswith("tg://chat?id="):
            try: out.append(int(x.split("=")[1])); continue
            except: pass
        m = re.match(r"^(?:https?://)?t\.me/c/(\d+)", x)
        if m: out.append(int("-100" + m.group(1))); continue
        if re.match(r"^-?\d+$", x): out.append(int(x))
        elif x.startswith("@"): out.append(x)
        elif re.match(r"^https?://t\.me/", x): out.append("@" + x.split("/")[-1])
    return out

# ------------------------------- Database --------------------------------
class Database:
    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()
    async def connect(self):
        conn = await aiosqlite.connect(self.path, timeout=30)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        return conn
    async def execute(self, sql, params=()):
        async with self.lock:
            conn = await self.connect()
            try:
                cur = await conn.execute(sql, params)
                await conn.commit()
                return cur.rowcount
            finally: await conn.close()
    async def one(self, sql, params=()):
        async with self.lock:
            conn = await self.connect()
            try:
                cur = await conn.execute(sql, params)
                r = await cur.fetchone()
                return dict(r) if r else None
            finally: await conn.close()
    async def all(self, sql, params=()):
        async with self.lock:
            conn = await self.connect()
            try:
                cur = await conn.execute(sql, params)
                return [dict(r) for r in await cur.fetchall()]
            finally: await conn.close()

    async def init(self):
        async with self.lock:
            conn = await self.connect()
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '', subscribed INTEGER DEFAULT 0, balance INTEGER DEFAULT 0, plan TEXT DEFAULT 'FREE', created_at TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, session TEXT, phone TEXT, status TEXT DEFAULT 'active', created_at TEXT);
                CREATE TABLE IF NOT EXISTS configs(owner_id INTEGER PRIMARY KEY, mode TEXT DEFAULT 'AUTO', send_gap REAL DEFAULT 3, templates TEXT DEFAULT '[]', targets TEXT DEFAULT '[]', randomize INTEGER DEFAULT 0, warmup INTEGER DEFAULT 1, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS stats(owner_id INTEGER PRIMARY KEY, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS tokens(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, token_hash TEXT UNIQUE, expires_at REAL, used INTEGER DEFAULT 0, created_at TEXT);
            """)
            await conn.commit()
            await conn.close()

    async def ensure_user(self, uid, username="", first_name=""):
        stamp = now()
        await self.execute("INSERT OR IGNORE INTO users(user_id,username,first_name,created_at,updated_at) VALUES(?,?,?,?,?)", (uid, username, first_name, stamp, stamp))
        await self.execute("INSERT OR IGNORE INTO configs(owner_id,updated_at) VALUES(?,?)", (uid, stamp))
        await self.execute("INSERT OR IGNORE INTO stats(owner_id,updated_at) VALUES(?,?)", (uid, stamp))

    async def get_config(self, uid):
        row = await self.one("SELECT * FROM configs WHERE owner_id=?", (uid,))
        if not row:
            await self.execute("INSERT OR IGNORE INTO configs(owner_id,updated_at) VALUES(?,?)", (uid, now()))
            row = await self.one("SELECT * FROM configs WHERE owner_id=?", (uid,))
        row["templates"] = jload(row["templates"], [])
        row["targets"] = jload(row["targets"], [])
        return row

    async def save_config(self, uid, gap, templates, targets, mode, randomize, warmup):
        await self.execute("UPDATE configs SET send_gap=?,templates=?,targets=?,mode=?,randomize=?,warmup=?,updated_at=? WHERE owner_id=?",
                           (gap, jdump(templates), jdump(targets), mode, int(randomize), int(warmup), now(), uid))

database = Database(DB_PATH)

# ------------------------------ Runtime ------------------------------
class Runtime:
    def __init__(self):
        self.loop = None
        self.bot = None
        self.started = time.time()
        self.logs = deque(maxlen=200)
        self.active_engines = {}

rt = Runtime()
def submit(coro):
    return asyncio.run_coroutine_threadsafe(coro, rt.loop)

# ------------------------------ Bot ------------------------------
async def on_command(event):
    uid = int(event.sender_id)
    sender = await event.get_sender()
    await database.ensure_user(uid, getattr(sender, "username", "") or "", getattr(sender, "first_name", "") or "")
    
    if event.raw_text.startswith("/start"):
        await event.respond("TITAN Enterprise", buttons=[[Button.inline("🌐 Войти в панель", b"login")]])
    elif event.raw_text.startswith("/panel"):
        if uid not in SUPER_ADMINS: return await event.respond("Доступ запрещён.")
        raw = secrets.token_urlsafe(32)
        await database.execute("INSERT INTO tokens(owner_id,token_hash,expires_at,created_at) VALUES(?,?,?,?)", (uid, digest(raw), time.time()+TOKEN_TTL, now()))
        await event.respond(f"Ссылка (24 часа):\n{PUBLIC_BASE_URL}/login?token={raw}")

async def on_callback(event):
    if event.data == b"login":
        uid = int(event.sender_id)
        raw = secrets.token_urlsafe(32)
        await database.execute("INSERT INTO tokens(owner_id,token_hash,expires_at,created_at) VALUES(?,?,?,?)", (uid, digest(raw), time.time()+TOKEN_TTL, now()))
        await event.respond(f"Ссылка (24 часа):\n{PUBLIC_BASE_URL}/login?token={raw}")
        await event.answer()

async def telegram_start():
    rt.bot = TelegramClient(StringSession(), API_ID, API_HASH)
    await rt.bot.start(bot_token=BOT_TOKEN)
    rt.bot.add_event_handler(on_command, events.NewMessage(incoming=True, pattern=r"^/(start|panel)"))
    rt.bot.add_event_handler(on_callback, events.CallbackQuery())

# ------------------------------ Engine ------------------------------
class SpamEngine:
    def __init__(self, uid, config, accounts):
        self.uid = uid
        self.config = config
        self.accounts = accounts
        self.stop_event = asyncio.Event()
        self.t_queue = deque(config["templates"])

    def _next_tpl(self):
        if not self.t_queue: return "..."
        t = self.t_queue.popleft()
        self.t_queue.append(t)
        return t

    async def start(self):
        tasks = [asyncio.create_task(self._worker(acc)) for acc in self.accounts]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _worker(self, acc):
        client = TelegramClient(StringSession(acc["session"]), API_ID, API_HASH)
        await client.connect()
        if self.config["warmup"]:
            try:
                dialogs = await client.get_dialogs(limit=3)
                for d in dialogs: await client.get_messages(d.id, limit=1)
            except: pass
        
        while not self.stop_event.is_set():
            for target in self.config["targets"]:
                if self.stop_event.is_set(): break
                try:
                    entity = target
                    if isinstance(target, int):
                        try: entity = await client.get_entity(target)
                        except: pass
                    await client.send_message(entity, self._next_tpl())
                    await database.execute("UPDATE stats SET sent=sent+1 WHERE owner_id=?", (self.uid,))
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                except Exception:
                    await database.execute("UPDATE stats SET failed=failed+1 WHERE owner_id=?", (self.uid,))
                
                delay = 0.05 if self.config["mode"] == "EXTREME" else self.config["send_gap"]
                if delay > 0:
                    try: await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    except: pass
        await client.disconnect()

# ------------------------------ Web ------------------------------
app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=COOKIE_DAYS*86400)

HTML = r"""
<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>TITAN Enterprise</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#050505;--panel:#111;--line:#222;--txt:#eee;--muted:#666;--red:#ff003c;--cyan:#00f0ff}
*{box-sizing:border-box;margin:0;padding:0}body{background:var(--bg);color:var(--txt);font:14px 'Inter',sans-serif;display:flex;min-height:100vh}
.side{width:220px;background:var(--panel);border-right:1px solid var(--line);padding:20px;display:flex;flex-direction:column}
.logo{font-weight:800;font-size:18px;margin-bottom:30px;letter-spacing:1px}.logo span{color:var(--red)}
.nav a{display:block;padding:12px;color:var(--muted);text-decoration:none;border-radius:8px;cursor:pointer;margin-bottom:5px;transition:0.2s}
.nav a:hover,.nav a.active{background:#222;color:#fff}
.main{flex:1;padding:20px;overflow-y:auto}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
h1{font-size:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:15px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px}
.metric{font-size:28px;font-weight:800;color:var(--red);margin-top:10px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:20px}
input,textarea,select{width:100%;background:#000;border:1px solid var(--line);color:#fff;padding:12px;border-radius:8px;margin-bottom:10px;font-family:inherit}
button{width:100%;padding:12px;background:var(--red);color:#fff;border:none;border-radius:8px;font-weight:800;cursor:pointer;margin-bottom:10px}
button.gray{background:#222}
button.cyan{background:var(--cyan);color:#000}
.chat-list{max-height:300px;overflow-y:auto;border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:15px;background:#000}
.chat-item{display:flex;align-items:center;gap:10px;padding:8px;border-bottom:1px solid #111}
.chat-item input{width:auto;margin:0}
.hidden{display:none!important}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line)}
@media(max-width:768px){body{flex-direction:column}.side{width:100%;border-right:none;border-bottom:1px solid var(--line);padding:10px}.nav{display:flex;overflow-x:auto;gap:5px}.nav a{white-space:nowrap;padding:8px 12px}.logo{margin-bottom:10px}.main{padding:15px}}
</style></head><body>
<div class="side">
<div class="logo">TITAN <span>ENTERPRISE</span></div>
<div class="nav">
<a class="active" onclick="tab('dash')">Обзор</a>
<a onclick="tab('acc')">Аккаунты</a>
<a onclick="tab('chats')">Мои чаты</a>
<a onclick="tab('cfg')">Настройки</a>
<a id="admin-btn" class="hidden" onclick="tab('adm')">Админка</a>
<a href="/logout">Выход</a>
</div>
</div>
<div class="main">
<div class="header"><h1 id="title">Обзор</h1><span id="status" style="color:var(--cyan)">ONLINE</span></div>

<div id="tab-dash" class="tab-content">
<div class="grid">
<div class="card"><div>Отправлено</div><div class="metric" id="m-sent">0</div></div>
<div class="card"><div>Ошибки</div><div class="metric" id="m-err">0</div></div>
<div class="card"><div>Аккаунты</div><div class="metric" id="m-acc">0</div></div>
</div>
<div class="panel">
<h3>Статус движка</h3>
<p id="eng-status" style="margin-top:10px;font-weight:800;">ОСТАНОВЛЕН</p>
</div>
</div>

<div id="tab-acc" class="tab-content hidden">
<div class="panel">
<h3>Добавить аккаунт</h3>
<input id="acc-phone" placeholder="+79990000000">
<button onclick="startAuth()">Получить код</button>
<div id="acc-code-div" class="hidden">
<input id="acc-code" placeholder="Код">
<button onclick="verifyAuth()">Войти</button>
</div>
</div>
</div>

<div id="tab-chats" class="tab-content hidden">
<div class="panel">
<h3>Выберите чаты для рассылки</h3>
<button class="gray" onclick="loadChats()">Обновить список</button>
<div class="chat-list" id="chat-list">Нажмите "Обновить список"</div>
<button class="cyan" onclick="saveSelectedChats()">Сохранить выбранные в цели</button>
</div>
</div>

<div id="tab-cfg" class="tab-content hidden">
<div class="panel">
<h3>Режим</h3>
<select id="cfg-mode"><option value="AUTO">AUTO</option><option value="EXTREME">EXTREME</option></select>
<h3>Задержка (сек)</h3><input id="cfg-delay" type="number" step="0.1">
<h3>Прогрев</h3><select id="cfg-warmup"><option value="1">Вкл</option><option value="0">Выкл</option></select>
<button onclick="saveCfg()">Сохранить</button>
</div>
<div class="panel"><h3>Шаблоны</h3><textarea id="cfg-tpl" rows="3"></textarea><button onclick="saveTpl()">Сохранить</button></div>
<div class="panel"><h3>Цели (вручную)</h3><textarea id="cfg-tgt" rows="3"></textarea><button onclick="saveTgt()">Сохранить</button></div>
<button onclick="engine('start')" class="cyan">ЗАПУСТИТЬ</button>
<button onclick="engine('stop')" class="gray">ОСТАНОВИТЬ</button>
</div>

<div id="tab-adm" class="tab-content hidden">
<div class="panel">
<h3>Пользователи</h3>
<table id="adm-table"></table>
</div>
</div>

</div>
<script>
let csrf="";
function tab(id){
    document.querySelectorAll('.tab-content').forEach(x=>x.classList.add('hidden'));
    document.getElementById('tab-'+id).classList.remove('hidden');
    document.querySelectorAll('.nav a').forEach(x=>x.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('title').textContent=event.target.textContent;
}
async function api(url,opts={}){
    const r=await fetch(url,{...opts,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...opts.headers}});
    if(r.status===401){location.href='/';return;}
    const d=await r.json();
    if(!r.ok) throw new Error(d.error||'Error');
    return d;
}
async function load(){
    try{
        const u=await api('/api/me'); csrf=u.csrf;
        document.getElementById('m-sent').textContent=u.stats.sent||0;
        document.getElementById('m-err').textContent=u.stats.failed||0;
        document.getElementById('m-acc').textContent=u.accounts;
        document.getElementById('eng-status').textContent=u.running?'РАБОТАЕТ':'ОСТАНОВЛЕН';
        document.getElementById('cfg-mode').value=u.mode;
        document.getElementById('cfg-delay').value=u.gap;
        document.getElementById('cfg-warmup').value=u.warmup;
        document.getElementById('cfg-tpl').value=u.templates.join('\n');
        document.getElementById('cfg-tgt').value=u.targets.join('\n');
        if(u.admin){
            document.getElementById('admin-btn').classList.remove('hidden');
            const d=await api('/api/admin/users');
            document.getElementById('adm-table').innerHTML=d.users.map(x=>`<tr><td>${x.user_id}</td><td>${x.balance}₽</td></tr>`).join('');
        }
    }catch(e){console.error(e)}
}
async function startAuth(){
    const r=await api('/api/auth/start',{method:'POST',body:JSON.stringify({phone:document.getElementById('acc-phone').value})});
    if(r.ok) document.getElementById('acc-code-div').classList.remove('hidden');
}
async function verifyAuth(){
    const r=await api('/api/auth/verify',{method:'POST',body:JSON.stringify({code:document.getElementById('acc-code').value})});
    if(r.ok) { alert('Аккаунт добавлен!'); load(); }
}
async function saveCfg(){
    await api('/api/cfg',{method:'POST',body:JSON.stringify({mode:document.getElementById('cfg-mode').value,delay:parseFloat(document.getElementById('cfg-delay').value),warmup:parseInt(document.getElementById('cfg-warmup').value)})});
    alert('Сохранено');
}
async function saveTpl(){
    await api('/api/tpl',{method:'POST',body:JSON.stringify({text:document.getElementById('cfg-tpl').value})});
    alert('Сохранено');
}
async function saveTgt(){
    await api('/api/tgt',{method:'POST',body:JSON.stringify({text:document.getElementById('cfg-tgt').value})});
    alert('Сохранено');
}
async function engine(action){
    await api('/api/engine/'+action,{method:'POST'});
    load();
}
async function loadChats(){
    document.getElementById('chat-list').innerHTML='Загрузка...';
    try {
        const r = await api('/api/chats');
        if(r.chats.length === 0) {
            document.getElementById('chat-list').innerHTML='Нет доступных чатов.';
            return;
        }
        document.getElementById('chat-list').innerHTML = r.chats.map(c => `
            <div class="chat-item">
                <input type="checkbox" value="${c.id}" id="chat_${c.id}">
                <label for="chat_${c.id}">${c.title} (${c.type})</label>
            </div>
        `).join('');
    } catch(e) {
        document.getElementById('chat-list').innerHTML='Ошибка: ' + e.message;
    }
}
async function saveSelectedChats(){
    const checkboxes = document.querySelectorAll('#chat-list input[type="checkbox"]:checked');
    const selected = Array.from(checkboxes).map(cb => cb.value);
    if(selected.length === 0) return alert('Ничего не выбрано');
    await api('/api/tgt/add', {method:'POST', body:JSON.stringify({targets: selected})});
    alert('Добавлено в цели!');
    load();
}
load(); setInterval(load, 5000);
</script></body></html>
"""

@app.route("/login")
def login():
    token = request.args.get("token", "")
    if not token:
        return "Токен отсутствует."
    
    row = submit(database.one("SELECT * FROM tokens WHERE token_hash=? AND used=0 AND expires_at>?", (digest(token), time.time()))).result()
    if not row:
        return "Ссылка истекла или недействительна. Запросите новую в боте (/panel)."
    
    # Помечаем как использованный (но он живет 24 часа, так что можно зайти по нему только раз)
    submit(database.execute("UPDATE tokens SET used=1 WHERE id=?", (row["id"],))).result()
    
    session.permanent = True
    session["user_id"] = row["owner_id"]
    session["csrf"] = secrets.token_urlsafe(32)
    return redirect("/")

@app.route("/")
def index():
    if "user_id" not in session: return "Пожалуйста, авторизуйтесь через бота (/panel)"
    return HTML

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

def get_u():
    uid = session.get("user_id")
    if not uid: return None
    return submit(database.one("SELECT * FROM users WHERE user_id=?", (uid,))).result()

def login_req(f):
    @wraps(f)
    def w(*a, **kw):
        if not get_u(): return jsonify({"error": "401"}), 401
        return f(*a, **kw)
    return w

@app.route("/api/me")
@login_req
def api_me():
    u = get_u()
    cfg = submit(database.get_config(u["user_id"])).result()
    accs = submit(database.all("SELECT id FROM accounts WHERE owner_id=?", (u["user_id"],))).result()
    stats = submit(database.one("SELECT sent,failed FROM stats WHERE owner_id=?", (u["user_id"],))).result() or {}
    return jsonify({
        "csrf": session.get("csrf"), "admin": u["user_id"] in SUPER_ADMINS,
        "accounts": len(accs), "running": u["user_id"] in rt.active_engines,
        "mode": cfg["mode"], "gap": cfg["send_gap"], "warmup": cfg["warmup"],
        "templates": cfg["templates"], "targets": cfg["targets"], "stats": stats
    })

@app.route("/api/chats")
@login_req
def api_chats():
    uid = get_u()["user_id"]
    accs = submit(database.all("SELECT * FROM accounts WHERE owner_id=?", (uid,))).result()
    if not accs: return jsonify({"error": "Нет аккаунтов"}), 400

    async def _get_chats():
        client = TelegramClient(StringSession(accs[0]["session"]), API_ID, API_HASH)
        await client.connect()
        dialogs = await client.get_dialogs(limit=100)
        res = []
        for d in dialogs:
            if d.is_group or d.is_channel:
                ctype = "Канал" if d.is_channel else "Группа"
                res.append({"id": str(d.id), "title": d.name, "type": ctype})
        await client.disconnect()
        return res
    try:
        chats = submit(_get_chats()).result(30)
        return jsonify({"chats": chats})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/tgt/add", methods=["POST"])
@login_req
def api_tgt_add():
    uid = get_u()["user_id"]
    new_targets = request.json.get("targets", [])
    cfg = submit(database.get_config(uid)).result()
    existing = cfg["targets"]
    for t in new_targets:
        if t not in existing: existing.append(t)
    submit(database.save_config(uid, cfg["send_gap"], cfg["templates"], existing, cfg["mode"], 0, cfg["warmup"])).result()
    return jsonify({"ok": True})

@app.route("/api/auth/start", methods=["POST"])
@login_req
def api_auth_start():
    uid = get_u()["user_id"]
    phone = request.json.get("phone")
    if not re.match(r"^\+\d{10,15}$", phone): return jsonify({"error": "Формат +7999..."}), 400
    async def _s():
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        s = await c.send_code_request(phone)
        rt.active_engines[f"auth_{uid}"] = {"c": c, "phone": phone, "hash": s.phone_code_hash}
        return True
    try: submit(_s()).result(20); return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)[:50]}), 400

@app.route("/api/auth/verify", methods=["POST"])
@login_req
def api_auth_verify():
    uid = get_u()["user_id"]
    code = request.json.get("code")
    f = rt.active_engines.get(f"auth_{uid}")
    if not f: return jsonify({"error": "Expired"}), 400
    async def _v():
        try:
            await f["c"].sign_in(phone=f["phone"], code=code, phone_code_hash=f["hash"])
            sess = f["c"].session.save()
            await database.execute("INSERT INTO accounts(owner_id,session,phone,created_at) VALUES(?,?,?,?)", (uid, sess, f["phone"], now()))
            await f["c"].disconnect()
            return {"ok": True}
        except Exception as e: return {"error": str(e)[:50]}
    res = submit(_v()).result(20)
    rt.active_engines.pop(f"auth_{uid}", None)
    return jsonify(res)

@app.route("/api/cfg", methods=["POST"])
@login_req
def api_cfg():
    uid = get_u()["user_id"]
    d = request.json
    cfg = submit(database.get_config(uid)).result()
    submit(database.save_config(uid, d["delay"], cfg["templates"], cfg["targets"], d["mode"], 0, d["warmup"])).result()
    return jsonify({"ok": True})

@app.route("/api/tpl", methods=["POST"])
@login_req
def api_tpl():
    uid = get_u()["user_id"]
    v = list(dict.fromkeys(x.strip() for x in request.json.get("text","").splitlines() if x.strip()))
    cfg = submit(database.get_config(uid)).result()
    submit(database.save_config(uid, cfg["send_gap"], v, cfg["targets"], cfg["mode"], 0, cfg["warmup"])).result()
    return jsonify({"ok": True})

@app.route("/api/tgt", methods=["POST"])
@login_req
def api_tgt():
    uid = get_u()["user_id"]
    v = parse_targets(request.json.get("text",""))
    cfg = submit(database.get_config(uid)).result()
    submit(database.save_config(uid, cfg["send_gap"], cfg["templates"], v, cfg["mode"], 0, cfg["warmup"])).result()
    return jsonify({"ok": True})

@app.route("/api/engine/<action>", methods=["POST"])
@login_req
def api_engine(action):
    uid = get_u()["user_id"]
    if action == "start":
        if uid in rt.active_engines: return jsonify({"error": "Running"})
        accs = submit(database.all("SELECT * FROM accounts WHERE owner_id=?", (uid,))).result()
        if not accs: return jsonify({"error": "No accounts"}), 400
        cfg = submit(database.get_config(uid)).result()
        engine = SpamEngine(uid, cfg, accs)
        rt.active_engines[uid] = engine
        submit(engine.start())
    else:
        if uid in rt.active_engines:
            rt.active_engines[uid].stop_event.set()
            del rt.active_engines[uid]
    return jsonify({"ok": True})

@app.route("/api/admin/users")
@login_req
def api_admin_users():
    if get_u()["user_id"] not in SUPER_ADMINS: return jsonify({"error": "403"}), 403
    users = submit(database.all("SELECT user_id, balance FROM users LIMIT 100")).result()
    return jsonify({"users": users})

def run_web(): serve(app, host=HOST, port=PORT, threads=8)

async def main():
    rt.loop = asyncio.get_running_loop()
    await database.init()
    await telegram_start()
    threading.Thread(target=run_web, daemon=True).start()
    await rt.bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
