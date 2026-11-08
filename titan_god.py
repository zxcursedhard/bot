from telethon import TelegramClient, events, functions, types, Button
from telethon.errors import FloodWaitError
import asyncio, random, re, os, datetime, json, urllib.parse, wikipediaapi, qrcode, glob
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

# --- КОНФИГУРАЦИЯ ---
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
BOT_TOKEN = '8732669492:AAF5bx9JLRqKfyGx0TXf4u5KVj18n4d4wYs'

TEMPLATES_FILE = 'templates.json'
CONFIG_FILE = 'config.json'

MESSAGES = []
min_delay = 100
max_delay = 300
is_spamming = False
targets = []

# --- ДВА КЛИЕНТА ---
# 1. Юзербот (твой аккаунт)
userbot = TelegramClient('my_account', API_ID, API_HASH)
# 2. Бот-настройщик (через токен)
bot = TelegramClient('bot_account', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

def load_data():
    global MESSAGES, min_delay, max_delay
    if os.path.exists(TEMPLATES_FILE):
        try:
            with open(TEMPLATES_FILE, 'r', encoding='utf-8') as f:
                MESSAGES = json.load(f)
        except: pass
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                min_delay = cfg.get('min_delay', 100)
                max_delay = cfg.get('max_delay', 300)
        except: pass

def save_templates():
    try:
        with open(TEMPLATES_FILE, 'w', encoding='utf-8') as f:
            json.dump(MESSAGES, f, ensure_ascii=False, indent=2)
    except: pass

def save_config():
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({'min_delay': min_delay, 'max_delay': max_delay}, f)
    except: pass

async def fetch_url(url, headers=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as response:
                return await response.text()
    except Exception as e:
        return f"ERROR: {e}"

# --- СПАМ ЧЕРЕЗ ЮЗЕРБОТА ---
async def spam_single_target(target):
    global is_spamming
    counter = 0
    while is_spamming:
        try:
            if not MESSAGES: break
            text = random.choice(MESSAGES)
            if random.random() > 0.7: text += "\u200b"
            
            await userbot.send_message(target, text)
            counter += 1
            log("SPAM", f"[{counter}] -> {target}", Fore.BLUE)
            
            await asyncio.sleep(random.randint(min_delay, max_delay) / 1000.0)
        except FloodWaitError as e:
            log("FLOOD", f"Бан {e.seconds}с", Fore.RED)
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            log("ERROR", f"Сбой: {e}", Fore.RED)
            break

async def spam_loop():
    global is_spamming, targets
    if not targets:
        is_spamming = False
        return
    log("SPAM", f"Запуск по {len(targets)}", Fore.YELLOW)
    tasks = [asyncio.create_task(spam_single_target(t)) for t in targets]
    await asyncio.gather(*tasks)
    is_spamming = False
    log("SPAM", "Потоки завершены.", Fore.YELLOW)

# --- МЕНЮ БОТА-НАСТРОЙЩИКА ---
def get_menu():
    buttons = [
        [Button.inline("📝 Шаблоны", b"templates"), Button.inline("⚙️ Задержка", b"delay")],
        [Button.inline("🚀 Запустить спам", b"start_spam"), Button.inline("🛑 Остановить", b"stop_spam")],
        [Button.inline("📊 Статистика", b"stats")]
    ]
    return buttons

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply(
        f"👋 **TITAN Setup Panel**\n\n"
        f"Шаблонов: `{len(MESSAGES)}`\n"
        f"Задержка: `{min_delay}-{max_delay} мс`\n"
        f"Статус: `{'Активен' if is_spamming else 'Выключен'}`",
        buttons=get_menu(),
        parse_mode='md'
    )

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    global is_spamming, targets, min_delay, max_delay, MESSAGES
    
    data = event.data.decode('utf-8')
    
    if data == "templates":
        text = "\n".join([f"{i+1}. {m}" for i, m in enumerate(MESSAGES[:10])])
        await event.edit(f"📝 **Шаблоны (первые 10):**\n\n{text}", buttons=[[Button.inline("🔙 Назад", b"back")]], parse_mode='md')
    
    elif data == "delay":
        await event.edit(f"⚙️ **Задержка:** `{min_delay}-{max_delay}` мс\n\nЧтобы изменить, напиши в Избранное: `.задержка 1000-2000`", buttons=[[Button.inline("🔙 Назад", b"back")]], parse_mode='md')
    
    elif data == "start_spam":
        if not MESSAGES:
            await event.answer("❌ Нет шаблонов!", alert=True)
        elif not targets:
            await event.answer("❌ Нет целей! Напиши в Избранное `.спам @user`", alert=True)
        else:
            await event.edit("🚀 **Спам запущен!**", buttons=[[Button.inline("🛑 Остановить", b"stop_spam")]])
            if not is_spamming:
                is_spamming = True
                asyncio.create_task(spam_loop())
    
    elif data == "stop_spam":
        is_spamming = False
        await event.edit("🛑 **Спам остановлен.**", buttons=[[Button.inline("🔙 Назад", b"back")]])
    
    elif data == "stats":
        await event.edit(f"📊 **Статистика:**\nШаблонов: `{len(MESSAGES)}`\nЗадержка: `{min_delay}-{max_delay}` мс\nСтатус: `{'Активен' if is_spamming else 'Выключен'}`", buttons=[[Button.inline("🔙 Назад", b"back")]], parse_mode='md')
        
    elif data == "back":
        await event.edit("👋 **TITAN Setup Panel**\n\nВыбери действие:", buttons=get_menu(), parse_mode='md')

# --- КОМАНДЫ В ИЗБРАННОМ (ЮЗЕРБОТ) ---
@userbot.on(events.NewMessage(outgoing=True))
async def handler(e):
    global MESSAGES, min_delay, max_delay, is_spamming, targets
    
    if not e.text or not e.text.startswith('.'): return
    text = e.text[1:]
    parts = text.split(' ', 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == 'шаблоны':
        MESSAGES = [line.strip() for line in arg.split('\n') if line.strip()]
        save_templates()
        await e.edit(f"✅ Сохранено: {len(MESSAGES)}")

    elif cmd == 'спам':
        if not MESSAGES: return await e.edit("❌ Нет шаблонов!")
        targets = [x.strip() for x in arg.split(',')]
        if not is_spamming:
            is_spamming = True
            asyncio.create_task(spam_loop())
            await e.edit(f"🚀 Запуск по {len(targets)}")
        else:
            await e.edit("⚠️ Уже работает")

    elif cmd == 'стоп':
        is_spamming = False
        await e.edit("🛑 СТОП")

    elif cmd == 'задержка':
        try:
            if '-' in arg: min_delay, max_delay = map(int, arg.split('-'))
            else: min_delay = max_delay = int(arg)
            save_config()
            await e.edit(f"⏳ Задержка: {min_delay}-{max_delay} мс")
        except: pass

    elif cmd == 'gpt':
        if not arg: return
        await e.edit("🤖...")
        res = await fetch_url(f"https://text.pollinations.ai/{urllib.parse.quote(arg)}")
        await e.edit(res[:4000] if not res.startswith("ERROR") else res)

    elif cmd == 'картинка':
        if not arg: return
        await e.edit("🎨...")
        try:
            img_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(arg)}?width=1024&height=1024&nologo=true"
            await userbot.send_file(e.chat_id, img_url, caption=f"Запрос: {arg}")
            await e.delete()
        except Exception as ex: await e.edit(f"❌ {ex}")

    elif cmd == 'вики':
        if not arg: return
        await e.edit("🕵️...")
        try:
            wiki = wikipediaapi.Wikipedia('TitanBot/1.0', 'ru')
            page = wiki.page(arg)
            if not page.exists(): return await e.edit("❌ Нет статьи")
            await e.edit(f"**{page.title}**\n\n{page.summary[:1000]}\n\n[Читать]({page.fullurl})", parse_mode='md', link_preview=False)
        except: pass

    elif cmd == 'скачать':
        if not arg: return
        await e.edit("📥...")
        for f in glob.glob("video.*"): os.remove(f)
        try:
            ydl_opts = {'format': 'best[ext=mp4]/best', 'outtmpl': 'video.%(ext)s', 'quiet': True, 'ffmpeg_location': FFMPEG_PATH}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([arg.strip()])
            for f in glob.glob("video.*"):
                await userbot.send_file(e.chat_id, f)
                os.remove(f)
                break
            await e.delete()
        except Exception as ex: await e.edit(f"❌ {ex}")

    elif cmd == 'парсинг':
        if not arg: return
        await e.edit("📦...")
        try:
            users = []
            async for u in userbot.iter_participants(arg.strip()):
                if u.username: users.append(f"@{u.username}")
            with open('users.txt', 'w') as f: f.write('\n'.join(users))
            await userbot.send_file(e.chat_id, 'users.txt', caption=f"✅ {len(users)}")
            await e.delete()
        except Exception as ex: await e.edit(f"❌ {ex}")

    elif cmd == 'инфо':
        try:
            u = await userbot.get_entity(arg.strip())
            await e.edit(f"**ID:** `{u.id}`\n**Ник:** `@{u.username}`")
        except: await e.edit("❌ Не найден")

    elif cmd == 'профиль':
        me = await userbot.get_me()
        await e.edit(f"**ID:** `{me.id}`\n**Имя:** `{me.first_name}`")

    elif cmd == 'пинг':
        s = datetime.datetime.now()
        await e.edit("📡")
        await e.edit(f"`{(datetime.datetime.now() - s).microseconds / 1000:.1f} мс`")

    elif cmd == 'хелп':
        await e.edit("**Команды:**\n.шаблоны, .спам, .стоп, .задержка\n.gpt, .картинка, .вики\n.скачать, .парсинг, .инфо\n.профиль, .пинг\n\n**Меню:** /start")

async def main():
    print(f"{Fore.CYAN}TITAN v40.0 (BOT + USERBOT) ЗАПУЩЕН{Fore.RESET}")
    load_data()
    log("SYS", f"Шаблонов: {len(MESSAGES)}", Fore.GREEN)
    log("SYS", f"Задержка: {min_delay}-{max_delay} мс", Fore.GREEN)
    
    threading.Thread(target=run_web, daemon=True).start()
    
    # Запускаем юзербота
    await userbot.start()
    log("SYS", "Юзербот подключен", Fore.GREEN)
    
    # Запускаем бота-настройщика
    await bot.start()
    log("SYS", "Бот-настройщик подключен", Fore.GREEN)
    
    # Держим оба активными
    await asyncio.gather(
        userbot.run_until_disconnected(),
        bot.run_until_disconnected()
    )

if __name__ == '__main__':
    asyncio.run(main())
