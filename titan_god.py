import os
import json
import time
import secrets
import sqlite3
import threading
import asyncio
import random
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, request, jsonify, render_template_string, redirect, url_for, session, abort
)
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError
)

# =====================================================================
# 1. КОНФИГУРАЦИЯ
# =====================================================================
API_ID = int(os.environ.get("API_ID", "2040"))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8885567581:AAEDY44uIQw5W09dzGA0IFSZUMa05uHd4fI")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# ID Админов (твои аккаунты)
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "9058235147,8773171051").split(",") if x.strip().isdigit()}

DB_PATH = "titan_saas.db"
AUTH_FLOW_TTL = 600
MIN_DELAY_SECONDS = 3.0
MAX_TEMPLATE_LINES = 9999

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False, 
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)

# =====================================================================
# 2. ЕДИНЫЙ АСИНХРОННЫЙ ЦИКЛ ДЛЯ TELETHON (БОТ И СПАМ)
# =====================================================================
async_loop = asyncio.new_event_loop()

def start_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

threading.Thread(target=start_async_loop, args=(async_loop,), daemon=True).start()

def run_async(coro, timeout=45):
    future = asyncio.run_coroutine_threadsafe(coro, async_loop)
    return future.result(timeout=timeout)

# =====================================================================
# 3. БАЗА ДАННЫХ (SQLITE)
# =====================================================================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, phone TEXT, session_string TEXT,
                templates TEXT NOT NULL DEFAULT '[]', min_delay REAL NOT NULL DEFAULT 3.0,
                max_delay REAL NOT NULL DEFAULT 5.0, is_banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()

init_db()

