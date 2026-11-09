import os
import json
import time
import secrets
import sqlite3
import threading
import asyncio
import random
import concurrent.futures
import urllib.request
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, render_template_string, redirect, session, abort
from telethon import TelegramClient, events, Button, connection
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError

# =====================================================================
# ТВОЙ НОВЫЙ ТОКЕН (СРОЧНО ПЕРЕВЫПУСТИ ЕГО В BOTFATHER!)
# =====================================================================
os.environ['BOT_TOKEN'] = "8732669492:AAF6lfPEgcOp9iKzo74UGnPIPw9wfVa7YGo"

# ВСТАВЬ СВОИ ДАННЫЕ С my.telegram.org
os.environ['API_ID'] = "2040" # ЗАМЕНИ НА СВОЙ ID
os.environ['API_HASH'] = "b18441a1ff607e10a989891a5462e627" # ЗАМЕНИ НА СВОЙ HASH
os.environ['ADMIN_IDS'] = "9058235147,8773171051" # ТВОЙ ТГ ID

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = "titan_saas.db"
BASE_URL = "https://bot-1-i2nc.onrender.com"

BOT_STATUS = "Инициализация..."
LAST_BOT_ERROR = "Нет ошибок"
PROXY_STATUS = "Не загружены"

def log_msg(msg):
    print(msg, flush=True)

PROXY_SOURCES = {
    "socks5": [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    ]
}
VALID_PROXIES = {"socks5": []}
LAST_PROXY_UPDATE = 0

def fetch_proxies():
    global VALID_PROXIES, LAST_PROXY_UPDATE, PROXY_STATUS
    PROXY_STATUS = "Загрузка прокси..."
    new_valid = {"socks5": []}
    for url in PROXY_SOURCES["socks5"]:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                lines = response.read().decode('utf-8', errors='ignore').splitlines()
                for line in lines[:100]:
                    line = line.strip()
                    if line and ":" in line and not line.startswith("#"):
                        parts = line.split(":")
                        if len(parts) == 2 and parts[1].isdigit():
                            new_valid["socks5"].append(f"socks5://{parts[0]}:{parts[1]}")
        except Exception as e:
            log_msg(f"❌ Ошибка загрузки прокси: {e}")
    
    VALID_PROXIES = new_valid
    LAST_PROXY_UPDATE = time.time()
    PROXY_STATUS = f"Загружено: {len(VALID_PROXIES['socks5'])}"
    log_msg(f"✅ Прокси обновлены: {PROXY_STATUS}")

def get_random_proxy():
    global LAST_PROXY_UPDATE
    if time.time() - LAST_PROXY_UPDATE > 3600 or not VALID_PROXIES["socks5"]:
        if time.time() - LAST_PROXY_UPDATE > 60:
            threading.Thread(target=fetch_proxies, daemon=True).start()
        if not VALID_PROXIES["socks5"]:
            return None
    return random.choice(VALID_PROXIES["socks5"])

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=False)

pending_invites = {}
pending_auth = {}

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
            conn.execute("UPDATE users SET phone=COALESCE(?,phone), session_string=COALESCE(?,session_string) WHERE user_id=?", (phone, session_str, uid))
        else:
            conn.execute("INSERT INTO users(user_id, phone, session_string, created_at) VALUES(?,?,?,?)", (uid, phone, session_str, datetime.now(timezone.utc).isoformat()))
        conn.commit()

def is_admin(uid): return int(uid) in ADMIN_IDS

async_loop = asyncio.new_event_loop()
def _run_loop(l): 
    asyncio.set_event_loop(l)
    l.run_forever()
threading.Thread(target=_run_loop, args=(async_loop,), daemon=True).start()

def run_async(coro, timeout=120):
    try:
        f = asyncio.run_coroutine_threadsafe(coro, async_loop)
        return f.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise Exception("Таймаут прокси")

def create_telegram_client(session=None, is_bot=False):
    proxy = get_random_proxy()
    kwargs = {"connection_retries": 2, "retry_delay": 2, "timeout": 40, "entity_cache_limit": 50} # ОГРАНИЧИВАЕМ ПАМЯТЬ
    
    session_obj = "bot_saas" if is_bot else (StringSession(session) if session else StringSession())
    if proxy:
        kwargs["proxy"] = proxy
    return TelegramClient(session_obj, API_ID, API_HASH, **kwargs)

bot = None

@events.register(events.NewMessage(pattern="/start"))
async def bot_start(event):
    uid = event.sender_id
    log_msg(f"Пришло сообщение от {uid}")
    await event.reply("Бот работает! Я жив, братан.")

