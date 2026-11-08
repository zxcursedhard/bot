import os
import re
import json
import time
import secrets
import sqlite3
import threading
import asyncio
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, request, jsonify, render_template_string, redirect, url_for,
    session, abort
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    FloodWaitError
)

# ---------- Configuration ----------
API_ID = int(os.environ.get("API_ID", "2040"))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # ЗАДАЙ В RENDER!
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get(
        "ADMIN_IDS", "9058235147,8773171051"
    ).split(",") if x.strip().isdigit()
}
DB_PATH = os.environ.get("DB_PATH", "titan_saas.db")
LOGIN_LINK_TTL = 300
AUTH_FLOW_TTL = 600
MAX_TEMPLATE_LINES = 9999
MIN_DELAY_SECONDS = 3.0

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)

# A single dedicated asyncio loop for Telethon; do not create a new loop per request.
async_loop = asyncio.new_event_loop()
loop_thread = threading.Thread(
    target=lambda: (asyncio.set_event_loop(async_loop), async_loop.run_forever()),
    daemon=True,
    name="telethon-loop",
)
loop_thread.start()

auth_flows = {}  # flow token -> {client, phone, phone_code_hash, created}
auth_lock = threading.Lock()
active_spam_engines = {}  # user_id -> SpamEngine


def run_async(coro, timeout=45):
    future = asyncio.run_coroutine_threadsafe(coro, async_loop)
    return future.result(timeout=timeout)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                phone TEXT,
                session_string TEXT,
                templates TEXT NOT NULL DEFAULT '[]',
                min_delay REAL NOT NULL DEFAULT 3.0,
                max_delay REAL NOT NULL DEFAULT 5.0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                is_disabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_login TEXT,
                session_version INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_tokens (
                token_hash TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                target_id INTEGER,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS web_sessions (
                sid_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


init_db()


def get_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def save_user(user_id, phone, session_string):
    with db() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE users SET phone=?, session_string=?, last_login=?,
                    session_version=session_version+1
                WHERE user_id=?
            """, (phone, session_string, utc_now(), user_id))
        else:
            conn.execute("""
                INSERT INTO users(user_id, phone, session_string, created_at, last_login)
                VALUES(?,?,?,?,?)
            """, (user_id, phone, session_string, utc_now(), utc_now()))
        conn.commit()


def hash_token(value):
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    user = get_user(int(uid))
    if not user or user["is_banned"] or user["is_disabled"]:
        session.clear()
        return None
    return user


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("home"))
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or int(user["user_id"]) not in ADMIN_IDS:
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def render_page(title, content, user=None):
    return render_template_string(BASE_HTML, title=title, content=content, user=user)


BASE_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · TITAN</title>
<style>
:root{color-scheme:dark;--bg:#101214;--panel:#191c20;--line:#2b3036;--muted:#9ba3ad;--text:#f2f4f6;--accent:#d5e6ff;--accentText:#111923;--danger:#ff7777}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}
.shell{max-width:1120px;margin:0 auto;padding:28px 18px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:26px}
.brand{font-size:13px;letter-spacing:.18em;font-weight:800;color:#cbd5e1}.brand span{color:#fff}
.nav{display:flex;gap:10px;align-items:center}.nav a,.btn{display:inline-flex;align-items:center;justify-content:center;text-decoration:none;border:1px solid var(--line);border-radius:10px;padding:9px 13px;color:var(--text);background:#20242a;cursor:pointer;font:inherit}
.btn.primary{background:var(--accent);color:var(--accentText);border-color:var(--accent);font-weight:700}.btn.danger{color:var(--danger)}
h1{font-size:clamp(25px,4vw,36px);line-height:1.15;margin:0 0 8px;letter-spacing:-.04em}h2{font-size:18px;margin:0 0 14px}
.sub{color:var(--muted);margin:0 0 24px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:16px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 6px}
input,textarea,select{width:100%;border:1px solid var(--line);background:#101316;color:var(--text);border-radius:10px;padding:12px;font:inherit;outline:none}
textarea{min-height:220px;resize:vertical}input:focus,textarea:focus{border-color:#8799b1}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.row>*{flex:1}.small{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:11px 8px;border-bottom:1px solid var(--line);vertical-align:middle}th{color:var(--muted);font-weight:600}
.status{display:inline-block;border:1px solid var(--line);padding:3px 8px;border-radius:999px;font-size:12px}
.error{color:#ff9292;margin-top:10px}.ok{color:#a7e7bd;margin-top:10px}
@media(max-width:720px){.grid{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}.shell{padding:18px 12px}.panel{padding:15px}table{display:block;overflow-x:auto}}
</style>
</head>
<body><div class="shell">
<header class="top"><div class="brand">TITAN <span>CONTROL</span></div>
<nav class="nav">{% if user %}<a href="/dashboard">Панель</a>{% if user['user_id']|int in admins %}<a href="/admin">Админка</a>{% endif %}<a href="/logout">Выйти</a>{% endif %}</nav></header>
{{ content|safe }}
</div></body></html>
"""


@app.context_processor
def inject_admins():
    return {"admins": ADMIN_IDS}


HOME = r"""
<div class="panel" style="max-width:520px;margin:8vh auto">
<h1>Вход в TITAN</h1><p class="sub">Подключение Telegram-аккаунта по номеру и коду.</p>
<label>Номер телефона</label><input id="phone" placeholder="+7 900 000-00-00" autocomplete="tel">
<button class="btn primary" style="width:100%;margin-top:14px" onclick="sendCode()">Получить код</button>
<div id="codebox" style="display:none"><label>Код Telegram</label><input id="code" autocomplete="one-time-code">
<label>Облачный пароль (если включён)</label><input id="password" type="password" autocomplete="current-password">
<button class="btn primary" style="width:100%;margin-top:14px" onclick="verify()">Войти</button></div>
<div id="msg" class="error"></div>
</div>
<script>
let flow=null;
async function post(url,obj){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});return await r.json()}
async function sendCode(){let p=document.getElementById('phone').value.trim();if(!p){msg.textContent='Введите номер телефона';return}
let d=await post('/api/send-code',{phone:p});if(d.status==='ok'){flow=d.flow;codebox.style.display='block';msg.className='ok';msg.textContent='Код отправлен. Проверьте Telegram.'}else{msg.className='error';msg.textContent=d.error||'Не удалось отправить код'}}
async function verify(){let d=await post('/api/verify-code',{flow:flow,code:document.getElementById('code').value.trim(),password:document.getElementById('password').value});
if(d.status==='ok'){location.href='/dashboard'}else{msg.className='error';msg.textContent=d.error||'Ошибка входа'}}
</script>
"""


@app.route("/")
def home():
    if current_user():
        return redirect("/dashboard")
    return render_page("Вход", HOME)


@app.route("/api/send-code", methods=["POST"])
def api_send_code():
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    if not re.fullmatch(r"\+?[0-9 ()-]{7,24}", phone):
        return jsonify(status="error", error="Проверьте номер телефона.")
    flow = secrets.token_urlsafe(32)

    async def create_and_send():
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        sent = await client.send_code_request(phone)
        return client, sent.phone_code_hash

    try:
        client, code_hash = run_async(create_and_send())
        with auth_lock:
            auth_flows[flow] = {
                "client": client, "phone": phone, "hash": code_hash,
                "created": time.time()
            }
        return jsonify(status="ok", flow=flow)
    except FloodWaitError as e:
        return jsonify(status="error", error=f"Telegram просит подождать {e.seconds} секунд.")
    except Exception as e:
        return jsonify(status="error", error=f"Telegram не отправил код: {str(e)[:240]}")


@app.route("/api/verify-code", methods=["POST"])
def api_verify_code():
    data = request.get_json(silent=True) or {}
    flow = data.get("flow")
    code = (data.get("code") or "").strip()
    password = data.get("password") or ""
    if not flow or not code:
        return jsonify(status="error", error="Введите код.")
    with auth_lock:
        auth = auth_flows.get(flow)
    if not auth:
        return jsonify(status="error", error="Срок входа истёк. Запросите новый код.")
    if time.time() - auth["created"] > AUTH_FLOW_TTL:
        with auth_lock:
            auth_flows.pop(flow, None)
        return jsonify(status="error", error="Срок входа истёк. Запросите новый код.")

    async def finish():
        client = auth["client"]
        try:
            await client.sign_in(
                phone=auth["phone"], code=code, phone_code_hash=auth["hash"]
            )
        except SessionPasswordNeededError:
            if not password:
                return {"need_password": True}
            await client.sign_in(password=password)
        me = await client.get_me()
        return {"user_id": me.id, "session": client.session.save()}

    try:
        result = run_async(finish())
        if result.get("need_password"):
            return jsonify(status="error", error="Нужен облачный пароль Telegram (2FA). Введите его и нажмите «Войти» ещё раз.")
        save_user(result["user_id"], auth["phone"], result["session"])
        with auth_lock:
            auth_flows.pop(flow, None)
        session.clear()
        session["uid"] = int(result["user_id"])
        session.permanent = True
        return jsonify(status="ok")
    except PhoneCodeInvalidError:
        return jsonify(status="error", error="Неверный код Telegram.")
    except PhoneCodeExpiredError:
        return jsonify(status="error", error="Код истёк. Запросите новый.")
    except FloodWaitError as e:
        return jsonify(status="error", error=f"Слишком много попыток. Подождите {e.seconds} секунд.")
    except Exception as e:
        return jsonify(status="error", error=f"Не удалось завершить вход: {str(e)[:240]}")


DASHBOARD = r"""
<h1>Панель управления</h1><p class="sub">Аккаунт подключён. Здесь можно управлять настройками и шаблонами.</p>
<div class="grid">
<div class="panel"><h2>Аккаунт</h2><p>Telegram ID: <b>{{ user['user_id'] }}</b></p>
<p>Телефон: <b>{{ user['phone'] or '—' }}</b></p><p>Статус: <span class="status">Доступ разрешён</span></p></div>
<div class="panel"><h2>Интервал для тестов</h2><p class="small">Минимальное значение зафиксировано на 3 секундах.</p>
<label>Максимальная задержка (сек.)</label><input id="maxdelay" type="number" min="3" max="3600" step="0.1" value="{{ user['max_delay'] }}">
<button class="btn primary" onclick="saveSettings()" style="margin-top:12px">Сохранить</button><div id="settingsmsg"></div></div>
</div>

<div class="panel"><h2>🚀 Запуск рассылки</h2><p class="sub">Введите цель (@username или ID чата) и запустите процесс.</p>
<div class="row">
    <input id="spam_target" placeholder="@username или -100123456789" style="flex:3">
    <button class="btn primary" onclick="startSpam()" style="flex:1">Запустить</button>
    <button class="btn danger" onclick="stopSpam()" style="flex:1">Остановить</button>
</div>
<div id="spammsg" class="small" style="margin-top:10px"></div>
</div>

<div class="panel"><h2>Шаблоны</h2><p class="sub">Каждая строка — отдельный шаблон. Максимум 9999 строк.</p>
<textarea id="templates" placeholder="Введите шаблоны — каждый с новой строки">{{ templates }}</textarea>
<div class="row"><button class="btn primary" onclick="saveTemplates()">Сохранить шаблоны</button><span class="small" id="count"></span></div><div id="templatemsg"></div></div>
<script>
const ta=document.getElementById('templates'),count=document.getElementById('count');
function countLines(){count.textContent=ta.value?ta.value.split('\\n').length+' строк':'0 строк'}ta.addEventListener('input',countLines);countLines();
async function post(url,obj){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});return await r.json()}
async function saveSettings(){let d=await post('/api/settings',{max_delay:document.getElementById('maxdelay').value});settingsmsg.textContent=d.status==='ok'?'Сохранено':d.error;settingsmsg.className=d.status==='ok'?'ok':'error'}
async function saveTemplates(){let d=await post('/api/templates',{text:ta.value});templatemsg.textContent=d.status==='ok'?'Сохранено ('+d.count+' строк)':d.error;templatemsg.className=d.status==='ok'?'ok':'error'}
async function startSpam(){let d=await post('/api/spam/start',{target:document.getElementById('spam_target').value});document.getElementById('spammsg').textContent=d.status==='ok'?'🚀 Запущено':d.error}
async function stopSpam(){let d=await post('/api/spam/stop',{});document.getElementById('spammsg').textContent=d.status==='ok'?'🛑 Остановлено':d.error}
</script>
"""


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    try:
        templates = json.loads(user["templates"] or "[]")
    except Exception:
        templates = []
    content = render_template_string(DASHBOARD, user=user, templates="\n".join(templates))
    return render_page("Панель", content, user=user)


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    data = request.get_json(silent=True) or {}
    try:
        max_delay = float(data.get("max_delay"))
    except (TypeError, ValueError):
        return jsonify(status="error", error="Введите корректную задержку.")
    if not MIN_DELAY_SECONDS <= max_delay <= 3600:
        return jsonify(status="error", error="Максимальная задержка должна быть от 3 до 3600 секунд.")
    uid = int(current_user()["user_id"])
    with db() as conn:
        conn.execute("UPDATE users SET min_delay=?, max_delay=? WHERE user_id=?",
                     (MIN_DELAY_SECONDS, max_delay, uid))
        conn.commit()
    return jsonify(status="ok")


@app.route("/api/templates", methods=["POST"])
@login_required
def api_templates():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    if not isinstance(text, str):
        return jsonify(status="error", error="Некорректный формат.")
    lines = [line.rstrip() for line in text.splitlines()]
    if len(lines) > MAX_TEMPLATE_LINES:
        return jsonify(status="error", error=f"Максимум {MAX_TEMPLATE_LINES} строк.")
    uid = int(current_user()["user_id"])
    with db() as conn:
        conn.execute("UPDATE users SET templates=? WHERE user_id=?",
                     (json.dumps(lines, ensure_ascii=False), uid))
        conn.commit()
    return jsonify(status="ok", count=len(lines))


# ---------- SPAM ENGINE ----------
class SpamEngine:
    def __init__(self, user_id, target, templates, min_d, max_d, session_str):
        self.user_id = user_id
        self.target = target
        self.templates = templates
        self.min_d = min_d
        self.max_d = max_d
        self.session_str = session_str
        self.is_active = False
        self.client = None

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
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 5)
                except Exception:
                    break
        except Exception:
            pass
        finally:
            if self.client: await self.client.disconnect()
            self.is_active = False


@app.route("/api/spam/start", methods=["POST"])
@login_required
def api_spam_start():
    data = request.get_json(silent=True) or {}
    target = data.get("target", "").strip()
    if not target:
        return jsonify(status="error", error="Укажите цель.")
    
    user = current_user()
    uid = int(user["user_id"])
    templates = json.loads(user["templates"] or "[]")
    if not templates:
        return jsonify(status="error", error="Сначала сохраните шаблоны.")
    
    if uid in active_spam_engines:
        return jsonify(status="error", error="Рассылка уже запущена.")

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
    return jsonify(status="error", error="Рассылка не запущена.")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


ADMIN_PAGE = r"""
<h1>Админ-панель</h1><p class="sub">Управление доступом пользователей. Действия фиксируются в журнале.</p>
<div class="panel"><table><thead><tr><th>Telegram ID</th><th>Телефон</th><th>Создан</th><th>Состояние</th><th>Действия</th></tr></thead><tbody>
{% for u in users %}<tr><td>{{ u['user_id'] }}</td><td>{{ u['phone'] or '—' }}</td><td>{{ u['created_at'] or '—' }}</td>
<td>{% if u['is_banned'] %}<span class="status">Бан</span>{% elif u['is_disabled'] %}<span class="status">Отключён</span>{% else %}<span class="status">Активен</span>{% endif %}</td>
<td><div class="row">
<form method="post" action="/admin/action"><input type="hidden" name="uid" value="{{u['user_id']}}"><input type="hidden" name="action" value="{{'unban' if u['is_banned'] else 'ban'}}"><button class="btn" type="submit">{{'Разбанить' if u['is_banned'] else 'Бан'}}</button></form>
<form method="post" action="/admin/action"><input type="hidden" name="uid" value="{{u['user_id']}}"><input type="hidden" name="action" value="{{'enable' if u['is_disabled'] else 'disable'}}"><button class="btn" type="submit">{{'Включить' if u['is_disabled'] else 'Отключить'}}</button></form>
</div></td></tr>{% endfor %}
</tbody></table></div>
<div class="panel"><h2>Последние действия администраторов</h2><table><tr><th>Админ</th><th>Пользователь</th><th>Действие</th><th>Дата</th></tr>
{% for l in logs %}<tr><td>{{l['admin_id']}}</td><td>{{l['target_id']}}</td><td>{{l['action']}}</td><td>{{l['created_at']}}</td></tr>{% endfor %}</table></div>
"""


@app.route("/admin")
@admin_required
def admin():
    with db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        logs = conn.execute("SELECT * FROM admin_logs ORDER BY id DESC LIMIT 50").fetchall()
    user = current_user()
    content = render_template_string(ADMIN_PAGE, users=users, logs=logs)
    return render_page("Админка", content, user=user)


@app.route("/admin/action", methods=["POST"])
@admin_required
def admin_action():
    admin_id = int(current_user()["user_id"])
    try:
        target_id = int(request.form.get("uid", ""))
    except ValueError:
        abort(400)
    action = request.form.get("action")
    if target_id in ADMIN_IDS:
        abort(403)
    mapping = {
        "ban": ("is_banned", 1), "unban": ("is_banned", 0),
        "disable": ("is_disabled", 1), "enable": ("is_disabled", 0),
    }
    if action not in mapping:
        abort(400)
    field, value = mapping[action]
    with db() as conn:
        conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, target_id))
        conn.execute("DELETE FROM web_sessions WHERE user_id=?", (target_id,))
        conn.execute("INSERT INTO admin_logs(admin_id,target_id,action,created_at) VALUES(?,?,?,?)",
                     (admin_id, target_id, action, utc_now()))
        conn.commit()
    
    # Если баним — останавливаем движок
    if action in ("ban", "disable") and target_id in active_spam_engines:
        active_spam_engines[target_id].is_active = False
        del active_spam_engines[target_id]
        
    return redirect("/admin")


@app.route("/health")
def health():
    return jsonify(status="ok", service="titan-control")


@app.errorhandler(403)
def forbidden(_):
    return render_page("Доступ запрещён", "<div class='panel'><h1>403</h1><p>Нет доступа.</p></div>")


# ---------- TELEGRAM BOT ----------
bot_client = TelegramClient("bot_saas", API_ID, API_HASH)

@bot_client.on(events.NewMessage(pattern="/start"))
async def bot_start(event):
    uid = event.sender_id
    user = get_user(uid)
    if not user:
        return await event.reply("Сначала авторизуйтесь на сайте: https://bot-1-dk7y.onrender.com")
    await event.reply(f"Вы авторизованы. Ваш ID: {uid}\nСсылка на панель: https://bot-1-dk7y.onrender.com/dashboard")

def start_bot():
    if not BOT_TOKEN:
        print("BOT_TOKEN не задан. Бот не запущен.")
        return
    run_async(bot_client.start(bot_token=BOT_TOKEN))
    print("Telegram бот запущен.")
    run_async(bot_client.run_until_disconnected())


if __name__ == "__main__":
    # Запускаем бота в фоне (если задан токен)
    threading.Thread(target=start_bot, daemon=True).start()
    
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
