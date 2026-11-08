import os
import sys
import json
import re
import asyncio
import random
import datetime
import threading
import shutil
import csv
import urllib.parse
from colorama import init, Fore, Style
from flask import Flask, jsonify, render_template_string
import aiosqlite
import aiohttp
import wikipediaapi
import qrcode
import imageio_ffmpeg
import yt_dlp

from telethon import TelegramClient, events, functions, types, Button
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.errors.rpcerrorlist import AccessTokenInvalidError

init(autoreset=True)

# =====================================================================
# 1. КОНСТАНТЫ И НАСТРОЙКИ
# =====================================================================
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
# Твой токен
BOT_TOKEN = '8732669492:AAGiMPHbCs8ntMzFhkfS8466iIVVa67w47g' 

DB_FILE = 'titan_database.db'
BACKUP_DIR = 'database_backups'
active_clients = {}
user_steps = {}
spam_engines = {}
auto_responders = {}
system_metrics = {
    "total_messages_sent": 0, "total_flood_wait_events": 0,
    "total_errors_caught": 0, "total_parsing_operations": 0, "total_invitations_sent": 0
}

try:
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = None

# =====================================================================
# 2. РАСШИРЕННОЕ ЛОГИРОВАНИЕ
# =====================================================================
class TitanLogger:
    @staticmethod
    def log(tag, msg, color=Fore.WHITE):
        t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

    @staticmethod
    def debug(msg): TitanLogger.log("DEBUG", msg, Fore.LIGHTBLUE_EX)
    @staticmethod
    def info(msg): TitanLogger.log("INFO", msg, Fore.GREEN)
    @staticmethod
    def warning(msg): TitanLogger.log("WARNING", msg, Fore.YELLOW)
    @staticmethod
    def error(msg): TitanLogger.log("ERROR", msg, Fore.RED)
    @staticmethod
    def critical(msg): print(f"{Fore.RED}{Style.BRIGHT}[CRITICAL] {msg}")

