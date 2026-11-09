import os
import json
import time
import secrets
import sqlite3
import threading
import asyncio
import random
import concurrent.futures
import urllib.parse
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, render_template_string, redirect, session, abort
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError

def log_msg(msg):
    print(msg, flush=True)
    try:
        with open("bot_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()} - {msg}\n")
    except: pass

API_ID = int(os.environ.get("API_ID", "2040"))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8885567581:AAEDY44uIQw5W09dzGA0IFSZUMa05uHd4fI")
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "9058235147,8773171051").split(",") if x.strip().isdigit()}
DB_PATH = "titan_saas.db"
BASE_URL = "https://bot-1-i2nc.onrender.com"

# --- ДОБАВЛЕНО: Чтение прокси из переменных окружения ---
# Формат: socks5://user:pass@host:port или http://host:port
PROXY_URL = os.environ.get("TELEGRAM_PROXY")
PROXY = None
if PROXY_URL:
    try:
        parsed = urllib.parse.urlparse(PROXY_URL)
        # Telethon ожидает кортеж (тип, хост, порт, rdns, юзер, пароль)
        # В новых версиях Telethon принимает строку, если установлен python-socks
        PROXY = PROXY_URL 
        log_msg(f"Используется прокси: {parsed.scheme}://{parsed.hostname}:{parsed.port}")
    except Exception as e:
        log_msg(f"Ошибка парсинга прокси: {e}")

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=False)

# Хранилища в памяти
pending_invites = {}   # token -> timestamp
pending_auth = {}      # flow_token -> (client, phone, hash)

