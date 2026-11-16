# =====================================================================
# TITAN Enterprise v15.1 — Omni-Core (Ultimate Monolith)
# =====================================================================
# Установка: pip install telethon flask waitress aiosqlite psutil aiohttp

import os, re, json, time, hmac, asyncio, secrets, hashlib, logging, threading, random
import datetime as dt
import urllib.request
from functools import wraps
from collections import deque, defaultdict
import aiosqlite
from flask import Flask, request, jsonify, redirect, render_template_string, session
from waitress import serve
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, FloodWaitError, SlowModeWaitError, 
    ChatWriteForbiddenError, UserBannedInChannelError, PeerFloodError,
    ChannelPrivateError, PhoneCodeInvalidError
)

try:
    import psutil
except ImportError:
    psutil = None

# -------------------- CONFIG --------------------
API_ID = int(os.getenv("API_ID", "2040"))
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8732669492:AAGjBx_kMW4JRpo4DyhCoRwAISSS-lJ1IhE")
FLASK_SECRET = os.getenv("FLASK_SECRET", "titan_v15_omnicore_secret")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "titan_v15.db")
WEB_PORT = int(os.getenv("PORT", "10000"))

SUPER_ADMINS = {int(x) for x in os.getenv("SUPER_ADMINS", "8655682794,8773171051").split(",") if x.strip().isdigit()}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] OMNI: %(message)s")
log = logging.getLogger("OMNI")

app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=False, SESSION_COOKIE_SAMESITE="Lax")

bot = None
db = None
loop = None
auth_flows = {}
system_logs = deque(maxlen=100)
active_engines = {}

def utcnow(): return dt.datetime.now(dt.timezone.utc)
def iso_now(): return utcnow().isoformat()
def is_admin(uid): return int(uid) in SUPER_ADMINS
def sha256(v): return hashlib.sha256(v.encode()).hexdigest()

def log_omni(uid, msg, level="INFO"):
    entry = {"time": dt.datetime.now().strftime("%H:%M:%S"), "uid": uid, "msg": msg, "level": level}
    system_logs.append(entry)
    getattr(log, level.lower(), log.info)(f"[{uid}] {msg}")