# =====================================================================
# 3. ПОЛНОЦЕННЫЙ ОРМ-СЛОЙ (БАЗА ДАННЫХ)
# =====================================================================
class TitanDB:
    @staticmethod
    async def bootstrap():
        if not os.path.exists(BACKUP_DIR): os.makedirs(BACKUP_DIR)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, session_string TEXT, templates TEXT DEFAULT '[]',
                min_delay INTEGER DEFAULT 100, max_delay INTEGER DEFAULT 300,
                is_admin INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0, registered_at TEXT)''')
            await db.execute('''CREATE TABLE IF NOT EXISTS spam_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, target TEXT,
                sent_messages_count INTEGER DEFAULT 0, status TEXT, started_at TEXT, ended_at TEXT)''')
            await db.execute('''CREATE TABLE IF NOT EXISTS parsed_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, group_identity TEXT,
                user_id_tg INTEGER, username TEXT, first_name TEXT, harvested_at TEXT)''')
            await db.execute('''CREATE TABLE IF NOT EXISTS intelligent_replies (
                user_id INTEGER, trigger_phrase TEXT, reply_payload TEXT, is_regex INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, trigger_phrase))''')
            await db.execute('''CREATE TABLE IF NOT EXISTS scheduler_queue (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, target_peer TEXT,
                payload_message TEXT, execution_timestamp TEXT, execution_status TEXT DEFAULT 'PENDING')''')
            await db.commit()
            TitanLogger.info("База данных инициализирована.")

    @staticmethod
    async def backup():
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(BACKUP_DIR, f"backup_{ts}.db")
            if os.path.exists(DB_FILE): shutil.copyfile(DB_FILE, path)
            TitanLogger.info(f"Бэкап создан: {path}")
        except Exception as e: TitanLogger.error(f"Ошибка бэкапа: {e}")

# =====================================================================
# 4. ДВИЖОК СПАМА (С АНТИДЕТЕКТОМ)
# =====================================================================
class SpamEngine:
    def __init__(self, client, user_id):
        self.client = client
        self.user_id = user_id
        self.is_active = False

    async def attack(self, target, templates, min_delay, max_delay):
        self.is_active = True
        sent = 0
        start = str(datetime.datetime.now())
        while self.is_active:
            try:
                if not templates: break
                msg = random.choice(templates)
                stealth = "".join([random.choice(['\u200b', '\u200c', '\u200d']) for _ in range(random.randint(2, 6))])
                msg = f"{msg}{stealth}"
                await self.client.send_message(target, msg)
                sent += 1
                system_metrics["total_messages_sent"] += 1
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute('''INSERT INTO spam_history (user_id, target, sent_messages_count, status, started_at, ended_at)
                        VALUES (?, ?, ?, ?, ?, ?)''', (self.user_id, str(target), sent, 'RUNNING', start, str(datetime.datetime.now())))
                    await db.commit()
                await asyncio.sleep(random.randint(min_delay, max_delay) / 1000.0)
            except FloodWaitError as e:
                system_metrics["total_flood_wait_events"] += 1
                TitanLogger.warning(f"FloodWait {e.seconds}с для {self.user_id}")
                await asyncio.sleep(e.seconds + 10)
            except Exception as e:
                system_metrics["total_errors_caught"] += 1
                TitanLogger.error(f"Сбой спама {self.user_id}: {e}")
                self.is_active = False
                break
        self.is_active = False

# =====================================================================
# 5. ИНТЕЛЛЕКТУАЛЬНЫЙ АВТООТВЕТЧИК
# =====================================================================
class AutoResponder:
    def __init__(self, client, user_id):
        self.client = client
        self.user_id = user_id
        self.rules = {}

    async def load(self):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT trigger_phrase, reply_payload, is_regex FROM intelligent_replies WHERE user_id = ?", (self.user_id,)) as cursor:
                for row in await cursor.fetchall(): self.rules[row[0]] = {"reply": row[1], "is_regex": row[2]}

    async def handle(self, event):
        if not event.text: return
        text = event.text.lower().strip()
        for trigger, data in self.rules.items():
            match = re.search(trigger, text, re.I) if data["is_regex"] else trigger in text
            if match:
                await asyncio.sleep(random.randint(1000, 3000) / 1000.0)
                await event.reply(data["reply"])
                break

# =====================================================================
# 6. ПАРСЕР И ИНВАЙТЕР
# =====================================================================
class Harvester:
    @staticmethod
    async def extract(client, owner_id, group, limit=1000):
        system_metrics["total_parsing_operations"] += 1
        result = []
        try:
            async for user in client.iter_participants(group, limit=limit):
                if user.username:
                    result.append({"id": user.id, "username": user.username, "name": user.first_name or "None"})
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute('''INSERT INTO parsed_users (owner_id, group_identity, user_id_tg, username, first_name, harvested_at)
                            VALUES (?, ?, ?, ?, ?, ?)''', (owner_id, str(group), user.id, user.username, user.first_name, str(datetime.datetime.now())))
                        await db.commit()
        except Exception as e: TitanLogger.error(f"Ошибка парсинга: {e}")
        return result

class Inviter:
    @staticmethod
    async def invite(client, group, usernames):
        ok, fail = 0, 0
        for u in usernames:
            try:
                await client(functions.channels.InviteToChannelRequest(channel=group, users=[u]))
                ok += 1
                system_metrics["total_invitations_sent"] += 1
                await asyncio.sleep(random.randint(8000, 20000) / 1000.0)
            except FloodWaitError: break
            except Exception:
                fail += 1
                await asyncio.sleep(random.randint(2000, 5000) / 1000.0)
        return ok, fail

# =====================================================================
# 7. ПЛАНИРОВЩИК ЗАДАЧ
# =====================================================================
class Scheduler:
    @staticmethod
    async def loop():
        TitanLogger.info("Планировщик задач запущен.")
        while True:
            try:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                async with aiosqlite.connect(DB_FILE) as db:
                    async with db.execute("SELECT task_id, user_id, target_peer, payload_message FROM scheduler_queue WHERE execution_timestamp <= ? AND execution_status = 'PENDING'", (now,)) as cursor:
                        for t_id, u_id, target, msg in await cursor.fetchall():
                            if u_id in active_clients:
                                try:
                                    await active_clients[u_id].send_message(target, msg)
                                    await db.execute("UPDATE scheduler_queue SET execution_status = 'COMPLETED' WHERE task_id = ?", (t_id,))
                                    await db.commit()
                                except Exception as e: TitanLogger.error(f"Ошибка задачи {t_id}: {e}")
            except Exception as e: TitanLogger.error(f"Сбой планировщика: {e}")
            await asyncio.sleep(30)

# =====================================================================
# 8. УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
# =====================================================================
async def get_user(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT session_string, templates, min_delay, max_delay FROM users WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            if row: return {'session': row[0], 'templates': json.loads(row[1]), 'min_delay': row[2], 'max_delay': row[3]}
    return None

async def save_user(uid, session, templates, min_d, max_d):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''INSERT INTO users (user_id, session_string, templates, min_delay, max_delay, registered_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            session_string=CASE WHEN excluded.session_string IS NOT NULL THEN excluded.session_string ELSE users.session_string END,
            templates=excluded.templates, min_delay=excluded.min_delay, max_delay=excluded.max_delay''',
            (uid, session, json.dumps(templates), min_d, max_d, str(datetime.datetime.now())))
        await db.commit()