# =====================================================================
# БАЗА ДАННЫХ
# =====================================================================
# (Без изменений)
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, phone TEXT, session_string TEXT,
            templates TEXT NOT NULL DEFAULT '[]', max_delay REAL NOT NULL DEFAULT 5.0,
            is_banned INTEGER NOT NULL DEFAULT 0, is_approved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT
        )""")
        try: conn.execute("ALTER TABLE users ADD COLUMN is_approved INTEGER NOT NULL DEFAULT 0")
        except: pass
        try: conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
        except: pass
        conn.commit()
init_db()

def get_user(uid):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def save_user(uid, phone=None, session_str=None):
    cur = get_user(uid)
    with sqlite3.connect(DB_PATH) as conn:
        if cur:
            conn.execute("UPDATE users SET phone=COALESCE(?,phone), session_string=COALESCE(?,session_string) WHERE user_id=?",
                         (phone, session_str, uid))
        else:
            conn.execute("INSERT INTO users(user_id, phone, session_string, created_at) VALUES(?,?,?,?)",
                         (uid, phone, session_str, datetime.now(timezone.utc).isoformat()))
        conn.commit()

def approve_user(uid):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET is_approved=1 WHERE user_id=?", (uid,)); conn.commit()

def revoke_user(uid):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET is_approved=0 WHERE user_id=?", (uid,)); conn.commit()

def ban_user(uid, val):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (val, uid)); conn.commit()

def is_admin(uid): return int(uid) in ADMIN_IDS
def is_approved(user):
    if not user: return False
    if is_admin(user["user_id"]): return True
    try: return bool(user["is_approved"])
    except: return False

def current_user():
    uid = session.get("uid")
    if not uid: return None
    user = get_user(int(uid))
    if not user or user["is_banned"]:
        session.clear(); return None
    return user

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user(): return redirect("/login")
        return fn(*args, **kwargs)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u or not is_admin(u["user_id"]): abort(403)
        return fn(*args, **kwargs)
    return wrapped

# =====================================================================
# ЦИКЛ СОБЫТИЙ
# =====================================================================
async_loop = asyncio.new_event_loop()
def _run_loop(l): asyncio.set_event_loop(l); l.run_forever()
threading.Thread(target=_run_loop, args=(async_loop,), daemon=True).start()

# --- ИЗМЕНЕНО: Увеличен таймаут по умолчанию и текст ошибки ---
def run_async(coro, timeout=60): 
    try:
        f = asyncio.run_coroutine_threadsafe(coro, async_loop)
        return f.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise Exception("Таймаут Telegram. Render блокирует прямые подключения. Настройте TELEGRAM_PROXY.")
    except Exception as e: raise e

# =====================================================================
# СПАМ
# =====================================================================
active_spam_engines = {}

async def spam_worker(uid, target, templates, max_delay, session_str):
    # --- ИЗМЕНЕНО: Добавлен прокси и таймауты ---
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH, proxy=PROXY, connection_retries=5, timeout=30)
    await client.connect()
    active_spam_engines[uid] = True
    log_msg(f"Спам {uid} -> {target}")
    try:
        while active_spam_engines.get(uid):
            try:
                text = random.choice(templates) + "\u200b"
                await client.send_message(target, text)
                await asyncio.sleep(random.uniform(3.0, max_delay))
            except FloodWaitError as e: await asyncio.sleep(e.seconds + 5)
            except Exception as e: 
                log_msg(f"Ошибка спама: {e}")
                break
    finally:
        await client.disconnect()
        if uid in active_spam_engines: del active_spam_engines[uid]

# =====================================================================
# БОТ
# =====================================================================
# --- ИЗМЕНЕНО: Добавлен прокси ---
bot = TelegramClient("bot_saas", API_ID, API_HASH, proxy=PROXY, connection_retries=5, timeout=30)

@bot.on(events.NewMessage(pattern="/start"))
async def bot_start(event):
    uid = event.sender_id
    user = get_user(uid)
    adm = is_admin(uid)
    appr = adm or (user and user["is_approved"])
    
    if adm:
        text = f"👑 **Админ**\nID: `{uid}`"
        buttons = [
            [Button.url("🌐 Панель", f"{BASE_URL}/login")],
            [Button.inline("🔗 Создать ссылку-доступ", b"invite")]
        ]
    elif appr:
        text = f"⚡ **TITAN**\nID: `{uid}`\nДоступ разрешён ✅"
        buttons = [[Button.url("🌐 Открыть панель", f"{BASE_URL}/login")]]
    else:
        text = f"🔒 **Доступ закрыт**\nВаш ID: `{uid}`\n\nОтправьте ID администратору для получения доступа."
        buttons = []
    
    await event.reply(text, buttons=buttons if buttons else None, parse_mode='md')

@bot.on(events.CallbackQuery)
async def bot_cb(event):
    if not is_admin(event.sender_id): return await event.answer("Только админ", alert=True)
    if event.data == b"invite":
        token = secrets.token_urlsafe(16)
        pending_invites[token] = time.time()
        link = f"{BASE_URL}/login?invite={token}"
        await event.reply(
            f"🔗 **Ссылка-приглашение (одноразовая):**\n`{link}`\n\nПосле входа через неё аккаунт автоматически получит доступ.",
            parse_mode='md'
        )

def bot_runner():
    if not BOT_TOKEN: 
        log_msg("BOT_TOKEN отсутствует")
        return
    log_msg(">>> ЗАПУСК БОТА...")
    async def _run():
        try:
            await bot.start(bot_token=BOT_TOKEN)
            log_msg(">>> БОТ УСПЕШНО ЗАПУЩЕН!")
            await bot.run_until_disconnected()
        except Exception as e:
            log_msg(f">>> ОШИБКА БОТА: {e}")
    asyncio.run_coroutine_threadsafe(_run(), async_loop)

threading.Thread(target=bot_runner, daemon=True).start()

# =====================================================================
# UI (Без изменений)
# =====================================================================
BASE_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · TITAN</title>
<style>
:root{color-scheme:dark;--bg:#101214;--panel:#191c20;--line:#2b3036;--muted:#9ba3ad;--text:#f2f4f6;--accent:#d5e6ff;--accentText:#111923;--danger:#ff7777;--ok:#a7e7bd}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,sans-serif}
.shell{max-width:1120px;margin:0 auto;padding:28px 18px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}
.brand{font-size:13px;letter-spacing:.18em;font-weight:800;color:#cbd5e1}.brand span{color:#fff}
.nav{display:flex;gap:10px}.nav a,.btn{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border:1px solid var(--line);border-radius:10px;padding:9px 13px;color:var(--text);background:#20242a;cursor:pointer;font:inherit}
.btn.primary{background:var(--accent);color:var(--accentText);border-color:var(--accent);font-weight:700}.btn.danger{color:var(--danger)}.btn.ok{color:var(--ok)}
h1{font-size:32px;margin:0 0 8px;letter-spacing:-.04em}h2{font-size:18px;margin:0 0 14px}
.sub{color:var(--muted);margin:0 0 24px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:16px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 6px}
input,textarea{width:100%;border:1px solid var(--line);background:#101316;color:var(--text);border-radius:10px;padding:12px;font:inherit;outline:none}
textarea{min-height:150px;resize:vertical}input:focus,textarea:focus{border-color:#8799b1}
.row{display:flex;gap:10px;align-items:center}.row>*{flex:1}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:11px 8px;border-bottom:1px solid var(--line)}
.badge{padding:2px 8px;border-radius:999px;font-size:12px;border:1px solid}
.badge.ok{color:var(--ok);border-color:var(--ok)} .badge.no{color:var(--danger);border-color:var(--danger)}
.error{color:var(--danger);margin-top:10px;font-weight:bold}.ok{color:var(--ok);margin-top:10px;font-weight:bold}
@media(max-width:720px){.grid{grid-template-columns:1fr}.top{flex-direction:column;align-items:flex-start}}
</style></head><body><div class="shell">
<header class="top"><div class="brand">TITAN <span>CONTROL</span></div>
<nav class="nav">{% if user %}<a href="/dashboard">Панель</a>{% if user['user_id']|int in admins %}<a href="/admin">Админка</a>{% endif %}<a href="/logout">Выйти</a>{% endif %}</nav></header>
{{ content|safe }}
</div></body></html>
"""

