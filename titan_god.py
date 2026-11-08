import os
import json
import time
import secrets
import sqlite3
import threading
import asyncio
import random
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, redirect, session, abort
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError

# --- НАСТРОЙКИ ---
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
BOT_TOKEN = "8885567581:AAEDY44uIQw5W09dzGA0IFSZUMa05uHd4fI" # Твой новый токен

ADMIN_IDS = {9058235147, 8773171051}
DB_PATH = "titan.db"

app = Flask(__name__)
app.secret_key = "super_secret_key_123"

# Отдельный цикл для бота, чтобы он не конфликтовал с Flask
bot_loop = asyncio.new_event_loop()
asyncio.set_event_loop(bot_loop)

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, phone TEXT, session_string TEXT,
            templates TEXT DEFAULT '[]', max_delay REAL DEFAULT 5.0, is_banned INTEGER DEFAULT 0
        )""")
        conn.commit()

init_db()

def get_user(uid):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def save_user(uid, phone, session_str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO users (user_id, phone, session_string) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET phone=?, session_string=?""", (uid, phone, session_str, phone, session_str))
        conn.commit()

# --- СПАМ-ДВИЖОК ---
active_engines = {}

class SpamEngine:
    def __init__(self, uid, target, templates, max_delay, session_str):
        self.uid = uid; self.target = target; self.templates = templates
        self.max_delay = max_delay; self.session_str = session_str
        self.is_active = True; self.client = None

    async def run(self):
        try:
            self.client = TelegramClient(StringSession(self.session_str), API_ID, API_HASH)
            await self.client.connect()
            while self.is_active:
                try:
                    text = random.choice(self.templates) + "\u200b"
                    await self.client.send_message(self.target, text)
                    await asyncio.sleep(random.uniform(3.0, self.max_delay))
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 5)
                except Exception:
                    break
        except:
            pass
        finally:
            if self.client: await self.client.disconnect()
            self.is_active = False

# --- БОТ ---
bot = TelegramClient("bot_session", API_ID, API_HASH)

@bot.on(events.NewMessage(pattern="/start"))
async def bot_start(event):
    uid = event.sender_id
    is_admin = uid in ADMIN_IDS
    
    token = secrets.token_urlsafe(16)
    link = f"https://bot-1-dk7y.onrender.com/login?token={token}"
    
    buttons = [[Button.url("🌐 Войти в панель", link)]]
    if is_admin:
        buttons.append([Button.inline("👑 Дать доступ кенту", b"invite")])

    await event.reply(f"⚡ **TITAN SaaS**\nВаш ID: `{uid}`", buttons=buttons, parse_mode='md')

@bot.on(events.CallbackQuery)
async def bot_cb(event):
    if event.sender_id not in ADMIN_IDS:
        return await event.answer("Доступ только для админа!", alert=True)
    if event.data == b"invite":
        token = secrets.token_urlsafe(16)
        link = f"https://bot-1-dk7y.onrender.com/login?invite={token}"
        await event.reply(f"🔗 **Ссылка для друга:**\n`{link}`", parse_mode='md')

def start_bot():
    print(">>> ЗАПУСК БОТА...")
    try:
        bot.start(bot_token=BOT_TOKEN)
        print(">>> БОТ УСПЕШНО ЗАПУЩЕН!")
        bot.run_until_disconnected()
    except Exception as e:
        print(f">>> ОШИБКА БОТА: {e}")

# Запуск бота в фоновом потоке прямо при старте скрипта
threading.Thread(target=start_bot, daemon=True).start()

