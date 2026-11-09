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
import string
import urllib.parse
from colorama import init, Fore, Style
from flask import Flask, jsonify, render_template_string
import aiosqlite

# Telethon imports
from telethon import TelegramClient, events, functions, types, Button
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.errors.rpcerrorlist import AccessTokenInvalidError, ChatWriteForbiddenError

init(autoreset=True)

# =====================================================================
# 1. КОНСТАНТЫ И НАСТРОЙКИ
# =====================================================================
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
BOT_TOKEN = '8732669492:AAF6lfPEgcOp9iKzo74UGnPIPw9wfVa7YGo'

ADMIN_ID = 0 

DB_FILE = 'titan_database.db'
BACKUP_DIR = 'database_backups'

# Глобальные состояния
active_clients = {}
user_steps = {}
spam_engines = {}
auto_responders = {}

system_metrics = {
    "total_messages_sent": 0, 
    "total_flood_wait_events": 0,
    "total_errors_caught": 0, 
    "total_parsing_operations": 0, 
    "total_invitations_sent": 0
}

# =====================================================================
# 2. ЛОГИРОВАНИЕ
# =====================================================================
class TitanLogger:
    @staticmethod
    def log(tag, msg, color=Fore.WHITE):
        t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

    @staticmethod
    def sys(msg): TitanLogger.log("SYS", msg, Fore.CYAN)
    @staticmethod
    def info(msg): TitanLogger.log("INFO", msg, Fore.GREEN)
    @staticmethod
    def warning(msg): TitanLogger.log("WARNING", msg, Fore.YELLOW)
    @staticmethod
    def error(msg): TitanLogger.log("ERROR", msg, Fore.RED)
    @staticmethod
    def critical(msg): print(f"{Fore.RED}{Style.BRIGHT}[CRITICAL] {msg}")

# =====================================================================
# 3. БАЗА ДАННЫХ
# =====================================================================
class TitanDB:
    @staticmethod
    async def bootstrap():
        if not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR)
            
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY, 
                    session_string TEXT, 
                    templates TEXT DEFAULT '[]',
                    min_delay INTEGER DEFAULT 5000, 
                    max_delay INTEGER DEFAULT 15000,
                    is_approved INTEGER DEFAULT 0,
                    is_admin INTEGER DEFAULT 0,
                    registered_at TEXT
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS spam_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    user_id INTEGER, 
                    target TEXT,
                    sent_messages_count INTEGER DEFAULT 0, 
                    status TEXT, 
                    started_at TEXT, 
                    ended_at TEXT
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS parsed_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    owner_id INTEGER, 
                    group_identity TEXT,
                    user_id_tg INTEGER, 
                    username TEXT, 
                    first_name TEXT, 
                    harvested_at TEXT
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS intelligent_replies (
                    user_id INTEGER, 
                    trigger_phrase TEXT, 
                    reply_payload TEXT, 
                    is_regex INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, trigger_phrase)
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS scheduler_queue (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                    user_id INTEGER, 
                    target_peer TEXT,
                    payload_message TEXT, 
                    execution_timestamp TEXT, 
                    execution_status TEXT DEFAULT 'PENDING'
                )
            ''')
            await db.commit()
            TitanLogger.info("Схема БД успешно развернута.")

    @staticmethod
    async def backup():
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(BACKUP_DIR, f"backup_{ts}.db")
            if os.path.exists(DB_FILE):
                shutil.copyfile(DB_FILE, path)
                TitanLogger.info(f"Создан бэкап БД: {path}")
        except Exception as e:
            TitanLogger.error(f"Ошибка бэкапа: {e}")

# =====================================================================
# 4. УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
# =====================================================================
async def get_user(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT session_string, templates, min_delay, max_delay, is_approved, is_admin FROM users WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    'session': row[0], 
                    'templates': json.loads(row[1]), 
                    'min_delay': row[2], 
                    'max_delay': row[3], 
                    'is_approved': row[4],
                    'is_admin': row[5]
                }
    return None

async def save_user(uid, session=None, templates=None, min_d=None, max_d=None, approved=None, admin=None):
    current = await get_user(uid)
    s = session if session is not None else (current['session'] if current else None)
    t = templates if templates is not None else (current['templates'] if current else [])
    mind = min_d if min_d is not None else (current['min_delay'] if current else 5000)
    maxd = max_d if max_d is not None else (current['max_delay'] if current else 15000)
    a = approved if approved is not None else (current['is_approved'] if current else 0)
    adm = admin if admin is not None else (current['is_admin'] if current else 0)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            INSERT INTO users (user_id, session_string, templates, min_delay, max_delay, is_approved, is_admin, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            session_string=excluded.session_string,
            templates=excluded.templates, 
            min_delay=excluded.min_delay, 
            max_delay=excluded.max_delay,
            is_approved=excluded.is_approved,
            is_admin=excluded.is_admin
        ''', (uid, s, json.dumps(t), mind, maxd, a, adm, str(datetime.datetime.now())))
        await db.commit()

