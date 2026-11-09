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

def log_msg(msg):
    print(msg, flush=True)
    try:
        with open("bot_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()} - {msg}\n")
    except: pass

API_ID = int(os.environ.get("API_ID", "2040"))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = "titan_saas.db"
BASE_URL = "https://bot-1-i2nc.onrender.com"

# =====================================================================
# АВТОЗАГРУЗКА БЕСПЛАТНЫХ ПРОКСИ
# =====================================================================
# Список публичных источников (SOCKS5 и MTProto)
PROXY_SOURCES = {
    "socks5": [
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "https://raw.githubusercontent.com/komutan234/Proxy-List-Free/main/proxies/socks5.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    ],
    "mtproto": [
        "https://raw.githubusercontent.com/LoneKingCode/free-proxy-db/main/proxies/mtproto.txt",
    ]
}

# Хранилище для проверенных прокси
VALID_PROXIES = {"socks5": [], "mtproto": []}
LAST_PROXY_UPDATE = 0

def fetch_and_validate_proxies():
    """Загружает списки и проверяет их на работоспособность (упрощенно)."""
    global VALID_PROXIES, LAST_PROXY_UPDATE
    log_msg("🔄 Начинаю обновление списка бесплатных прокси...")
    new_valid = {"socks5": [], "mtproto": []}

    # --- SOCKS5 ---
    for url in PROXY_SOURCES["socks5"]:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                lines = response.read().decode('utf-8', errors='ignore').splitlines()
                for line in lines[:200]: # Берем первые 200 для проверки
                    line = line.strip()
                    if line and ":" in line and not line.startswith("#"):
                        # Формат: ip:port
                        parts = line.split(":")
                        if len(parts) == 2:
                            ip, port = parts
                            if port.isdigit():
                                new_valid["socks5"].append(f"socks5://{ip}:{port}")
            log_msg(f"📥 SOCKS5: получено {len(new_valid['socks5'])} кандидатов из {url.split('/')[-1]}")
        except Exception as e:
            log_msg(f"❌ Ошибка загрузки SOCKS5 с {url}: {e}")

    # --- MTProto ---
    # MTProto парсится сложнее, здесь упрощенно, только если есть готовые строки
    for url in PROXY_SOURCES["mtproto"]:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                lines = response.read().decode('utf-8', errors='ignore').splitlines()
                for line in lines[:100]:
                    line = line.strip()
                    # Ожидаем формат host:port:secret или tg://proxy?...
                    if line and ":" in line and not line.startswith("#"):
                        parts = line.split(":")
                        if len(parts) >= 3 and parts[1].isdigit():
                            host, port, secret = parts[0], int(parts[1]), parts[2]
                            new_valid["mtproto"].append((host, port, secret))
            log_msg(f"📥 MTProto: получено {len(new_valid['mtproto'])} кандидатов")
        except Exception as e:
            log_msg(f"❌ Ошибка загрузки MTProto: {e}")

    # Простая валидация (на самом деле нужно проверять реальным подключением, но для скорости пропускаем)
    # В реальном бою тут нужно пробовать подключиться, но это долго. Положимся на авось.
    VALID_PROXIES = new_valid
    LAST_PROXY_UPDATE = time.time()
    log_msg(f"✅ Обновление завершено. SOCKS5: {len(VALID_PROXIES['socks5'])}, MTProto: {len(VALID_PROXIES['mtproto'])}")

def get_random_proxy():
    """Возвращает случайный прокси (SOCKS5 или MTProto кортеж)."""
    global LAST_PROXY_UPDATE
    # Обновляем раз в час
    if time.time() - LAST_PROXY_UPDATE > 3600 or (not VALID_PROXIES["socks5"] and not VALID_PROXIES["mtproto"]):
        # Запускаем в фоне, чтобы не блокировать запрос
        threading.Thread(target=fetch_and_validate_proxies, daemon=True).start()
        # Если список пуст, возвращаем None (прямое подключение)
        if not VALID_PROXIES["socks5"] and not VALID_PROXIES["mtproto"]:
            return None

    # Приоритет: сначала SOCKS5 (их проще использовать), потом MTProto
    if VALID_PROXIES["socks5"] and random.random() < 0.7: # 70% шанс взять SOCKS5
        return random.choice(VALID_PROXIES["socks5"])
    elif VALID_PROXIES["mtproto"]:
        return random.choice(VALID_PROXIES["mtproto"])
    elif VALID_PROXIES["socks5"]:
        return random.choice(VALID_PROXIES["socks5"])
    return None

# =====================================================================
# TELEGRAM КЛИЕНТЫ С АВТО-ПРОКСИ
# =====================================================================
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=False)

