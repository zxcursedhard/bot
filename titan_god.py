from telethon import TelegramClient, events, functions, types, Button
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

# --- FFMPEG ---
try:
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except:
    FFMPEG_PATH = None

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
app = Flask(__name__)

@app.route('/')
def home():
    return "TITAN MULTI-TENANT IS ALIVE!"

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# --- КОНФИГУРАЦИЯ ---
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
# Используем переменную окружения, если она есть, иначе токен по умолчанию
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8732669492:AAF5bx9JLRqKfyGx0TXf4u5KVj18n4d4wYs')

# --- БАЗА ДАННЫХ ---
DB_FILE = 'titan_database.db'

# Клиенты (основной бот)
bot = TelegramClient('bot_master', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# Словарь для хранения активных сессий
active_clients = {}

def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                session_string TEXT,
                templates TEXT,
                min_delay INTEGER DEFAULT 100,
                max_delay INTEGER DEFAULT 300
            )
        ''')
        await db.commit()

async def get_user_data(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT session_string, templates, min_delay, max_delay FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    'session': row[0],
                    'templates': json.loads(row[1]) if row[1] else [],
                    'min_delay': row[2],
                    'max_delay': row[3]
                }
    return None

async def save_user_data(user_id, session, templates, min_delay, max_delay):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            INSERT INTO users (user_id, session_string, templates, min_delay, max_delay)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            session_string=excluded.session_string,
            templates=excluded.templates,
            min_delay=excluded.min_delay,
            max_delay=excluded.max_delay
        ''', (user_id, session, json.dumps(templates), min_delay, max_delay))
        await db.commit()

async def get_user_client(user_id):
    if user_id in active_clients:
        return active_clients[user_id]
    
    data = await get_user_data(user_id)
    if not data or not data['session']:
        return None
    
    client = TelegramClient(StringSession(data['session']), API_ID, API_HASH)
    await client.connect()
    active_clients[user_id] = client
    return client

# --- СПАМ-ЛОГИКА ---
async def spam_single_target(user_id, target, message_list, min_d, max_d):
    client = await get_user_client(user_id)
    if not client: return
    
    counter = 0
    while user_id in active_clients:
        try:
            if not message_list: break
            text = random.choice(message_list)
            if random.random() > 0.7: text += "\u200b"
            
            await client.send_message(target, text)
            counter += 1
            log("SPAM", f"[{user_id}] [{counter}] -> {target}", Fore.BLUE)
            
            await asyncio.sleep(random.randint(min_d, max_d) / 1000.0)
        except FloodWaitError as e:
            log("FLOOD", f"[{user_id}] Бан {e.seconds}с", Fore.RED)
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            log("ERROR", f"[{user_id}] Сбой: {e}", Fore.RED)
            break