async def resolve_client(uid):
    if uid in active_clients:
        return active_clients[uid]
    p = await get_user(uid)
    if not p or not p['session']:
        return None
    try:
        c = TelegramClient(StringSession(p['session']), API_ID, API_HASH)
        await c.connect()
        if await c.is_user_authorized():
            active_clients[uid] = c
            return c
        else:
            TitanLogger.warning(f"Сессия {uid} не авторизована.")
    except Exception as e:
        TitanLogger.error(f"Сбой поднятия сессии {uid}: {e}")
    return None

# =====================================================================
# 5. ДВИЖОК СПАМА (БЕЗ УМНОЙ ЗАДЕРЖКИ)
# =====================================================================
class SpamEngine:
    def __init__(self, client, user_id):
        self.client = client
        self.user_id = user_id
        self.is_active = False
        self.counter = 0

    async def attack(self, target, templates, min_delay, max_delay):
        self.is_active = True
        self.counter = 0
        start_time = str(datetime.datetime.now())
        
        # Очистка ссылки, если ввели https://t.me/username
        if "t.me/" in target:
            target = "@" + target.split("t.me/")[-1].split("/")[0].split("?")[0]

        TitanLogger.info(f"Запуск спам-атаки [{self.user_id}] по цели: {target} (Задержка: {min_delay/1000}-{max_delay/1000} сек)")

        while self.is_active:
            try:
                if not templates:
                    TitanLogger.warning("Шаблоны закончились.")
                    break
                
                msg = random.choice(templates)
                await self.client.send_message(target, msg)
                self.counter += 1
                system_metrics["total_messages_sent"] += 1
                
                TitanLogger.log("SPAM", f"[{self.user_id}] [{self.counter}] -> {target}", Fore.BLUE)
                
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute('''
                        INSERT INTO spam_history (user_id, target, sent_messages_count, status, started_at, ended_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (self.user_id, str(target), self.counter, 'RUNNING', start_time, str(datetime.datetime.now())))
                    await db.commit()

                # Задержка строго по твоим настройкам из БД
                delay = random.randint(min_delay, max_delay) / 1000.0
                await asyncio.sleep(delay)

            except FloodWaitError as e:
                system_metrics["total_flood_wait_events"] += 1
                TitanLogger.warning(f"FloodWait: {e.seconds} сек. для {self.user_id}. Ждем...")
                # Ждем ровно столько, сколько сказал Telegram, и продолжаем с той же задержкой
                await asyncio.sleep(e.seconds + 5)
                
            except ChatWriteForbiddenError:
                system_metrics["total_errors_caught"] += 1
                TitanLogger.error(f"Невозможно писать в {target}. Нет прав или не в чате.")
                self.is_active = False
                break
                
            except Exception as e:
                system_metrics["total_errors_caught"] += 1
                TitanLogger.error(f"Сбой спама [{self.user_id}]: {e}")
                self.is_active = False
                break
        
        self.is_active = False
        TitanLogger.info(f"Атака [{self.user_id}] завершена. Отправлено: {self.counter}")

# =====================================================================
# 6. АВТООТВЕТЧИК
# =====================================================================
class AutoResponder:
    def __init__(self, client, user_id):
        self.client = client
        self.user_id = user_id
        self.rules = {}

    async def load(self):
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT trigger_phrase, reply_payload, is_regex FROM intelligent_replies WHERE user_id = ?", (self.user_id,)) as cursor:
                for row in await cursor.fetchall():
                    self.rules[row[0]] = {"reply": row[1], "is_regex": row[2]}

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
# 7. TELEGRAM UI (BOT)
# =====================================================================
bot = TelegramClient('bot_master', API_ID, API_HASH)

def build_menu(uid, is_admin):
    menu = [
        [Button.inline("👤 Аккаунт", b"nav_account"), Button.inline("📝 Шаблоны", b"nav_templates")],
        [Button.inline("⚙️ Задержка", b"nav_delay"), Button.inline("🚀 Спам", b"nav_spam")],
        [Button.inline("🛑 Стоп", b"nav_stop")]
    ]
    if is_admin:
        menu.append([Button.inline("👑 Выдать доступ", b"nav_admin")])
    return menu

@bot.on(events.NewMessage(pattern='/start'))
async def cmd_start(e):
    global ADMIN_ID
    uid = e.sender_id
    p = await get_user(uid)
    
    if ADMIN_ID == 0:
        ADMIN_ID = uid
        await save_user(uid, admin=1, approved=1)
        TitanLogger.sys(f"Назначен главный админ: {ADMIN_ID}")
        p = await get_user(uid)

    is_admin = p and p['is_admin'] == 1
    is_approved = p and p['is_approved'] == 1

    if not is_admin and not is_approved:
        await e.reply(f"⛔ **Доступ закрыт.**\n\nВаш ID: `{uid}`\nПередайте его администратору.", parse_mode='md')
        return

    user_steps[uid] = "IDLE"
    status = "✅ Подключен" if p and p['session'] else "❌ Нет сессии"
    templates_count = len(p['templates']) if p else 0
    
    text = f"⚡ **TITAN Enterprise v8.0**\n\n"
    text += f"🆔 ID: `{uid}`\n"
    text += f"📱 Аккаунт: {status}\n"
    text += f"📝 Шаблонов: {templates_count}\n"
    text += f"⚙️ Задержка: {p['min_delay']/1000}-{p['max_delay']/1000} сек\n\n"
    text += f"Выбери действие:"
    
    await e.reply(text, buttons=build_menu(uid, is_admin), parse_mode='md')

@bot.on(events.CallbackQuery)
async def cb_handler(e):
    uid = e.sender_id
    action = e.data.decode('utf-8')
    p = await get_user(uid) or {}
    
    is_admin = p.get('is_admin') == 1
    is_approved = p.get('is_approved') == 1

    if not is_admin and not is_approved:
        return await e.answer("⛔ Доступ заблокирован!", alert=True)

    if action == "nav_account":
        if not p.get('session'):
            user_steps[uid] = "ST_PHONE"
            await e.edit("📱 Введите номер телефона (с +):", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        else:
            await e.edit("✅ Ваш аккаунт успешно авторизован.", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
            
    elif action == "nav_templates":
        tpls = p.get('templates', [])
        txt = "\n".join([f"• {t}" for t in tpls[:15]])
        if len(tpls) > 15: txt += f"\n...и еще {len(tpls)-15}"
        if not txt: txt = "Список пуст."
        await e.edit(f"📝 Ваши шаблоны:\n\n{txt}\n\nОтправьте боту текст (каждая строка = новый шаблон).", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        
    elif action == "nav_delay":
        await e.edit(
            f"⚙️ Текущая задержка: {p.get('min_delay', 5000)/1000}-{p.get('max_delay', 15000)/1000} сек\n\n"
            f"Отправьте новые значения через дефис в СЕКУНДАХ (например, 10-20):",
            buttons=[[Button.inline("🔙 Назад", b"nav_home")]]
        )
        user_steps[uid] = "ST_DELAY"
        
    elif action == "nav_spam":
        if not p.get('session'): return await e.answer("❌ Нет сессии!", alert=True)
        if not p.get('templates'): return await e.answer("❌ Нет шаблонов!", alert=True)
        user_steps[uid] = "ST_TARGET"
        await e.edit("🚀 Введите цель (@username или ссылку типа https://t.me/username):")
        
    elif action == "nav_stop":
        if uid in spam_engines:
            spam_engines[uid].is_active = False
            del spam_engines[uid]
        await e.edit("🛑 Все процессы остановлены.", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        
    elif action == "nav_admin" and is_admin:
        await e.edit("👑 **Админ-панель**\n\nДля выдачи доступа отправьте:\n`.approve <ID>`\n\nДля забора доступа:\n`.revoke <ID>`", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        
    elif action == "nav_home":
        user_steps[uid] = "IDLE"
        await e.edit("👋 Главное меню", buttons=build_menu(uid, is_admin))

@bot.on(events.NewMessage)
async def text_handler(e):
    global ADMIN_ID
    uid = e.sender_id
    text = e.text.strip()
    
    if text.startswith('/'): return
    
    p = await get_user(uid) or {}
    is_admin = p.get('is_admin') == 1
    is_approved = p.get('is_approved') == 1

    if is_admin:
        if text.startswith('.approve'):
            try:
                t_id = int(text.split(' ')[1])
                await save_user(t_id, approved=1)
                await e.reply(f"✅ Доступ выдан юзеру `{t_id}`")
            except: await e.reply("❌ Формат: .approve 1234567")
            return
        elif text.startswith('.revoke'):
            try:
                t_id = int(text.split(' ')[1])
                await save_user(t_id, approved=0)
                await e.reply(f"🚫 Доступ забран у `{t_id}`")
            except: await e.reply("❌ Формат: .revoke 1234567")
            return
        elif text == '.id':
            await e.reply(f"Ваш ID: `{uid}`", parse_mode='md')
            return

    if not is_admin and not is_approved:
        return

    step = user_steps.get(uid, "IDLE")

    if step == "ST_PHONE":
        await e.reply("⏳ Отправляю код...")
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await c.connect()
            req = await c.send_code_request(text)
            active_clients[f"temp_{uid}"] = {'client': c, 'phone': text, 'hash': req.phone_code_hash}
            user_steps[uid] = "ST_CODE"
            await e.reply("📩 Введите код из Telegram:")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
            user_steps[uid] = "IDLE"
            
    elif step == "ST_CODE":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
        try:
            await b['client'].sign_in(phone=b['phone'], code=text, phone_code_hash=b['hash'])
            await save_user(uid, session=b['client'].session.save())
            active_clients[uid] = b['client']
            del active_clients[f"temp_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ Сессия успешно создана!")
        except SessionPasswordNeededError:
            user_steps[uid] = "ST_2FA"
            await e.reply("🔐 Введите пароль 2FA:")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
            user_steps[uid] = "IDLE"
            
    elif step == "ST_2FA":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
        try:
            await b['client'].sign_in(password=text)
            await save_user(uid, session=b['client'].session.save())
            active_clients[uid] = b['client']
            del active_clients[f"temp_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ 2FA пройдена, сессия сохранена!")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
            
    elif step == "ST_TARGET":
        user_steps[uid] = "IDLE"
        await e.reply(f"🚀 Запуск по {text}...")
        c = await resolve_client(uid)
        if not c:
            await e.reply("❌ Ошибка: нет активного клиента (сессия потеряна).")
            return
        eng = SpamEngine(c, uid)
        spam_engines[uid] = eng
        asyncio.create_task(eng.attack(text, p['templates'], p['min_delay'], p['max_delay']))
        
    elif step == "ST_DELAY":
        try:
            # Вводим в СЕКУНДАХ, конвертируем в миллисекунды
            low_sec, high_sec = map(int, text.split('-'))
            if low_sec < 1 or high_sec < 1:
                await e.reply("⚠️ Минимальная задержка — 1 секунда!")
                return
            if high_sec < low_sec:
                await e.reply("⚠️ Максимум не может быть меньше минимума!")
                return
            low, high = low_sec * 1000, high_sec * 1000
            await save_user(uid, min_d=low, max_d=high)
            await e.reply(f"✅ Задержка изменена: {low_sec}-{high_sec} сек")
        except:
            await e.reply("❌ Ошибка. Формат: 10-20 (в секундах)")
        user_steps[uid] = "IDLE"
        
    elif step == "IDLE":
        new_lines = [line.strip() for line in text.split('\n') if line.strip()]
        current_templates = p.get('templates', [])
        added = 0
        for line in new_lines:
            if line not in current_templates:
                current_templates.append(line)
                added += 1
                
        if added > 0:
            await save_user(uid, templates=current_templates)
            await e.reply(f"📝 Добавлено шаблонов: {added}\nВсего: {len(current_templates)}")
        else:
            await e.reply("⚠️ Все эти шаблоны уже есть в базе.")

# =====================================================================
# 8. FLASK WEB
# =====================================================================
app = Flask(__name__)
START_TIME = datetime.datetime.now()

@app.route('/')
def index():
    uptime = str(datetime.datetime.now() - START_TIME).split('.')[0]
    html = f"""
    <html><head><title>TITAN V8</title><style>
    body {{ background: #0e1621; color: #fff; font-family: sans-serif; padding: 40px; }}
    .box {{ background: #17212b; padding: 20px; border-radius: 10px; max-width: 600px; margin: auto; }}
    h1 {{ color: #5288c1; }} .stat {{ display: inline-block; background: #24303f; padding: 15px; margin: 5px; border-radius: 5px; text-align: center; }}
    .stat span {{ font-size: 24px; font-weight: bold; color: #2ec4b6; }}
    </style></head><body><div class="box"><h1>TITAN v8.0</h1><p>Uptime: {uptime}</p>
    <div><div class="stat">Отправлено<br><span>{system_metrics['total_messages_sent']}</span></div>
    <div class="stat">Сессий<br><span>{len(active_clients)}</span></div>
    <div class="stat">Ошибок<br><span>{system_metrics['total_errors_caught']}</span></div></div></div></body></html>
    """
    return html

@app.route('/healthz')
def health(): return jsonify({"status": "OK"}), 200

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# =====================================================================
# 9. ЗАПУСК
# =====================================================================
async def main():
    await TitanDB.bootstrap()
    await TitanDB.backup()
    
    threading.Thread(target=run_web, daemon=True).start()
    TitanLogger.sys("Веб-сервер (Flask) запущен на порту 8080")
    
    try:
        await bot.start(bot_token=BOT_TOKEN)
        TitanLogger.info("Telegram Bot успешно запущен!")
        await bot.run_until_disconnected()
    except AccessTokenInvalidError:
        TitanLogger.critical("!!! ОШИБКА ТОКЕНА !!!")
        while True: await asyncio.sleep(3600)
    except Exception as e:
        TitanLogger.critical(f"Критический сбой: {e}")

if __name__ == '__main__':
    TitanLogger.sys("==============================================")
    TitanLogger.sys("    ЗАПУСК TITAN ENTERPRISE v8.0              ")
    TitanLogger.sys("==============================================")
    asyncio.run(main())
