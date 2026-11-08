import os
import re
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

# ---------- Configuration ----------
API_ID = int(os.environ.get("API_ID", "2040"))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8732669492:AAEIMsA49rH-2MTp78gTgc9DrLLyCzgop3E")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Твои два ID (Админы)
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "9058235147,8773171051").split(",") if x.strip().isdigit()}

DB_PATH = os.environ.get("DB_PATH", "titan_saas.db")
MIN_DELAY_SECONDS = 3.0
MAX_TEMPLATE_LINES = 9999
AUTH_FLOW_TTL = 600

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)

# Глобальный цикл событий
async_loop = asyncio.new_event_loop()
threading.Thread(target=lambda: (asyncio.set_event_loop(async_loop), async_loop.run_forever()), daemon=True).start()

auth_flows = {}
active_spam_engines = {} 
invite_tokens = {}

def run_async(coro, timeout=45):
    future = asyncio.run_coroutine_threadsafe(coro, async_loop)
    return future.result(timeout=timeout)

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, phone TEXT, session_string TEXT,
                templates TEXT NOT NULL DEFAULT '[]', min_delay REAL NOT NULL DEFAULT 3.0,
                max_delay REAL NOT NULL DEFAULT 5.0, is_banned INTEGER NOT NULL DEFAULT 0,
                is_disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            )
        """)
        conn.commit()

init_db()

# ---------- Helpers ----------
def get_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def save_user(user_id, phone, session_string):
    with db() as conn:
        existing = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET phone=?, session_string=? WHERE user_id=?", (phone, session_string, user_id))
        else:
            conn.execute("INSERT INTO users(user_id, phone, session_string, created_at) VALUES(?,?,?,?)", (user_id, phone, session_string, utc_now()))
        conn.commit()

def current_user():
    uid = session.get("uid")
    if not uid: return None
    user = get_user(int(uid))
    if not user or user["is_banned"] or user["is_disabled"]:
        session.clear()
        return None
    return user

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user(): return redirect(url_for("home"))
        return fn(*args, **kwargs)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or int(user["user_id"]) not in ADMIN_IDS: abort(403)
        return fn(*args, **kwargs)
    return wrapped

# ---------- UI Templates ----------
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

# ---------- ROUTES (LOGIN) ----------
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

@app.route("/")
def home():
    if current_user(): return redirect("/dashboard")
    return render_page("Вход", HOME)

# Исправленный роут, который ловит и /login?token=, и /login?invite=
@app.route("/login")
def login_route():
    if current_user(): return redirect("/dashboard")
    return render_page("Вход", HOME)

@app.route("/api/send-code", methods=["POST"])
def api_send_code():
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    if not re.fullmatch(r"\+?[0-9 ()-]{7,24}", phone):
        return jsonify(status="error", error="Проверьте номер.")
    flow = secrets.token_urlsafe(32)

    async def create_and_send():
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        sent = await client.send_code_request(phone)
        return client, sent.phone_code_hash

    try:
        client, code_hash = run_async(create_and_send())
        auth_flows[flow] = {"client": client, "phone": phone, "hash": code_hash, "created": time.time()}
        return jsonify(status="ok", flow=flow)
    except FloodWaitError as e:
        return jsonify(status="error", error=f"Подождите {e.seconds} секунд.")
    except Exception as e:
        return jsonify(status="error", error=f"Ошибка: {str(e)[:100]}")

@app.route("/api/verify-code", methods=["POST"])
def api_verify_code():
    data = request.get_json(silent=True) or {}
    flow, code, password = data.get("flow"), data.get("code"), data.get("password")
    if not flow or not code: return jsonify(status="error", error="Введите код.")
    
    auth = auth_flows.get(flow)
    if not auth or time.time() - auth["created"] > AUTH_FLOW_TTL:
        return jsonify(status="error", error="Сессия истекла.")

    async def finish():
        client = auth["client"]
        try:
            await client.sign_in(phone=auth["phone"], code=code, phone_code_hash=auth["hash"])
        except SessionPasswordNeededError:
            if not password: return {"need_password": True}
            await client.sign_in(password=password)
        me = await client.get_me()
        return {"user_id": me.id, "session": client.session.save()}

    try:
        result = run_async(finish())
        if result.get("need_password"):
            return jsonify(status="error", error="Нужен пароль 2FA.")
        
        save_user(result["user_id"], auth["phone"], result["session"])
        del auth_flows[flow]
        session.clear()
        session["uid"] = int(result["user_id"])
        return jsonify(status="ok")
    except Exception as e:
        return jsonify(status="error", error=f"Неверный код или ошибка: {str(e)[:100]}")

# ---------- ROUTES (DASHBOARD) ----------
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
<textarea id="templates" placeholder="Каждая строка — сообщение">{{ templates }}</textarea>
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

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    try: templates = json.loads(user["templates"] or "[]")
    except: templates = []
    return render_page("Панель", render_template_string(DASHBOARD, user=user, templates="\n".join(templates)), user=user)

@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    data = request.get_json(silent=True) or {}
    try: max_delay = float(data.get("max_delay"))
    except: return jsonify(status="error", error="Неверная задержка")
    if max_delay < MIN_DELAY_SECONDS: return jsonify(status="error", error="Мин. 3 сек.")
    with db() as conn:
        conn.execute("UPDATE users SET max_delay=? WHERE user_id=?", (max_delay, current_user()["user_id"]))
        conn.commit()
    return jsonify(status="ok")

@app.route("/api/templates", methods=["POST"])
@login_required
def api_templates():
    data = request.get_json(silent=True) or {}
    lines = [l.rstrip() for l in data.get("text","").splitlines()]
    if len(lines) > MAX_TEMPLATE_LINES: return jsonify(status="error", error="Слишком много строк")
    with db() as conn:
        conn.execute("UPDATE users SET templates=? WHERE user_id=?", (json.dumps(lines), current_user()["user_id"]))
        conn.commit()
    return jsonify(status="ok", count=len(lines))

# ---------- SPAM ENGINE ----------
class SpamEngine:
    def __init__(self, user_id, target, templates, min_d, max_d, session_str):
        self.user_id, self.target, self.templates = user_id, target, templates
        self.min_d, self.max_d, self.session_str = min_d, max_d, session_str
        self.is_active, self.client = False, None

    async def run(self):
        self.is_active = True
        try:
            self.client = TelegramClient(StringSession(self.session_str), API_ID, API_HASH)
            await self.client.connect()
            while self.is_active:
                try:
                    text = random.choice(self.templates) + "\u200b"
                    await self.client.send_message(self.target, text)
                    await asyncio.sleep(random.uniform(self.min_d, self.max_d))
                except FloodWaitError as e: await asyncio.sleep(e.seconds + 5)
                except Exception: break
        except: pass
        finally:
            if self.client: await self.client.disconnect()
            self.is_active = False

@app.route("/api/spam/start", methods=["POST"])
@login_required
def api_spam_start():
    data = request.get_json(silent=True) or {}
    target = data.get("target", "").strip()
    user = current_user()
    uid = int(user["user_id"])
    
    if not target: return jsonify(status="error", error="Укажите цель.")
    if uid in active_spam_engines: return jsonify(status="error", error="Уже запущено.")
    
    templates = json.loads(user["templates"] or "[]")
    if not templates: return jsonify(status="error", error="Нет шаблонов.")

    engine = SpamEngine(uid, target, templates, user["min_delay"], user["max_delay"], user["session_string"])
    active_spam_engines[uid] = engine
    asyncio.run_coroutine_threadsafe(engine.run(), async_loop)
    return jsonify(status="ok")

@app.route("/api/spam/stop", methods=["POST"])
@login_required
def api_spam_stop():
    uid = int(current_user()["user_id"])
    if uid in active_spam_engines:
        active_spam_engines[uid].is_active = False
        del active_spam_engines[uid]
        return jsonify(status="ok")
    return jsonify(status="error", error="Не запущено.")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------- ADMIN & INVITES ----------
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
<td>{% if u['is_banned'] %}Бан{% elif u['is_disabled'] %}Отключён{% else %}Активен{% endif %}</td>
<td>
<form method="post" action="/admin/action" style="display:inline">
<input type="hidden" name="uid" value="{{u['user_id']}}">
<input type="hidden" name="action" value="{{'unban' if u['is_banned'] else 'ban'}}">
<button class="btn" type="submit">{{'Разбанить' if u['is_banned'] else 'Бан'}}</button>
</form>
</td></tr>{% endfor %}
</table>
</div>
<script>
async function genInvite(){
    let r=await fetch('/admin/gen_invite', {method:'POST'});
    let d=await r.json();
    document.getElementById('invite_link').innerHTML = `Ссылка: <b>https://bot-1-dk7y.onrender.com/login?invite=${d.token}</b>`;
}
</script>
"""