async def resolve_client(uid):
    if uid in active_clients: return active_clients[uid]
    p = await get_user(uid)
    if not p or not p['session']: return None
    try:
        c = TelegramClient(StringSession(p['session']), API_ID, API_HASH)
        await c.connect()
        if await c.is_user_authorized():
            active_clients[uid] = c
            r = AutoResponder(c, uid)
            await r.load()
            auto_responders[uid] = r
            @c.on(events.NewMessage(incoming=True))
            async def hook(e): await r.handle(e)
            return c
    except Exception as e: TitanLogger.error(f"Ошибка сессии {uid}: {e}")
    return None

# =====================================================================
# 9. TELEGRAM БОТ-ПАНЕЛЬ (ИНТЕРФЕЙС)
# =====================================================================
bot = TelegramClient('bot_master', API_ID, API_HASH)

def build_menu():
    return [
        [Button.inline("👤 Аккаунт", b"nav_account"), Button.inline("📝 Шаблоны", b"nav_templates")],
        [Button.inline("⚙️ Задержка", b"nav_delay"), Button.inline("🚀 Спам", b"nav_spam")],
        [Button.inline("👥 Парсинг", b"nav_parse"), Button.inline("➕ Инвайт", b"nav_invite")],
        [Button.inline("🤖 Автоответчик", b"nav_ai"), Button.inline("📅 Планировщик", b"nav_schedule")],
        [Button.inline("🛑 Стоп всё", b"nav_kill_all")]
    ]

@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(e):
    uid = e.sender_id
    user_steps[uid] = "IDLE"
    p = await get_user(uid)
    status = "❌ Нет сессии" if not p or not p['session'] else "✅ Подключен"
    text = f"⚡ **TITAN Enterprise v6.0**\n\nСтатус: {status}\nБаза: {DB_FILE}\n\nВыбери действие:"
    await e.reply(text, buttons=build_menu(), parse_mode='md')