# --- ВЕБ-САЙТ (FLASK) ---
HTML_LOGIN = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>Вход</title>
<style>body{background:#101214;color:#fff;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}
.box{background:#191c20;padding:40px;border-radius:16px;width:300px;text-align:center;}
input{width:100%;padding:12px;margin:10px 0;background:#101316;border:1px solid #333;color:#fff;border-radius:8px;box-sizing:border-box;}
button{width:100%;padding:12px;background:#4f8cff;border:none;color:#fff;border-radius:8px;cursor:pointer;font-weight:bold;}</style></head>
<body><div class="box"><h2>Вход в TITAN</h2>
<div id="s1"><input type="text" id="phone" placeholder="+7...">
<button onclick="sendCode()">Получить код</button></div>
<div id="s2" style="display:none"><input type="text" id="code" placeholder="Код из ТГ">
<button onclick="verify()">Войти</button></div>
<div id="err" style="color:red;margin-top:10px;"></div></div>
<script>
let flow=null;
async function post(url,data){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});return r.json();}
async function sendCode(){let d=await post('/api/send-code',{phone:document.getElementById('phone').value});if(d.status==='ok'){flow=d.flow;s1.style.display='none';s2.style.display='block';}else{err.innerText=d.error;}}
async function verify(){let d=await post('/api/verify-code',{flow:flow,code:document.getElementById('code').value});if(d.status==='ok'){location.href='/dashboard';}else{err.innerText=d.error;}}
</script></body></html>
"""

HTML_DASH = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>Панель</title>
<style>body{background:#101214;color:#fff;font-family:sans-serif;padding:20px;}
.box{background:#191c20;padding:20px;border-radius:12px;max-width:600px;margin:auto;}
input,textarea{width:100%;padding:10px;margin:10px 0;background:#101316;border:1px solid #333;color:#fff;border-radius:8px;box-sizing:border-box;}
button{padding:10px;background:#4f8cff;border:none;color:#fff;border-radius:8px;cursor:pointer;}</style></head>
<body><div class="box">
<h2>Панель управления</h2>
<p>Статус: <span style="color:lime;">Подключено</span></p>
<h3>Шаблоны</h3>
<textarea id="tpl" rows="5">{{ templates }}</textarea>
<button onclick="saveTpl()">Сохранить шаблоны</button>
<h3>Запуск спама</h3>
<input type="text" id="target" placeholder="@username или ID">
<button onclick="startSpam()">Запустить</button>
<button onclick="stopSpam()" style="background:red;">Остановить</button>
<div id="msg" style="margin-top:10px;"></div>
</div>
<script>
async function post(url,data){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});return r.json();}
async function saveTpl(){await post('/api/save_tpl',{text:document.getElementById('tpl').value});alert('Сохранено!');}
async function startSpam(){let d=await post('/api/spam/start',{target:document.getElementById('target').value});msg.innerText=d.status==='ok'?'Запущено!':d.error;}
async function stopSpam(){await post('/api/spam/stop',{});msg.innerText='Остановлено';}
</script></body></html>
"""

# Заглушки для токенов авторизации (чтобы ссылки из бота работали)
valid_tokens = {}

@app.route("/")
def home():
    if "uid" in session: return redirect("/dashboard")
    return HTML_LOGIN

@app.route("/login")
def login():
    # Просто пускаем на главную, токен пока не проверяем для простоты
    return home()

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

    future = asyncio.run_coroutine_threadsafe(_send(), bot_loop)
    try:
        client, code_hash = future.result(timeout=30)
        session["auth_client"] = True # Упрощенно
        # Сохраняем клиент в память (для простоты)
        globals()[f"auth_{flow}"] = (client, phone, code_hash)
        return jsonify(status="ok", flow=flow)
    except Exception as e:
        return jsonify(status="error", error=str(e))

@app.route("/api/verify-code", methods=["POST"])
def api_verify_code():
    data = request.json
    flow = data.get('flow')
    code = data.get('code')
    
    auth_data = globals().get(f"auth_{flow}")
    if not auth_data: return jsonify(status="error", error="Сессия истекла")
    client, phone, code_hash = auth_data

    async def _verify():
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except SessionPasswordNeededError:
            return None
        me = await client.get_me()
        return me.id, client.session.save()

    future = asyncio.run_coroutine_threadsafe(_verify(), bot_loop)
    try:
        res = future.result(timeout=30)
        if not res: return jsonify(status="error", error="Нужен пароль 2FA (пока не поддержан)")
        uid, sess = res
        save_user(uid, phone, sess)
        session["uid"] = uid
        return jsonify(status="ok")
    except Exception as e:
        return jsonify(status="error", error=f"Неверный код: {e}")

@app.route("/dashboard")
def dashboard():
    if "uid" not in session: return redirect("/")
    user = get_user(session["uid"])
    if not user: return redirect("/")
    tpls = json.loads(user["templates"])
    return render_template_string(HTML_DASH, templates="\n".join(tpls))

@app.route("/api/save_tpl", methods=["POST"])
def api_save_tpl():
    if "uid" not in session: return jsonify(status="error")
    lines = request.json.get("text", "").split("\n")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET templates=? WHERE user_id=?", (json.dumps(lines), session["uid"]))
        conn.commit()
    return jsonify(status="ok")

@app.route("/api/spam/start", methods=["POST"])
def api_spam_start():
    if "uid" not in session: return jsonify(status="error")
    target = request.json.get("target")
    user = get_user(session["uid"])
    tpls = json.loads(user["templates"])
    if not tpls: return jsonify(status="error", error="Нет шаблонов")
    
    eng = SpamEngine(session["uid"], target, tpls, user["max_delay"], user["session_string"])
    active_engines[session["uid"]] = eng
    asyncio.run_coroutine_threadsafe(eng.run(), bot_loop)
    return jsonify(status="ok")

@app.route("/api/spam/stop", methods=["POST"])
def api_spam_stop():
    if "uid" in session and session["uid"] in active_engines:
        active_engines[session["uid"]].is_active = False
        del active_engines[session["uid"]]
    return jsonify(status="ok")

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