@app.route("/admin")
@admin_required
def admin():
    with db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return render_page("Админка", render_template_string(ADMIN_PAGE, users=users), user=current_user())

@app.route("/admin/action", methods=["POST"])
@admin_required
def admin_action():
    admin_id = int(current_user()["user_id"])
    target_id = int(request.form.get("uid", ""))
    action = request.form.get("action")
    
    if target_id in ADMIN_IDS: abort(403)
    
    field = "is_banned" if action in ("ban", "unban") else "is_disabled"
    value = 1 if action in ("ban", "disable") else 0
    
    with db() as conn:
        conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, target_id))
        conn.commit()
    
    if value == 1 and target_id in active_spam_engines:
        active_spam_engines[target_id].is_active = False
        del active_spam_engines[target_id]
        
    return redirect("/admin")

@app.route("/admin/gen_invite", methods=["POST"])
@admin_required
def gen_invite():
    token = secrets.token_urlsafe(16)
    invite_tokens[token] = time.time()
    return jsonify(status="ok", token=token)

@app.route("/health")
def health(): return jsonify(status="ok")

# ---------- BOT (С КНОПКАМИ) ----------
bot_client = TelegramClient("bot_saas", API_ID, API_HASH)

@bot_client.on(events.NewMessage(pattern="/start"))
async def bot_start(event):
    uid = event.sender_id
    is_admin = uid in ADMIN_IDS
    
    # Создаем токен для входа на сайт
    token = secrets.token_urlsafe(16)
    invite_tokens[token] = time.time()
    
    web_link = f"https://bot-1-dk7y.onrender.com/login?token={token}"
    
    buttons = [
        [Button.url("🌐 Открыть веб-панель", web_link)],
        [Button.inline("👤 Мой профиль", b"profile"), Button.inline("⚙️ Настройки", b"settings")]
    ]
    if is_admin:
        buttons.append([Button.inline("👑 Админка (генератор ссылок)", b"admin_panel")])
        
    text = (
        f"⚡ **TITAN Enterprise v14.0**\n\n"
        f"Твой Telegram ID: `{uid}`\n"
        f"Статус: {'👑 Администратор' if is_admin else '👤 Пользователь'}\n\n"
        f"Для управления рассылкой используй веб-панель (кнопка ниже)."
    )
    
    await event.reply(text, buttons=buttons, parse_mode='md')