def get_user(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def save_user(user_id, phone, session_string):
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET phone=?, session_string=? WHERE user_id=?", (phone, session_string, user_id))
        else:
            conn.execute("INSERT INTO users(user_id, phone, session_string, created_at) VALUES(?,?,?,?)", (user_id, phone, session_string, datetime.now(timezone.utc).isoformat()))
        conn.commit()

def get_web_token(token):
    # Для простоты токены храним в памяти (сбрасываются при рестарте)
    return globals().get(f"web_token_{token}")

def set_web_token(token, uid):
    globals()[f"web_token_{token}"] = uid

def current_user():
    uid = session.get("uid")
    if not uid: return None
    user = get_user(int(uid))
    if not user or user["is_banned"]:
        session.clear()
        return None
    return user

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user(): return redirect(url_for("login_page"))
        return fn(*args, **kwargs)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or int(user["user_id"]) not in ADMIN_IDS: abort(403)
        return fn(*args, **kwargs)
    return wrapped

# =====================================================================
# 4. СПАМ-ДВИЖОК
# =====================================================================
active_spam_engines = {}

async def spam_worker(uid, target, templates, max_delay, session_str):
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    active_spam_engines[uid] = True
    
    try:
        while active_spam_engines.get(uid):
            try:
                text = random.choice(templates) + "\u200b"
                await client.send_message(target, text)
                await asyncio.sleep(random.uniform(MIN_DELAY_SECONDS, max_delay))
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:
                print(f"Ошибка спама {uid}: {e}")
                break
    finally:
        await client.disconnect()
        if uid in active_spam_engines:
            del active_spam_engines[uid]

# =====================================================================
# 5. TELEGRAM БОТ
# =====================================================================
bot = TelegramClient("bot_saas", API_ID, API_HASH)

@bot.on(events.NewMessage(pattern="/start"))
async def bot_start(event):
    uid = event.sender_id
    is_admin = uid in ADMIN_IDS
    
    token = secrets.token_urlsafe(16)
    set_web_token(token, uid)
    
    link = f"https://bot-1-dk7y.onrender.com/login?token={token}"
    
    buttons = [[Button.url("🌐 Войти в панель", link)]]
    if is_admin:
        buttons.append([Button.inline("👑 Дать доступ", b"gen_invite")])

    text = (
        f"⚡ **TITAN Enterprise**\n\n"
        f"Ваш ID: `{uid}`\n"
        f"Статус: {'Администратор' if is_admin else 'Пользователь'}\n\n"
        f"Нажмите кнопку ниже для входа в панель."
    )
    await event.reply(text, buttons=buttons, parse_mode='md')

@bot.on(events.CallbackQuery)
async def bot_callback(event):
    if event.sender_id not in ADMIN_IDS:
        return await event.answer("Доступ только для админа!", alert=True)
    
    if event.data == b"gen_invite":
        token = secrets.token_urlsafe(16)
        link = f"https://bot-1-dk7y.onrender.com/login?invite={token}"
        await event.reply(f"🔗 **Ссылка для друга:**\n`{link}`", parse_mode='md')

def start_bot_thread():
    if not BOT_TOKEN: return
    try:
        bot.start(bot_token=BOT_TOKEN)
        print(">>> БОТ УСПЕШНО ЗАПУЩЕН!")
        bot.run_until_disconnected()
    except Exception as e:
        print(f">>> ОШИБКА БОТА: {e}")

threading.Thread(target=start_bot_thread, daemon=True).start()

# =====================================================================
# 6. UI (HTML/CSS) - ДИЗАЙН КАК ТЫ ПРОСИЛ
# =====================================================================
BASE_HTML = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · TITAN</title>
<style>
:root{color-scheme:dark;--bg:#101214;--panel:#191c20;--line:#2b3036;--muted:#9ba3ad;--text:#f2f4f6;--accent:#d5e6ff;--accentText:#111923;--danger:#ff7777}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,sans-serif}
.shell{max-width:1120px;margin:0 auto;padding:28px 18px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}
.brand{font-size:13px;letter-spacing:.18em;font-weight:800;color:#cbd5e1}.brand span{color:#fff}
.nav{display:flex;gap:10px}.nav a,.btn{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border:1px solid var(--line);border-radius:10px;padding:9px 13px;color:var(--text);background:#20242a;cursor:pointer;font:inherit}
.btn.primary{background:var(--accent);color:var(--accentText);border-color:var(--accent);font-weight:700}.btn.danger{color:var(--danger)}
h1{font-size:32px;margin:0 0 8px;letter-spacing:-.04em}h2{font-size:18px;margin:0 0 14px}
.sub{color:var(--muted);margin:0 0 24px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:16px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 6px}
input,textarea{width:100%;border:1px solid var(--line);background:#101316;color:var(--text);border-radius:10px;padding:12px;font:inherit;outline:none}
textarea{min-height:150px;resize:vertical}input:focus,textarea:focus{border-color:#8799b1}
.row{display:flex;gap:10px;align-items:center}.row>*{flex:1}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:11px 8px;border-bottom:1px solid var(--line)}
.status{display:inline-block;border:1px solid var(--line);padding:3px 8px;border-radius:999px;font-size:12px}
.error{color:#ff9292;margin-top:10px}.ok{color:#a7e7bd;margin-top:10px}
@media(max-width:720px){.grid{grid-template-columns:1fr}.top{flex-direction:column;align-items:flex-start}}
</style></head><body><div class="shell">
<header class="top"><div class="brand">TITAN <span>CONTROL</span></div>
<nav class="nav">{% if user %}<a href="/dashboard">Панель</a>{% if user['user_id']|int in admins %}<a href="/admin">Админка</a>{% endif %}<a href="/logout">Выйти</a>{% endif %}</nav></header>
{{ content|safe }}
</div></body></html>
"""

@app.context_processor
def inject_admins(): return {"admins": ADMIN_IDS}

def render_page(title, content, user=None):
    return render_template_string(BASE_HTML, title=title, content=content, user=user)

# ==================== СТРАНИЦА ВХОДА ====================
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
let flow=null;
async function post(url,obj){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});return await r.json()}
async function sendCode(){let p=document.getElementById('phone').value.trim();if(!p){msg.textContent='Введите номер';return}
let d=await post('/api/send-code',{phone:p});if(d.status==='ok'){flow=d.flow;codebox.style.display='block';msg.className='ok';msg.textContent='Код отправлен'}else{msg.className='error';msg.textContent=d.error}}
async function verify(){let d=await post('/api/verify-code',{flow:flow,code:document.getElementById('code').value.trim(),password:document.getElementById('password').value});
if(d.status==='ok'){location.href='/dashboard'}else{msg.className='error';msg.textContent=d.error}}
</script>
"""

# ==================== ДАШБОРД ====================
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

# ==================== АДМИНКА ====================
ADMIN_PAGE = r"""
<h1>Админ-панель</h1>
<div class="panel">
<h2>🔗 Генерация ссылок</h2>
<button class="btn primary" onclick="genInvite()">Создать ссылку для друга</button>
<div id="invite_link" style="margin-top:10px;color:#a7e7bd;"></div>
</div>
<div class="panel">
<table><tr><th>ID</th><th>Телефон</th><th>Состояние</th><th>Действие</th></tr>
{% for u in users %}<tr><td>{{ u['user_id'] }}</td><td>{{ u['phone'] or '—' }}</td>
<td>{% if u['is_banned'] %}Бан{% else %}Активен{% endif %}</td>
<td><form method="post" action="/admin/action" style="display:inline"><input type="hidden" name="uid" value="{{u['user_id']}}"><input type="hidden" name="action" value="{{'unban' if u['is_banned'] else 'ban'}}"><button class="btn" type="submit">{{'Разбанить' if u['is_banned'] else 'Бан'}}</button></form></td></tr>{% endfor %}
</table>
</div>
<script>
async function genInvite(){let r=await fetch('/admin/gen_invite', {method:'POST'});let d=await r.json();document.getElementById('invite_link').innerHTML = `Ссылка: <b>https://bot-1-dk7y.onrender.com/login?invite=${d.token}</b>`;}
</script>
"""

# =====================================================================
# 7. FLASK МАРШРУТЫ
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
    try: templates = json.loads(user["templates"] or "[]")
    except: templates = []
    return render_page("Панель", render_template_string(DASHBOARD, user=user, templates="\n".join(templates)), user=user)

@app.route("/admin")
@admin_required
def admin():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT * FROM users ORDER BY user_id DESC").fetchall()
    return render_page("Админка", render_template_string(ADMIN_PAGE, users=users), user=current_user())

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# =====================================================================
# 8. API МАРШРУТЫ
# =====================================================================
@app.route("/api/send-code", methods=["POST"])
def api_send_code():
    data = request.json
    phone = data.get('phone')
    flow = secrets.token_urlsafe(16)
    
    async def _send():
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        req = await c.send_code_request(phone)
        return c, req.phone_code_hash

    try:
        client, code_hash = run_async(_send())
        globals()[f"auth_{flow}"] = (client, phone, code_hash)
        return jsonify(status="ok", flow=flow)
    except Exception as e:
        return jsonify(status="error", error=str(e))

@app.route("/api/verify-code", methods=["POST"])
def api_verify_code():
    data = request.json
    flow = data.get('flow')
    code = data.get('code')
    password = data.get('password')
    
    auth_data = globals().get(f"auth_{flow}")
    if not auth_data: return jsonify(status="error", error="Сессия истекла")
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
        res = run_async(_verify())
        if isinstance(res, dict) and res.get("need_password"):
             return jsonify(status="error", error="Нужен пароль 2FA (введите его и нажмите Войти)")
        
        save_user(res["uid"], phone, res["sess"])
        session["uid"] = res["uid"]
        return jsonify(status="ok")
    except Exception as e:
        return jsonify(status="error", error=f"Ошибка: {e}")

@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    try:
        max_d = float(request.json.get("max_delay"))
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE users SET max_delay=? WHERE user_id=?", (max_d, session["uid"]))
            conn.commit()
        return jsonify(status="ok")
    except: return jsonify(status="error")

@app.route("/api/templates", methods=["POST"])
@login_required
def api_templates():
    lines = request.json.get("text", "").splitlines()
    clean_lines = [l.strip() for l in lines if l.strip()]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET templates=? WHERE user_id=?", (json.dumps(clean_lines), session["uid"]))
        conn.commit()
    return jsonify(status="ok")

@app.route("/api/spam/start", methods=["POST"])
@login_required
def api_spam_start():
    uid = session["uid"]
    if uid in active_spam_engines: return jsonify(status="error", error="Уже запущено")
    
    target = request.json.get("target")
    user = get_user(uid)
    tpls = json.loads(user["templates"])
    
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

@app.route("/admin/action", methods=["POST"])
@admin_required
def admin_action():
    target = int(request.form.get("uid"))
    action = request.form.get("action")
    val = 1 if action == "ban" else 0
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (val, target))
        conn.commit()
    return redirect("/admin")

@app.route("/admin/gen_invite", methods=["POST"])
@admin_required
def admin_gen_invite():
    return jsonify(status="ok", token=secrets.token_urlsafe(16))

@app.route("/health")
def health(): return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