# --- МЕНЮ ---
def get_main_menu():
    return [
        [Button.inline("👤 Мой аккаунт", b"my_account"), Button.inline("📝 Шаблоны", b"templates")],
        [Button.inline("⚙️ Задержка", b"delay"), Button.inline("🚀 Запустить", b"start_spam")],
        [Button.inline("🛑 Остановить", b"stop_spam")]
    ]

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user_id = event.sender_id
    data = await get_user_data(user_id)
    status = "❌ Не подключен" if not data or not data['session'] else "✅ Подключен"
    
    await event.reply(
        f"👋 **TITAN Multi-Account Panel**\n\n"
        f"Статус аккаунта: {status}\n\n"
        f"Выбери действие:",
        buttons=get_main_menu(),
        parse_mode='md'
    )

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    data = await get_user_data(user_id)
    s_data = data if data else {'session': None, 'templates': [], 'min_delay': 100, 'max_delay': 300}
    cb_data = event.data.decode('utf-8')
    
    if cb_data == "my_account":
        if not s_data['session']:
            await event.edit("🔑 **Подключение аккаунта**\n\nОтправь свой номер телефона в формате: `+79991234567`", buttons=[[Button.inline("🔙 Назад", b"back")]])
        else:
            await event.edit("✅ **Аккаунт уже подключен.**\n\nЧтобы отключить, нажми кнопку ниже.", buttons=[[Button.inline("❌ Отключить аккаунт", b"logout")], [Button.inline("🔙 Назад", b"back")]])
            
    elif cb_data == "templates":
        text = "\n".join([f"{i+1}. {m}" for i, m in enumerate(s_data['templates'][:10])])
        if not text: text = "Пусто."
        await event.edit(f"📝 **Твои шаблоны:**\n\n{text}\n\n*Чтобы изменить, напиши боту текст с новой строки (без точки)*", buttons=[[Button.inline("🔙 Назад", b"back")]], parse_mode='md')
        
    elif cb_data == "delay":
        await event.edit(f"⚙️ **Задержка:** `{s_data['min_delay']}-{s_data['max_delay']}` мс", buttons=[[Button.inline("🔙 Назад", b"back")]])
        
    elif cb_data == "start_spam":
        if not s_data['session']: await event.answer("❌ Сначала подключи аккаунт!", alert=True)
        elif not s_data['templates']: await event.answer("❌ Нет шаблонов!", alert=True)
        else: await event.edit("🚀 **Введи цель для спама:**\n\nОтправь сообщение в формате `@username` или `-100123456789`")
            
    elif cb_data == "stop_spam":
        if user_id in active_clients:
            await active_clients[user_id].disconnect()
            del active_clients[user_id]
        await event.edit("🛑 **Спам остановлен.**", buttons=[[Button.inline("🔙 Назад", b"back")]])
        
    elif cb_data == "back":
        await event.edit("👋 **Главное меню**", buttons=get_main_menu(), parse_mode='md')
        
    elif cb_data == "logout":
        if user_id in active_clients:
            await active_clients[user_id].disconnect()
            del active_clients[user_id]
        await save_user_data(user_id, None, s_data['templates'], s_data['min_delay'], s_data['max_delay'])
        await event.edit("❌ **Аккаунт отключен.**", buttons=[[Button.inline("🔙 Назад", b"back")]])

@bot.on(events.NewMessage)
async def text_handler(event):
    user_id = event.sender_id
    text = event.text
    
    if text.startswith('/'): return
    
    if text.startswith('+') and len(text) > 10:
        await event.reply("⏳ Отправляю код...")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            await client.send_code_request(text)
            active_clients[f"temp_{user_id}"] = {'client': client, 'phone': text}
            await event.reply("📩 **Введи код из Telegram:**")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {e}")
        return
        
    if text.isdigit() and len(text) == 5 and f"temp_{user_id}" in active_clients:
        temp = active_clients[f"temp_{user_id}"]
        client = temp['client']
        phone = temp['phone']
        try:
            await client.sign_in(phone, code=text)
            session_str = client.session.save()
            data = await get_user_data(user_id)
            templates = data['templates'] if data else []
            await save_user_data(user_id, session_str, templates, 100, 300)
            await client.disconnect()
            del active_clients[f"temp_{user_id}"]
            await get_user_client(user_id)
            await event.reply("✅ **Аккаунт успешно подключен!** Теперь напиши свои шаблоны (с новой строки).", buttons=get_main_menu())
        except SessionPasswordNeededError:
            active_clients[f"temp_{user_id}"]['await_password'] = True
            await event.reply("🔐 **Введи облачный пароль (2FA):**")
        except Exception as e:
            await event.reply(f"❌ Ошибка кода: {e}")
        return

    if (text.startswith('@') or text.startswith('-')) and user_id in active_clients and not text.startswith('.'):
        data = await get_user_data(user_id)
        if data and data['templates']:
            asyncio.create_task(spam_single_target(user_id, text, data['templates'], data['min_delay'], data['max_delay']))
            await event.reply(f"🚀 **Спам запущен по:** `{text}`", parse_mode='md')
        return

    data = await get_user_data(user_id)
    if data:
        new_templates = [line.strip() for line in text.split('\n') if line.strip()]
        if new_templates:
            await save_user_data(user_id, data['session'], new_templates, data['min_delay'], data['max_delay'])
            await event.reply(f"✅ **Шаблоны обновлены:** {len(new_templates)} шт.")

async def main():
    print(f"{Fore.CYAN}TITAN MULTI-TENANT v1.0 ЗАПУЩЕН{Fore.RESET}")
    await init_db()
    log("SYS", "База данных инициализирована", Fore.GREEN)
    threading.Thread(target=run_web, daemon=True).start()
    
    try:
        await bot.start()
        log("SYS", "Мастер-бот подключен", Fore.GREEN)
        await bot.run_until_disconnected()
    except Exception as e:
        log("FATAL", f"Ошибка запуска бота: {e}", Fore.RED)

if __name__ == '__main__':
    asyncio.run(main())