# -------------------- DATABASE --------------------
class OmniDB:
    def __init__(self, path): self.path = path
    def conn(self): return aiosqlite.connect(self.path, timeout=30)

    async def init(self):
        async with self.conn() as db:
            # Исправлено: PRAGMA выполняется по одной команде
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS users(
                    id INTEGER PRIMARY KEY, username TEXT, role TEXT DEFAULT 'USER',
                    approved INTEGER DEFAULT 0, balance INTEGER DEFAULT 0, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS accounts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, session TEXT, 
                    status TEXT DEFAULT 'active', phone TEXT, last_active TEXT
                );
                CREATE TABLE IF NOT EXISTS configs(
                    uid INTEGER PRIMARY KEY, templates TEXT DEFAULT '[]', targets TEXT DEFAULT '[]',
                    delay_ms INTEGER DEFAULT 10000, mode TEXT DEFAULT 'AUTO', 
                    randomize INTEGER DEFAULT 0, warmup INTEGER DEFAULT 1, 
                    auto_reply INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS stats(
                    uid INTEGER PRIMARY KEY, sent INTEGER DEFAULT 0, errors INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY, uid INTEGER, exp REAL);
                CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, uid INTEGER, exp REAL);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, val TEXT);
            """)
            await db.commit()
        
        for admin in SUPER_ADMINS:
            await self.ensure_user(admin, "admin")
            await self.set_user(admin, role="ADMIN", approved=1)

    async def get_user(self, uid):
        async with self.conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT u.*, c.templates, c.targets, c.delay_ms, c.mode, c.randomize, 
                c.warmup, c.auto_reply
                FROM users u LEFT JOIN configs c ON u.id = c.uid WHERE u.id=?
            """, (uid,)) as cur: return await cur.fetchone()

    async def ensure_user(self, uid, username=""):
        async with self.conn() as db:
            await db.execute("INSERT OR IGNORE INTO users(id, username, created_at) VALUES(?,?,?)", (uid, username, iso_now()))
            await db.execute("INSERT OR IGNORE INTO configs(uid) VALUES(?)", (uid,))
            await db.execute("INSERT OR IGNORE INTO stats(uid) VALUES(?)", (uid,))
            await db.commit()

    async def set_user(self, uid, **kwargs):
        allowed = {"role", "approved", "balance"}
        f = {k: v for k, v in kwargs.items() if k in allowed}
        if not f: return
        cols = ", ".join(f"{k}=?" for k in f)
        vals = list(f.values()) + [uid]
        async with self.conn() as db:
            await db.execute(f"UPDATE users SET {cols} WHERE id=?", vals)
            await db.commit()

    async def set_config(self, uid, **kwargs):
        allowed = {"templates", "targets", "delay_ms", "mode", "randomize", "warmup", "auto_reply"}
        f = {k: v for k, v in kwargs.items() if k in allowed}
        if not f: return
        cols = ", ".join(f"{k}=?" for k in f)
        vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in f.values()] + [uid]
        async with self.conn() as db:
            await db.execute(f"UPDATE configs SET {cols} WHERE uid=?", vals)
            await db.commit()

    async def get_accounts(self, uid):
        async with self.conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM accounts WHERE uid=?", (uid,)) as cur: 
                return await cur.fetchall()

    async def add_account(self, uid, session, phone):
        async with self.conn() as db:
            await db.execute("INSERT INTO accounts(uid, session, phone) VALUES(?,?,?)", (uid, session, phone))
            await db.commit()

    async def add_stat(self, uid, key):
        async with self.conn() as db:
            await db.execute(f"UPDATE stats SET {key}={key}+1 WHERE uid=?", (uid,))
            await db.commit()

    async def create_token(self, uid):
        raw = secrets.token_urlsafe(32)
        async with self.conn() as db:
            await db.execute("DELETE FROM tokens WHERE exp<?", (time.time(),))
            await db.execute("INSERT INTO tokens VALUES(?,?,?)", (sha256(raw), uid, time.time()+3600))
            await db.commit()
        return raw

    async def verify_token(self, raw):
        async with self.conn() as db:
            async with db.execute("SELECT uid, exp FROM tokens WHERE token=?", (sha256(raw),)) as cur:
                r = await cur.fetchone()
                return r[0] if r and time.time() < r[1] else None

    async def create_session(self, uid):
        raw = secrets.token_urlsafe(40)
        async with self.conn() as db:
            await db.execute("DELETE FROM sessions WHERE exp<?", (time.time(),))
            await db.execute("INSERT INTO sessions VALUES(?,?,?)", (sha256(raw), uid, time.time()+14*86400))
            await db.commit()
        return raw

    async def get_session(self, raw):
        if not raw: return None
        async with self.conn() as db:
            async with db.execute("SELECT uid FROM sessions WHERE sid=? AND exp>?", (sha256(raw), time.time())) as cur:
                r = await cur.fetchone()
                return r[0] if r else None