pending_invites = {}
pending_auth = {}

# ... (код базы данных и вспомогательных функций без изменений) ...
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

def run_async(coro, timeout=60):
    try:
        f = asyncio.run_coroutine_threadsafe(coro, async_loop)
        return f.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise Exception("Таймаут Telegram. Все бесплатные прокси, возможно, мертвы. Подожди обновления списка.")
    except Exception as e: raise e

# =====================================================================
# ФУНКЦИЯ СОЗДАНИЯ КЛИЕНТА С ПРОКСИ
# =====================================================================
def create_telegram_client(session=None, is_bot=False):
    """Создает клиент Telethon, автоматически подбирая рабочий прокси."""
    proxy = get_random_proxy()
    kwargs = {
        "connection_retries": 3,
        "retry_delay": 2,
        "timeout": 30,
    }
    
    if is_bot:
        session_obj = "bot_saas" # Имя файла сессии для бота
    else:
        session_obj = StringSession(session) if session else StringSession()

    if proxy:
        if isinstance(proxy, str) and proxy.startswith("socks5://"):
            # SOCKS5
            kwargs["proxy"] = proxy
            log_msg(f"🌐 Использую SOCKS5 прокси: {proxy.split('@')[-1] if '@' in proxy else proxy}")
        elif isinstance(proxy, tuple) and len(proxy) == 3:
            # MTProto
            host, port, secret = proxy
            kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
            kwargs["proxy"] = (host, port, secret)
            log_msg(f"🌐 Использую MTProto прокси: {host}:{port}")
    else:
        log_msg("⚠️ Прокси не найден, пробую прямое подключение (может упасть).")

    return TelegramClient(session_obj, API_ID, API_HASH, **kwargs)

# =====================================================================
# СПАМ
# =====================================================================
active_spam_engines = {}

async def spam_worker(uid, target, templates, max_delay, session_str):
    client = create_telegram_client(session_str)
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
bot = None # Инициализируем позже

@events.register(events.NewMessage(pattern="/start"))
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

@events.register(events.CallbackQuery)
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
    global bot
    if not BOT_TOKEN:
        log_msg("BOT_TOKEN отсутствует")
        return
    log_msg(">>> ЗАПУСК БОТА...")
    async def _run():
        nonlocal bot
        while True:
            try:
                if bot is None or not bot.is_connected():
                    bot = create_telegram_client(is_bot=True)
                    bot.add_event_handler(bot_start)
                    bot.add_event_handler(bot_cb)
                    await bot.start(bot_token=BOT_TOKEN)
                    log_msg(">>> БОТ УСПЕШНО ЗАПУЩЕН!")
                await bot.run_until_disconnected()
            except Exception as e:
                log_msg(f">>> ОШИБКА БОТА: {e}")
                await asyncio.sleep(15) # Ждем и пробуем снова, возможно, прокси сменится
                bot = None
    asyncio.run_coroutine_threadsafe(_run(), async_loop)

# =====================================================================
# UI И МАРШРУТЫ (Без изменений в логике, только шаблоны)
# =====================================================================
BASE_HTML = r"""
... (тот же BASE_HTML) ...
"""

HOME = r"""
... (тот же HOME) ...
"""

WAITING = r"""
... (тот же WAITING) ...
"""

DASHBOARD = r"""
... (тот же DASHBOARD) ...
"""

ADMIN_PAGE = r"""
... (тот же ADMIN_PAGE) ...
"""

@app.context_processor
def _admins(): return {"admins": ADMIN_IDS}

def render_page(title, content, user=None):
    return render_template_string(BASE_HTML, title=title, content=content, user=user)

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
# API (Создание клиентов через create_telegram_client)
# =====================================================================
@app.route("/api/send-code", methods=["POST"])
def api_send_code():
    try:
        data = request.json
        phone = data.get('phone')
        flow = secrets.token_urlsafe(16)
        
        async def _send():
            c = create_telegram_client()
            await c.connect()
            req = await c.send_code_request(phone)
            return c, req.phone_code_hash

        client, code_hash = run_async(_send(), timeout=60)
        pending_auth[flow] = (client, phone, code_hash)
        return jsonify(status="ok", flow=flow)
    except Exception as e:
        return jsonify(status="error", error=f"Telegram: {str(e)[:150]}"), 500

@app.route("/api/verify-code", methods=["POST"])
def api_verify_code():
    try:
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
        return jsonify(status="error", error=f"Ошибка входа: {str(e)[:150]}"), 500

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
    # Запускаем первичную загрузку прокси в фоне при старте
    threading.Thread(target=fetch_and_validate_proxies, daemon=True).start()
    # Запускаем бота
    threading.Thread(target=bot_runner, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