@bot_client.on(events.CallbackQuery)
async def bot_callback(event):
    uid = event.sender_id
    data = event.data.decode('utf-8')
    
    if data == "profile":
        user = get_user(uid)
        status = "✅ Подключен" if user and user['session_string'] else "❌ Нет сессии"
        await event.answer(f"Статус аккаунта: {status}", alert=True)
        
    elif data == "settings":
        await event.answer("Настройки меняются в веб-панели!", alert=True)
        
    elif data == "admin_panel":
        if uid not in ADMIN_IDS:
            return await event.answer("⛔ Доступ запрещен!", alert=True)
            
        token = secrets.token_urlsafe(16)
        invite_tokens[token] = time.time()
        link = f"https://bot-1-dk7y.onrender.com/login?invite={token}"
        
        await event.reply(f"👑 **Ссылка для кента:**\n\n`{link}`\n\n(Отправь её другу, чтобы он подключился)", parse_mode='md')

def start_bot_thread():
    if not BOT_TOKEN: 
        print("BOT_TOKEN не задан!")
        return
    try:
        print("Запускаю бота...")
        run_async(bot_client.start(bot_token=BOT_TOKEN))
        print("Бот успешно запущен!")
        run_async(bot_client.run_until_disconnected())
    except Exception as e:
        print(f"ОШИБКА БОТА: {e}")

# Запуск в фоне
threading.Thread(target=start_bot_thread, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