def bot_runner():
    global bot, BOT_STATUS, LAST_BOT_ERROR
    BOT_STATUS = "Старт потока..."
    
    if not BOT_TOKEN or not API_ID or not API_HASH:
        BOT_STATUS = "❌ ОШИБКА: НЕТ ТОКЕНА ИЛИ API"
        return

    async def _run():
        global bot, BOT_STATUS, LAST_BOT_ERROR
        while True:
            try:
                if bot is None or not bot.is_connected():
                    BOT_STATUS = "Подключение к TG..."
                    bot = create_telegram_client(is_bot=True)
                    bot.add_event_handler(bot_start)
                    await asyncio.wait_for(bot.start(bot_token=BOT_TOKEN), timeout=120.0)
                    BOT_STATUS = "🟢 БОТ РАБОТАЕТ"
                    LAST_BOT_ERROR = "Нет ошибок"
                    log_msg(">>> 🟢 БОТ УСПЕШНО ЗАПУЩЕН! 🟢")
                
                await bot.run_until_disconnected()
            except asyncio.TimeoutError:
                BOT_STATUS = "❌ Таймаут прокси"
                LAST_BOT_ERROR = "Прокси не отвечает."
                bot = None
                await asyncio.sleep(2)
            except Exception as e:
                BOT_STATUS = "❌ Ошибка"
                LAST_BOT_ERROR = str(e)[:100]
                bot = None
                await asyncio.sleep(5)
                
    asyncio.run_coroutine_threadsafe(_run(), async_loop)

# --- УПРОЩЕННЫЙ UI (чтобы не жрал память) ---
BASE_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>TITAN</title><style>:root{color-scheme:dark;--bg:#101214;--panel:#191c20;--line:#2b3036;--text:#f2f4f6;--accent:#d5e6ff}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 sans-serif;padding:20px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;max-width:500px;margin:50px auto}input,button{width:100%;padding:10px;margin-top:10px;border-radius:8px;border:1px solid #333;background:#222;color:#fff}</style></head><body>{{ content|safe }}</body></html>"""
def render_page(title, content):
    return render_template_string(BASE_HTML, content=content)

HOME = r"""<div class="panel"><h2>Вход в TITAN</h2><input id="phone" placeholder="+7..."><button onclick="sendCode()">Получить код</button><div id="codebox" style="display:none"><input id="code" placeholder="Код"><input id="password" placeholder="2FA пароль (если есть)"><button onclick="verify()">Войти</button></div><div id="msg" style="color:red"></div></div><script>let flow=null;async function sendCode(){let m=document.getElementById('msg');m.textContent='Отправка...';let r=await fetch('/api/send-code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone:document.getElementById('phone').value})});let d=await r.json();if(d.status==='ok'){flow=d.flow;document.getElementById('codebox').style.display='block';m.textContent='Код отправлен'}else{m.textContent=d.error}}async function verify(){let m=document.getElementById('msg');let r=await fetch('/api/verify-code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({flow:flow,code:document.getElementById('code').value,password:document.getElementById('password').value})});let d=await r.json();if(d.status==='ok')location.href='/';else{m.textContent=d.error}}</script>"""

@app.route("/")
@app.route("/login")
def login_page(): return render_page("Вход", HOME)

@app.route("/api/send-code", methods=["POST"])
def api_send_code():
    try:
        data = request.json
        flow = secrets.token_urlsafe(16)
        async def _send():
            c = create_telegram_client()
            await c.connect()
            req = await c.send_code_request(data.get('phone'))
            return c, req.phone_code_hash
        client, code_hash = run_async(_send(), timeout=120)
        pending_auth[flow] = (client, data.get('phone'), code_hash)
        return jsonify(status="ok", flow=flow)
    except Exception as e:
        return jsonify(status="error", error=f"Telegram: {str(e)[:100]}"), 500

@app.route("/api/verify-code", methods=["POST"])
def api_verify_code():
    try:
        data = request.json
        auth_data = pending_auth.get(data.get('flow'))
        if not auth_data: return jsonify(status="error", error="Сессия истекла")
        client, phone, code_hash = auth_data
        async def _verify():
            try: await client.sign_in(phone=phone, code=data.get('code'), phone_code_hash=code_hash)
            except SessionPasswordNeededError:
                if not data.get('password'): return {"need_password": True}
                await client.sign_in(password=data.get('password'))
            me = await client.get_me()
            return {"uid": me.id, "sess": client.session.save()}
        res = run_async(_verify(), timeout=120)
        if isinstance(res, dict) and res.get("need_password"):
            return jsonify(status="error", error="Нужен пароль 2FA")
        save_user(res["uid"], phone=phone, session_str=res["sess"])
        del pending_auth[data.get('flow')]
        session["uid"] = res["uid"]
        return jsonify(status="ok")
    except Exception as e:
        return jsonify(status="error", error=f"Ошибка: {str(e)[:100]}"), 500

@app.route("/health")
def health(): return "OK", 200

@app.route("/api/status")
def api_status():
    return jsonify({"bot_status": BOT_STATUS, "last_error": LAST_BOT_ERROR, "proxy_status": PROXY_STATUS})

# =====================================================================
# ЗАПУСК
# =====================================================================
threading.Thread(target=fetch_proxies, daemon=True).start()
threading.Thread(target=bot_runner, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