@app.context_processor
def _admins(): return {"admins": ADMIN_IDS}

def render_page(title, content, user=None):
    return render_template_string(BASE_HTML, title=title, content=content, user=user)

# (Остальные HTML шаблоны без изменений, они корректны)
HOME = r"""
<div class="panel" style="max-width:520px;margin:8vh auto">
<h1>Вход в TITAN</h1><p class="sub">Подключение Telegram-аккаунта.</p>
<label>Номер телефона</label><input id="phone" placeholder="+7 900 000-00-00">
<button class="btn primary" style="width:100%;margin-top:14px" onclick="sendCode()">Получить код</button>
<div id="codebox" style="display:none">
<label>Код Telegram</label><input id="code">
<label>Облачный пароль (2FA)</label><input id="password" type="password">
<button class="btn primary" style="width:100%;margin-top:14px" onclick="verify()">Войти</button></div>
<div id="msg" class="error"></div>
</div>
<script>
const urlParams = new URLSearchParams(window.location.search);
const inviteToken = urlParams.get('invite') || '';
let flow=null;
async function post(url,obj){
    let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});
    if(!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
}
async function sendCode(){
    let p=document.getElementById('phone').value.trim();
    if(!p){msg.textContent='Введите номер';return}
    msg.className='ok'; msg.textContent='Отправляю код в Telegram (до 60 сек)...';
    try {
        let d=await post('/api/send-code',{phone:p});
        if(d.status==='ok'){flow=d.flow;codebox.style.display='block';msg.className='ok';msg.textContent='Код отправлен! Проверьте Telegram.';}
        else{msg.className='error';msg.textContent=d.error;}
    } catch(e) { msg.className='error'; msg.textContent='Сбой: '+e.message; }
}
async function verify(){
    let d=await post('/api/verify-code',{flow:flow,code:document.getElementById('code').value.trim(),password:document.getElementById('password').value,invite:inviteToken});
    if(d.status==='ok'){location.href='/dashboard'}else{msg.className='error';msg.textContent=d.error}
}
</script>
"""

WAITING = r"""
<div class="panel" style="max-width:520px;margin:8vh auto;text-align:center">
<h1>⏳ Ожидание доступа</h1>
<p class="sub">Ваш аккаунт ещё не одобрен администратором.</p>
<p style="font-size:14px">Ваш Telegram ID:</p>
<p style="font-size:24px;font-weight:800;color:var(--accent);font-family:monospace">{{ user['user_id'] }}</p>
<p style="margin-top:20px">Отправьте этот ID администратору. После одобрения просто обновите страницу.</p>
</div>
"""