@bot.on(events.CallbackQuery)
async def callback(e):
    uid = e.sender_id
    action = e.data.decode('utf-8')
    p = await get_user(uid) or {'session': None, 'templates': [], 'min_delay': 100, 'max_delay': 300}
    
    if action == "nav_account":
        if not p['session']:
            user_steps[uid] = "ST_PHONE"
            await e.edit("📱 Введи номер телефона (с +):", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        else:
            await e.edit("✅ Сессия активна.", buttons=[[Button.inline("❌ Выйти", b"nav_logout")], [Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_templates":
        txt = "\n".join([f"• {t}" for t in p['templates']]) or "Пусто"
        await e.edit(f"📝 Шаблоны:\n\n{txt}\n\nОтправь текст для добавления.", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_delay":
        await e.edit(f"⚙️ Задержка: {p['min_delay']}-{p['max_delay']} мс\nОтправь новые (например 500-1000)", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        user_steps[uid] = "ST_DELAY"
    elif action == "nav_spam":
        if not p['session']: await e.answer("❌ Нет сессии!", alert=True)
        elif not p['templates']: await e.answer("❌ Нет шаблонов!", alert=True)
        else:
            user_steps[uid] = "ST_TARGET"
            await e.edit("🚀 Введи цель (@username или ID):")
    elif action == "nav_parse":
        if not p['session']: await e.answer("❌ Нет сессии!", alert=True)
        else:
            user_steps[uid] = "ST_PARSE_GROUP"
            await e.edit("👥 Введи ссылку на группу для парсинга:")
    elif action == "nav_invite":
        if not p['session']: await e.answer("❌ Нет сессии!", alert=True)
        else:
            user_steps[uid] = "ST_INVITE_GROUP"
            await e.edit("➕ Введи группу для инвайтинга:")
    elif action == "nav_ai":
        user_steps[uid] = "ST_AI_RULE"
        await e.edit("🤖 Введи правило (триггер | ответ):", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_schedule":
        user_steps[uid] = "ST_SCHED_TASK"
        await e.edit("📅 Введи задачу (@target | ГГГГ-ММ-ДД ЧЧ:ММ | текст):", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_kill_all":
        if uid in spam_engines: spam_engines[uid].is_active = False; del spam_engines[uid]
        await e.edit("🛑 Всё остановлено.", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_home":
        user_steps[uid] = "IDLE"
        await e.edit("👋 Главное меню", buttons=build_menu())
    elif action == "nav_logout":
        if uid in active_clients:
            try: await active_clients[uid].disconnect()
            except: pass
            del active_clients[uid]
        await save_user(uid, None, p['templates'], p['min_delay'], p['max_delay'])
        await e.edit("❌ Сессия удалена.", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])

@bot.on(events.NewMessage)
async def router(e):
    uid = e.sender_id
    text = e.text.strip()
    if text.startswith('/'): return
    step = user_steps.get(uid, "IDLE")

    if step == "ST_PHONE":
        await e.reply("⏳ Отправляю код...")
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await c.connect()
            req = await c.send_code_request(text)
            active_clients[f"pending_{uid}"] = {'client': c, 'phone': text, 'hash': req.phone_code_hash}
            user_steps[uid] = "ST_CODE"
            await e.reply("📩 Введи код из Telegram:")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
            user_steps[uid] = "IDLE"
    elif step == "ST_CODE":
        b = active_clients.get(f"pending_{uid}")
        if not b: return
        try:
            await b['client'].sign_in(phone=b['phone'], code=text, phone_code_hash=b['hash'])
            await save_user(uid, b['client'].session.save(), [], 100, 300)
            active_clients[uid] = b['client']
            del active_clients[f"pending_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ Сессия сохранена!")
        except SessionPasswordNeededError:
            user_steps[uid] = "ST_2FA"
            await e.reply("🔐 Введи пароль 2FA:")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
            user_steps[uid] = "IDLE"
    elif step == "ST_2FA":
        b = active_clients.get(f"pending_{uid}")
        if not b: return
        try:
            await b['client'].sign_in(password=text)
            await save_user(uid, b['client'].session.save(), [], 100, 300)
            active_clients[uid] = b['client']
            del active_clients[f"pending_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ 2FA пройдена!")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
    elif step == "ST_TARGET":
        p = await get_user(uid)
        c = await resolve_client(uid)
        if not c: return
        eng = SpamEngine(c, uid)
        spam_engines[uid] = eng
        user_steps[uid] = "IDLE"
        await e.reply(f"🚀 Запуск по {text}")
        asyncio.create_task(eng.attack(text, p['templates'], p['min_delay'], p['max_delay']))
    elif step == "ST_PARSE_GROUP":
        c = await resolve_client(uid)
        if not c: return
        await e.reply("⏳ Парсинг...")
        members = await Harvester.extract(c, uid, text)
        await e.reply(f"✅ Собрано: {len(members)}" if members else "❌ Ничего не найдено.")
        user_steps[uid] = "IDLE"
    elif step == "ST_INVITE_GROUP":
        c = await resolve_client(uid)
        if not c: return
        usernames = []
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT username FROM parsed_users WHERE owner_id = ?", (uid,)) as cursor:
                usernames = [r[0] for r in await cursor.fetchall()]
        if not usernames:
            await e.reply("❌ Нет базы для инвайта.")
            user_steps[uid] = "IDLE"
            return
        await e.reply(f"👥 Инвайт {len(usernames)} человек...")
        ok, fail = await Inviter.invite(c, text, usernames)
        await e.reply(f"✅ Успешно: {ok} | Ошибок: {fail}")
        user_steps[uid] = "IDLE"
    elif step == "ST_AI_RULE":
        try:
            trig, rep = text.split("|", 1)
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("INSERT OR REPLACE INTO intelligent_replies (user_id, trigger_phrase, reply_payload) VALUES (?, ?, ?)", (uid, trig.strip().lower(), rep.strip()))
                await db.commit()
            if uid in auto_responders: await auto_responders[uid].load()
            await e.reply(f"✅ Правило для '{trig.strip()}' добавлено.")
        except:
            await e.reply("❌ Формат: триггер | ответ")
        user_steps[uid] = "IDLE"
    elif step == "ST_SCHED_TASK":
        try:
            parts = text.split("|")
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("INSERT INTO scheduler_queue (user_id, target_peer, payload_message, execution_timestamp) VALUES (?, ?, ?, ?)", (uid, parts[0].strip(), parts[2].strip(), parts[1].strip()))
                await db.commit()
            await e.reply("✅ Задача запланирована.")
        except:
            await e.reply("❌ Формат: @target | ГГГГ-ММ-ДД ЧЧ:ММ | текст")
        user_steps[uid] = "IDLE"
    elif step == "IDLE":
        p = await get_user(uid)
        tpls = p['templates'] if p else []
        if text not in tpls:
            tpls.append(text)
            await save_user(uid, p['session'] if p else None, tpls, p['min_delay'] if p else 100, p['max_delay'] if p else 300)
            await e.reply("📝 Шаблон добавлен.")

# =====================================================================
# 10. FLASK WEB DASHBOARD (ДЛЯ RENDER)
# =====================================================================
app = Flask(__name__)
START_TIME = datetime.datetime.now()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>TITAN ENTERPRISE DASHBOARD</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: 'Segoe UI', sans-serif; background: #0e1621; color: #fff; margin: 0; padding: 40px; }
        .container { max-width: 900px; margin: auto; background: #17212b; padding: 30px; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.5); }
        h1 { color: #5288c1; border-bottom: 2px solid #24303f; padding-bottom: 15px; }
        .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-top: 25px; }
        .card { background: #24303f; padding: 20px; border-radius: 8px; text-align: center; border: 1px solid #2b394a; }
        .card h3 { margin: 0 0 10px 0; color: #8ab4f8; font-size: 14px; text-transform: uppercase; }
        .card p { margin: 0; font-size: 28px; font-weight: bold; color: #fff; }
        .status { display: inline-block; padding: 6px 14px; background: #2ec4b6; color: #fff; border-radius: 20px; font-weight: bold; font-size: 14px; }
        footer { margin-top: 40px; text-align: center; color: #6c7a89; font-size: 13px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>TITAN OMNI COMPONENT V6.0</h1>
        <p>Статус платформы: <span class="status">ONLINE</span></p>
        <p>Время аптайма: {{ uptime }}</p>
        <p>Активных сессий: {{ sessions }}</p>
        <div class="grid">
            <div class="card"><h3>Отправлено</h3><p>{{ m.total_messages_sent }}</p></div>
            <div class="card"><h3>FloodWait</h3><p>{{ m.total_flood_wait_events }}</p></div>
            <div class="card"><h3>Ошибок</h3><p>{{ m.total_errors_caught }}</p></div>
            <div class="card"><h3>Парсинг</h3><p>{{ m.total_parsing_operations }}</p></div>
            <div class="card"><h3>Инвайтов</h3><p>{{ m.total_invitations_sent }}</p></div>
        </div>
        <footer>TITAN Multi-Tenant System Node • {{ current_time }}</footer>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    uptime = str(datetime.datetime.now() - START_TIME).split('.')[0]
    return render_template_string(HTML_TEMPLATE, uptime=uptime, sessions=len(active_clients), m=system_metrics, current_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.route('/healthz')
def health():
    return jsonify({"status": "OK", "time": str(datetime.datetime.now())}), 200

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# =====================================================================
# 11. ГЛАВНЫЙ ЗАПУСК
# =====================================================================
async def main():
    await TitanDB.bootstrap()
    await TitanDB.backup()
    asyncio.create_task(Scheduler.loop())
    
    try:
        await bot.start(bot_token=BOT_TOKEN)
        TitanLogger.info("Бот успешно подключен к Telegram.", Fore.GREEN)
        await bot.run_until_disconnected()
    except AccessTokenInvalidError:
        TitanLogger.error("!!! НЕВЕРНЫЙ ТОКЕН БОТА !!!")
        TitanLogger.warning("Бот не запущен. Веб-сервер (Flask) продолжает работать.")
        while True: await asyncio.sleep(3600)
    except Exception as e:
        TitanLogger.error(f"Критическая ошибка: {e}")

def start_async():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())

if __name__ == '__main__':
    TitanLogger.info("==========================================================", Fore.MAGENTA)
    TitanLogger.info("   STARTING TITAN CENTRAL INDUSTRIAL DISTRIBUTION SYSTEM  ", Fore.MAGENTA)
    TitanLogger.info("==========================================================", Fore.MAGENTA)
    threading.Thread(target=run_web, daemon=True).start()
    TitanLogger.info("Служба удержания HTTP-трафика развернута на порту 8080.")
    start_async()