# -------------------- OMNICORE ENGINE --------------------
class OmniEngine:
    def __init__(self, uid, config, accounts):
        self.uid = uid
        self.config = config
        self.accounts = accounts
        self.running = False
        self.stop_event = asyncio.Event()
        self.templates = json.loads(config["templates"] or "[]")
        self.targets = json.loads(config["targets"] or "[]")
        self.t_queue = deque(self.templates)

    def _rand_text(self, t):
        if not self.config["randomize"]: return t
        repl = {'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 'х': 'x'}
        return ''.join(repl.get(c, c) for c in t)

    def _next_tpl(self):
        if not self.t_queue: return "..."
        t = self.t_queue.popleft()
        self.t_queue.append(t)
        return self._rand_text(t)

    async def _warmup(self, client):
        if not self.config["warmup"]: return
        log_omni(self.uid, "Запуск прогрева аккаунта...")
        try:
            dialogs = await client.get_dialogs(limit=5)
            for d in dialogs:
                await client.get_messages(d.id, limit=2)
                await asyncio.sleep(random.uniform(1, 2))
            log_omni(self.uid, "Прогрев завершен.")
        except Exception as e:
            log_omni(self.uid, f"Ошибка прогрева: {e}", "WARNING")

    async def start(self):
        self.running = True
        tasks = [asyncio.create_task(self._worker(acc)) for acc in self.accounts]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.running = False

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
                    await db.add_stat(self.uid, "sent")
                except FloodWaitError as e:
                    log_omni(self.uid, f"FloodWait {e.seconds}s", "WARNING")
                    await asyncio.sleep(e.seconds + 2)
                except Exception as e:
                    await db.add_stat(self.uid, "errors")
                    log_omni(self.uid, f"Error: {e}", "ERROR")
                
                delay = 0.05 if self.config["mode"] == "EXTREME" else self.config["delay_ms"]/1000.0
                if delay > 0:
                    try: await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError: pass
        await client.disconnect()

    def stop(self):
        self.stop_event.set()

# -------------------- BOT --------------------
async def setup_bot():
    global bot
    bot = TelegramClient("titan_v15_bot", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)

    @bot.on(events.NewMessage(pattern=r"^/start$"))
    async def start(e):
        await db.ensure_user(e.sender_id)
        u = await db.get_user(e.sender_id)
        if not u["approved"] and not is_admin(e.sender_id):
            return await e.respond(f"⛔ Доступ закрыт. ID: {e.sender_id}")
        await e.respond("TITAN v15 Omni-Core", buttons=[[Button.inline("🚀 Панель", b"login")]])

    @bot.on(events.CallbackQuery)
    async def cb(e):
        if e.data == b"login":
            token = await db.create_token(e.sender_id)
            await e.respond(f"🔗 {PUBLIC_BASE_URL}/login?t={token}")
            await e.answer()

# -------------------- WEB UI --------------------
UI_HTML = r"""
<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN v15 // Omni-Core</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#050505;--panel:#0f0f12;--line:#222;--txt:#e0e0e0;--muted:#666;--primary:#ff003c;--accent:#00f0ff}
*{box-sizing:border-box;margin:0;padding:0}body{background:var(--bg);color:var(--txt);font:14px 'Inter',sans-serif;display:flex;height:100vh;overflow:hidden}
.sidebar{width:260px;background:var(--panel);border-right:1px solid var(--line);padding:30px;display:flex;flex-direction:column}
.logo{font-family:'Space Mono';font-size:24px;font-weight:700;color:#fff;margin-bottom:40px;border-bottom:1px solid var(--line);padding-bottom:20px}
.logo span{color:var(--primary)}
.nav-item{padding:12px 15px;border-radius:8px;color:var(--muted);cursor:pointer;transition:0.2s;font-weight:600;margin-bottom:5px}
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
.terminal{background:#000;color:#00ff00;font-family:'Space Mono';padding:15px;border-radius:8px;height:300px;overflow-y:auto;font-size:12px;line-height:1.5}
</style></head><body>
<div class="sidebar">
<div class="logo">TITAN<span>//15</span></div>
<div class="nav-item active" onclick="tab('dash')">Обзор</div>
<div class="nav-item" onclick="tab('acc')">Аккаунты</div>
<div class="nav-item" onclick="tab('cfg')">Конфигурация</div>
<div class="nav-item" onclick="tab('term')">Терминал</div>
<div style="margin-top:auto"><div class="nav-item" onclick="location.href='/logout'">Выход</div></div>
</div>
<div class="main">
<div class="header"><h1 id="title">Обзор системы</h1></div>

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
<input type="text" id="acc-code" placeholder="Код">
<button onclick="verifyAuth()">Войти</button>
</div>
</div>
</div>

<div id="tab-cfg" class="tab hidden">
<div class="panel">
<h3>Режим</h3>
<select id="cfg-mode"><option value="AUTO">AUTO</option><option value="EXTREME">EXTREME</option></select>
<h3>Задержка (мс)</h3><input type="number" id="cfg-delay" value="10000">
<h3>Рандомизация</h3><select id="cfg-rand"><option value="0">Выкл</option><option value="1">Вкл</option></select>
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

</div>
<script>
let csrf="";
function tab(id){document.querySelectorAll('.tab').forEach(x=>x.classList.add('hidden'));document.getElementById('tab-'+id).classList.remove('hidden');document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('active'));event.target.classList.add('active');document.getElementById('title').textContent=event.target.textContent}
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...opts.headers}});if(r.status===401){location.href='/';return}const d=await r.json();if(!r.ok)throw new Error(d.error);return d}
async function load(){
    try{
        const u=await api('/api/me'); csrf=u.csrf;
        document.getElementById('m-sent').textContent=u.stats.sent;
        document.getElementById('m-err').textContent=u.stats.errors;
        document.getElementById('m-acc').textContent=u.accounts;
        document.getElementById('eng-status').textContent=u.running?'РАБОТАЕТ':'ОСТАНОВЛЕН';
        document.getElementById('eng-status').style.color=u.running?'var(--accent)':'var(--muted)';
        document.getElementById('cfg-mode').value=u.mode;
        document.getElementById('cfg-delay').value=u.delay_ms;
        document.getElementById('cfg-rand').value=u.randomize;
        document.getElementById('cfg-warmup').value=u.warmup;
        document.getElementById('cfg-tpl').value=u.templates.join('\n');
        document.getElementById('cfg-tgt').value=u.targets.join('\n');
        
        const logs=await api('/api/logs');
        document.getElementById('term-out').innerHTML=logs.logs.map(l=>`<div>[${l.time}] [${l.uid}] ${l.msg}</div>`).join('');
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
async function saveCfg(){await api('/api/cfg',{method:'POST',body:JSON.stringify({mode:document.getElementById('cfg-mode').value,delay:parseInt(document.getElementById('cfg-delay').value),randomize:document.getElementById('cfg-rand').value,warmup:document.getElementById('cfg-warmup').value})});alert('OK')}
async function saveTpl(){await api('/api/tpl',{method:'POST',body:JSON.stringify({text:document.getElementById('cfg-tpl').value})});alert('OK')}
async function saveTgt(){await api('/api/tgt',{method:'POST',body:JSON.stringify({text:document.getElementById('cfg-tgt').value})});alert('OK')}
async function engine(a){await api('/api/engine/'+a,{method:'POST'});load()}
load();setInterval(load,5000);
</script></body></html>
"""

@app.route("/")
def index(): return "Auth"
@app.route("/login")
def login():
    t = request.args.get("t", "")
    uid = asyncio.run_coroutine_threadsafe(db.verify_token(t), loop).result()
    if not uid: return "Error"
    sid = asyncio.run_coroutine_threadsafe(db.create_session(uid), loop).result()
    resp = redirect("/panel")
    resp.set_cookie("titan_v15", sid, max_age=14*86400)
    return resp
@app.route("/panel")
def panel():
    raw = request.cookies.get("titan_v15")
    uid = asyncio.run_coroutine_threadsafe(db.get_session(raw), loop).result() if raw else None
    if not uid: return redirect("/")
    return render_template_string(UI_HTML)
@app.route("/logout")
def logout():
    resp = redirect("/")
    resp.delete_cookie("titan_v15")
    return resp

# --- API ---
def get_u():
    raw = request.cookies.get("titan_v15")
    uid = asyncio.run_coroutine_threadsafe(db.get_session(raw), loop).result() if raw else None
    if not uid: return None
    return asyncio.run_coroutine_threadsafe(db.get_user(uid), loop).result()

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = get_u()
        if not u: return jsonify({"error":"401"}), 401
        request.user = u
        return f(*a, **kw)
    return w

@app.get("/api/me")
@login_required
def api_me():
    u = request.user
    if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(16)
    accs = asyncio.run_coroutine_threadsafe(db.get_accounts(u["id"]), loop).result()
    return jsonify({
        "id": u["id"], "accounts": len(accs), "running": u["id"] in active_engines,
        "mode": u["mode"], "delay_ms": u["delay_ms"], "randomize": u["randomize"], "warmup": u["warmup"],
        "templates": json.loads(u["templates"] or "[]"),
        "targets": json.loads(u["targets"] or "[]"),
        "stats": asyncio.run_coroutine_threadsafe(db.get_user(u["id"]), loop).result() and {"sent": 0, "errors": 0},
        "csrf": session["csrf"]
    })

@app.get("/api/logs")
def api_logs():
    return jsonify({"logs": list(system_logs)})

@app.post("/api/auth/start")
@login_required
def api_auth_start():
    uid = request.user["id"]
    data = request.json
    phone = data["phone"]
    async def _s():
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        s = await client.send_code_request(phone)
        auth_flows[uid] = {"client": client, "phone": phone, "hash": s.phone_code_hash}
        return True
    try: asyncio.run_coroutine_threadsafe(_s(), loop).result(20); return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)[:50]}), 400