DASHBOARD = r"""
<h1>Панель управления</h1><p class="sub">Аккаунт подключён.</p>
<div class="grid">
<div class="panel"><h2>Аккаунт</h2><p>ID: <b>{{ user['user_id'] }}</b></p><p>Телефон: <b>{{ user['phone'] }}</b></p></div>
<div class="panel"><h2>Задержка</h2>
<label>Максимальная (сек.)</label><input id="maxdelay" type="number" min="3" max="3600" value="{{ user['max_delay'] }}">
<button class="btn primary" onclick="saveSettings()" style="margin-top:12px">Сохранить</button></div>
</div>
<div class="panel"><h2>🚀 Запуск</h2>
<div class="row">
    <input id="spam_target" placeholder="@username или -100123...">
    <button class="btn primary" onclick="startSpam()">Запустить</button>
    <button class="btn danger" onclick="stopSpam()">Остановить</button>
</div>
<div id="spammsg" style="margin-top:10px"></div>
</div>
<div class="panel"><h2>Шаблоны</h2>
<textarea id="templates" placeholder="Каждая строка — отдельное сообщение">{{ templates }}</textarea>
<button class="btn primary" onclick="saveTemplates()" style="margin-top:12px">Сохранить</button>
</div>
<script>
async function post(url,obj){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});return await r.json()}
async function saveSettings(){await post('/api/settings',{max_delay:document.getElementById('maxdelay').value});alert('Сохранено!')}
async function saveTemplates(){await post('/api/templates',{text:document.getElementById('templates').value});alert('Шаблоны сохранены!')}
async function startSpam(){let d=await post('/api/spam/start',{target:document.getElementById('spam_target').value});document.getElementById('spammsg').textContent=d.status==='ok'?'🚀 Запущено':d.error}
async function stopSpam(){await post('/api/spam/stop',{});document.getElementById('spammsg').textContent='🛑 Остановлено'}
</script>
"""

ADMIN_PAGE = r"""
<h1>Админ-панель</h1>

<div class="panel">
<h2>🔗 Ссылки-приглашения</h2>
<p class="sub" style="font-size:13px">Создайте ссылку и отправьте другу. После входа через неё аккаунт автоматически получит доступ.</p>
<button class="btn primary" onclick="genInvite()">Создать ссылку-доступ</button>
<div id="invite_link" style="margin-top:12px;word-break:break-all"></div>
</div>

<div class="panel">
<h2>👥 Пользователи</h2>
<table>
<tr><th>ID</th><th>Телефон</th><th>Доступ</th><th>Бан</th><th>Действия</th></tr>
{% for u in users %}
<tr>
<td><b>{{ u['user_id'] }}</b></td>
<td>{{ u['phone'] or '—' }}</td>
<td>{% if u['user_id']|int in admins %}<span class="badge ok">Админ</span>{% elif u['is_approved'] %}<span class="badge ok">Разрешено</span>{% else %}<span class="badge no">Ожидает</span>{% endif %}</td>
<td>{% if u['is_banned'] %}<span class="badge no">Бан</span>{% else %}<span class="badge ok">ОК</span>{% endif %}</td>
<td>
{% if u['user_id']|int not in admins %}
<form method="post" action="/admin/action" style="display:inline">
<input type="hidden" name="uid" value="{{u['user_id']}}">
<input type="hidden" name="action" value="{{'revoke' if u['is_approved'] else 'approve'}}">
<button class="btn {% if u['is_approved'] %}danger{% else %}ok{% endif %}" type="submit">{{'Отозвать' if u['is_approved'] else 'Разрешить'}}</button>
</form>
<form method="post" action="/admin/action" style="display:inline;margin-left:6px">
<input type="hidden" name="uid" value="{{u['user_id']}}">
<input type="hidden" name="action" value="{{'unban' if u['is_banned'] else 'ban'}}">
<button class="btn danger" type="submit">{{'Разбанить' if u['is_banned'] else 'Забанить'}}</button>
</form>
{% else %}<span style="color:var(--muted)">—</span>{% endif %}
</td>
</tr>
{% endfor %}
</table>
</div>

<script>
async function genInvite(){
    let r=await fetch('/admin/gen_invite',{method:'POST'});
    let d=await r.json();
    if(d.status==='ok') document.getElementById('invite_link').innerHTML = '🔗 <code style="word-break:break-all">'+d.link+'</code>';
}
</script>
"""

# =====================================================================
# МАРШРУТЫ
# =====================================================================
@app.route("/")
@app.route("/login")
def login_page():
    if "uid" in session: return redirect("/dashboard")
    return render_page("Вход", HOME)

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if not user: return redirect("/login")
    if not is_approved(user):
        return render_page("Ожидание", render_template_string(WAITING, user=user), user=user)
    try: templates = json.loads(user["templates"] or "[]")
    except: templates = []
    return render_page("Панель", render_template_string(DASHBOARD, user=user, templates="\n".join(templates)), user=user)

@app.route("/admin")
@admin_required
def admin():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return render_page("Админка", render_template_string(ADMIN_PAGE, users=users), user=current_user())

@app.route("/logout")
def logout():
    session.clear(); return redirect("/login")

@app.route("/admin/gen_invite", methods=["POST"])
@admin_required
def admin_gen_invite():
    token = secrets.token_urlsafe(16)
    pending_invites[token] = time.time()
    return jsonify(status="ok", link=f"{BASE_URL}/login?invite={token}")

