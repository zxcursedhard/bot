from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
import asyncio, random, re, os, datetime, json, urllib.parse, wikipediaapi, qrcode, glob, aiosqlite
from colorama import init, Fore
from flask import Flask
import threading
import aiohttp
import yt_dlp
import imageio_ffmpeg

init(autoreset=True)

try:
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except:
    FFMPEG_PATH = None

app = Flask(__name__)

@app.route('/')
def home():
    return "TITAN BOT IS ALIVE!"

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
BOT_TOKEN = '8732669492:AAF5bx9JLRqKfyGx0TXf4u5KVj18n4d4wYs'

DB_FILE = 'titan_database.db'
active_clients = {}
user_steps = {}

def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            session_string TEXT,
            templates TEXT DEFAULT '[]',
            min_delay INTEGER DEFAULT 100,
            max_delay INTEGER DEFAULT 300
        )''')
        await db.commit()

async def get_user_data(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT session_string, templates, min_delay, max_delay FROM users WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {'session': row[0], 'templates': json.loads(row[1]), 'min_delay': row[2], 'max_delay': row[3]}
    return None

async def save_user_data(uid, session, templates, min_d, max_d):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''INSERT INTO users (user_id, session_string, templates, min_delay, max_delay)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            session_string=CASE WHEN excluded.session_string IS NOT NULL THEN excluded.session_string ELSE users.session_string END,
            templates=excluded.templates, min_delay=excluded.min_delay, max_delay=excluded.max_delay''',
            (uid, session, json.dumps(templates), min_d, max_d))
        await db.commit()

async def get_client(uid):
    if uid in active_clients: return active_clients[uid]
    p = await get_user_data(uid)
    if not p or not p['session']: return None
    try:
        c = TelegramClient(StringSession(p['session']), API_ID, API_HASH)
        await c.connect()
        if await c.is_user_authorized():
            active_clients[uid] = c
            return c
    except Exception as e:
        log("ERROR", f"Ошибка сессии {uid}: {e}", Fore.RED)
    return None

# --- СПАМ-ЛОГИКА ---
async def spam_engine(uid, target, templates, min_d, max_d):
    c = await get_client(uid)
    if not c: return
    while True:
        try:
            text = random.choice(templates) + "\u200b"
            await c.send_message(target, text)
            log("SPAM", f"[{uid}] -> {target}", Fore.BLUE)
            await asyncio.sleep(random.randint(min_d, max_d) / 1000.0)
        except FloodWaitError as e:
            log("FLOOD", f"Бан {e.seconds}с", Fore.RED)
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            log("ERROR", f"Сбой: {e}", Fore.RED)
            break

# --- БОТ ---
bot = TelegramClient('bot_master', API_ID, API_HASH)

def main_menu():
    return [
        [Button.inline("👤 Аккаунт", b"nav_account"), Button.inline("📝 Шаблоны", b"nav_templates")],
        [Button.inline("⚙️ Задержка", b"nav_delay"), Button.inline("🚀 Спам", b"nav_spam")],
        [Button.inline("🛑 Стоп", b"nav_stop")]
    ]

@bot.on(events.NewMessage(pattern='/start'))
async def start(e):
    uid = e.sender_id
    user_steps[uid] = "IDLE"
    p = await get_user_data(uid)
    status = "❌ Нет сессии" if not p or not p['session'] else "✅ Подключен"
    await e.reply(f"⚡ **TITAN Panel**\n\nСтатус: {status}\nШаблонов: {len(p['templates']) if p else 0}", buttons=main_menu(), parse_mode='md')

@bot.on(events.CallbackQuery)
async def cb(e):
    uid = e.sender_id
    action = e.data.decode('utf-8')
    p = await get_user_data(uid) or {'session': None, 'templates': [], 'min_delay': 100, 'max_delay': 300}
    
    if action == "nav_account":
        if not p['session']:
            user_steps[uid] = "ST_PHONE"
            await e.edit("📱 Введи номер (с +):", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        else:
            await e.edit("✅ Сессия активна.", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_templates":
        txt = "\n".join([f"• {t}" for t in p['templates']]) or "Пусто"
        await e.edit(f"📝 Шаблоны:\n\n{txt}\n\nОтправь текст для добавления.", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_delay":
        await e.edit(f"⚙️ Задержка: {p['min_delay']}-{p['max_delay']} мс\n\nОтправь новые значения (например, 500-1000)", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        user_steps[uid] = "ST_DELAY"
    elif action == "nav_spam":
        if not p['session']: await e.answer("❌ Нет сессии!", alert=True)
        elif not p['templates']: await e.answer("❌ Нет шаблонов!", alert=True)
        else:
            user_steps[uid] = "ST_TARGET"
            await e.edit("🚀 Введи цель (@username или ID):")
    elif action == "nav_stop":
        await e.edit("🛑 Всё остановлено (для остановки спама нужно перезапустить бота).", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_home":
        user_steps[uid] = "IDLE"
        await e.edit("👋 Главное меню", buttons=main_menu())

@bot.on(events.NewMessage)
async def msg_router(e):
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
            active_clients[f"temp_{uid}"] = {'client': c, 'phone': text, 'hash': req.phone_code_hash}
            user_steps[uid] = "ST_CODE"
            await e.reply("📩 Введи код из Telegram:")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
            user_steps[uid] = "IDLE"
    elif step == "ST_CODE":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
        try:
            await b['client'].sign_in(phone=b['phone'], code=text, phone_code_hash=b['hash'])
            await save_user_data(uid, b['client'].session.save(), [], 100, 300)
            active_clients[uid] = b['client']
            del active_clients[f"temp_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ Сессия сохранена!")
        except SessionPasswordNeededError:
            user_steps[uid] = "ST_2FA"
            await e.reply("🔐 Введи пароль 2FA:")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
            user_steps[uid] = "IDLE"
    elif step == "ST_2FA":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
        try:
            await b['client'].sign_in(password=text)
            await save_user_data(uid, b['client'].session.save(), [], 100, 300)
            active_clients[uid] = b['client']
            del active_clients[f"temp_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ 2FA пройдена!")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
    elif step == "ST_TARGET":
        p = await get_user_data(uid)
        user_steps[uid] = "IDLE"
        await e.reply(f"🚀 Запуск по {text}")
        asyncio.create_task(spam_engine(uid, text, p['templates'], p['min_delay'], p['max_delay']))
    elif step == "ST_DELAY":
        try:
            low, high = map(int, text.split('-'))
            p = await get_user_data(uid)
            await save_user_data(uid, p['session'], p['templates'], low, high)
            await e.reply(f"✅ Задержка изменена на {low}-{high} мс")
        except:
            await e.reply("❌ Ошибка. Формат: 500-1000")
        user_steps[uid] = "IDLE"
    elif step == "IDLE":
        p = await get_user_data(uid)
        tpls = p['templates'] if p else []
        if text not in tpls:
            tpls.append(text)
            await save_user_data(uid, p['session'] if p else None, tpls, p['min_delay'] if p else 100, p['max_delay'] if p else 300)
            await e.reply("📝 Шаблон добавлен.")

async def main():
    await init_db()
    print("TITAN v41.0 ЗАПУЩЕН")
    threading.Thread(target=run_web, daemon=True).start()
    try:
        await bot.start(bot_token=BOT_TOKEN)
        print("Бот подключен к Telegram")
        await bot.run_until_disconnected()
    except Exception as e:
        print(f"ОШИБКА БОТА: {e}")

if __name__ == '__main__':
    asyncio.run(main())