@app.post("/api/auth/verify")
@login_required
def api_auth_verify():
    uid = request.user["id"]
    code = request.json["code"]
    f = auth_flows.get(uid)
    if not f: return jsonify({"error":"Expired"}), 400
    async def _v():
        try:
            await f["client"].sign_in(phone=f["phone"], code=code, phone_code_hash=f["hash"])
            sess = f["client"].session.save()
            async with db.conn() as c:
                await c.execute("INSERT INTO accounts(uid, session, phone) VALUES(?,?,?)", (uid, sess, f["phone"]))
                await c.commit()
            await f["client"].disconnect()
            return {"ok": True}
        except Exception as e: return {"error":str(e)[:50]}
    res = asyncio.run_coroutine_threadsafe(_v(), loop).result(20)
    auth_flows.pop(uid, None)
    return jsonify(res)

@app.post("/api/cfg")
@login_required
def api_cfg():
    d = request.json
    asyncio.run_coroutine_threadsafe(db.set_config(request.user["id"], mode=d["mode"], delay_ms=d["delay"], randomize=d["randomize"], warmup=d["warmup"]), loop).result()
    return jsonify({"ok":True})

@app.post("/api/tpl")
@login_required
def api_tpl():
    v = list(dict.fromkeys(x.strip() for x in request.json["text"].splitlines() if x.strip()))
    asyncio.run_coroutine_threadsafe(db.set_config(request.user["id"], templates=v), loop).result()
    return jsonify({"ok":True})