@app.route("/admin/action", methods=["POST"])
@admin_required
def admin_action():
    try: target = int(request.form.get("uid"))
    except: abort(400)
    if is_admin(target): abort(403)
    action = request.form.get("action")
    if action == "approve": approve_user(target)
    elif action == "revoke": revoke_user(target)
    elif action == "ban": ban_user(target, 1)
    elif action == "unban": ban_user(target, 0)
    return redirect("/admin")

# =====================================================================
# API
# =====================================================================
@app.route("/api/send-code", methods=["POST"])
def api_send_code():
    data = request.json
    phone = data.get('phone')
    flow = secrets.token_urlsafe(16)
    
    async def _send():
        # --- ИЗМЕНЕНО: Добавлен прокси, таймауты и ретраи ---
        c = TelegramClient(StringSession(), API_ID, API_HASH, proxy=PROXY, connection_retries=5, retry_delay=2, timeout=60)
        await c.connect()
        req = await c.send_code_request(phone)
        return c, req.phone_code_hash

    try:
        # --- ИЗМЕНЕНО: Таймаут увеличен до 60 секунд ---
        client, code_hash = run_async(_send(), timeout=60)
        pending_auth[flow] = (client, phone, code_hash)
        return jsonify(status="ok", flow=flow)
    except Exception as e:
        return jsonify(status="error", error=f"Telegram: {str(e)[:120]}")

@app.route("/api/verify-code", methods=["POST"])
def api_verify_code():
    data = request.json
    flow = data.get('flow')
    code = data.get('code')
    password = data.get('password')
    invite = data.get('invite')
    
    auth_data = pending_auth.get(flow)
    if not auth_data: return jsonify(status="error", error="Сессия истекла, запросите код заново")
    client, phone, code_hash = auth_data

    async def _verify():
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except SessionPasswordNeededError:
            if not password: return {"need_password": True}
            await client.sign_in(password=password)
        me = await client.get_me()
        return {"uid": me.id, "sess": client.session.save()}

    try:
        # --- ИЗМЕНЕНО: Таймаут увеличен до 60 секунд ---
        res = run_async(_verify(), timeout=60)
        if isinstance(res, dict) and res.get("need_password"):
            return jsonify(status="error", error="Нужен пароль 2FA (введите и нажмите Войти)")
        save_user(res["uid"], phone=phone, session_str=res["sess"])
        if invite and invite in pending_invites:
            del pending_invites[invite]
            approve_user(res["uid"])
        del pending_auth[flow]
        session["uid"] = res["uid"]
        return jsonify(status="ok")
    except Exception as e:
        return jsonify(status="error", error=f"Ошибка входа: {str(e)[:120]}")

@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    user = current_user()
    if not is_approved(user): return jsonify(status="error", error="Нет доступа")
    try:
        max_d = float(request.json.get("max_delay"))
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE users SET max_delay=? WHERE user_id=?", (max_d, user["user_id"]))
            conn.commit()
        return jsonify(status="ok")
    except: return jsonify(status="error")

@app.route("/api/templates", methods=["POST"])
@login_required
def api_templates():
    user = current_user()
    if not is_approved(user): return jsonify(status="error", error="Нет доступа")
    lines = request.json.get("text", "").splitlines()
    clean = [l.strip() for l in lines if l.strip()]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET templates=? WHERE user_id=?", (json.dumps(clean), user["user_id"]))
        conn.commit()
    return jsonify(status="ok")

@app.route("/api/spam/start", methods=["POST"])
@login_required
def api_spam_start():
    user = current_user()
    if not is_approved(user): return jsonify(status="error", error="Нет доступа")
    uid = user["user_id"]
    if uid in active_spam_engines: return jsonify(status="error", error="Уже запущено")
    target = request.json.get("target")
    tpls = json.loads(user["templates"] or "[]")
    if not tpls: return jsonify(status="error", error="Нет шаблонов")
    if not user["session_string"]: return jsonify(status="error", error="Нет сессии")
    asyncio.run_coroutine_threadsafe(
        spam_worker(uid, target, tpls, user["max_delay"], user["session_string"]),
        async_loop
    )
    return jsonify(status="ok")

@app.route("/api/spam/stop", methods=["POST"])
@login_required
def api_spam_stop():
    uid = session.get("uid")
    if uid in active_spam_engines:
        active_spam_engines[uid] = False
    return jsonify(status="ok")

@app.route("/health")
def health(): return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