@app.post("/api/tgt")
@login_required
def api_tgt():
    v = []
    for line in request.json["text"].splitlines():
        x = line.strip()
        if re.match(r"^-?\d+$", x): v.append(int(x))
        elif x.startswith("@"): v.append(x)
        elif re.match(r"^https?://t\.me/", x): v.append("@" + x.split("/")[-1])
    asyncio.run_coroutine_threadsafe(db.set_config(request.user["id"], targets=v), loop).result()
    return jsonify({"ok":True})

@app.post("/api/engine/<action>")
@login_required
def api_engine(action):
    uid = request.user["id"]
    if action == "start":
        if uid in active_engines: return jsonify({"error":"Running"})
        accs = asyncio.run_coroutine_threadsafe(db.get_accounts(uid), loop).result()
        if not accs: return jsonify({"error":"No accounts"}), 400
        engine = OmniEngine(uid, request.user, accs)
        active_engines[uid] = engine
        asyncio.run_coroutine_threadsafe(engine.start(), loop)
    else:
        if uid in active_engines:
            active_engines[uid].stop()
            del active_engines[uid]
    return jsonify({"ok":True})

# -------------------- STARTUP --------------------
def web_server(): serve(app, host="0.0.0.0", port=WEB_PORT, threads=8)

async def main():
    global db, loop, bot
    loop = asyncio.get_running_loop()
    db = OmniDB(DB_PATH)
    await db.init()
    threading.Thread(target=web_server, daemon=True).start()
    log.info(f"WEB STARTED ON {WEB_PORT}")
    await setup_bot()
    await bot.run_until_disconnected()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
