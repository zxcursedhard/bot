from telethon import TelegramClient, events, functions, types
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.errors import FloodWaitError
import asyncio, random, re, os, datetime, string, json, urllib.parse, wikipediaapi, qrcode, pytz
from PIL import Image
from colorama import init, Fore
from flask import Flask
import threading
import aiohttp
import yt_dlp

init(autoreset=True)

# --- ФЕЙКОВЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ---
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
TEMPLATES_FILE = 'templates.json'

# Глобальные переменные
MESSAGES = []
min_delay = 2000
max_delay = 4000
is_spamming = False
targets = []
afk_enabled = False
afk_text = "Отошел, отвечу позже."
auto_reply_enabled = False
auto_reply_text = "Занят, напишу позже."

client = TelegramClient('my_account', API_ID, API_HASH)

# --- ЛОГГИРОВАНИЕ ---
def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

# --- РАБОТА С ФАЙЛАМИ ---
def load_data():
    global MESSAGES
    if os.path.exists(TEMPLATES_FILE):
        try:
            with open(TEMPLATES_FILE, 'r', encoding='utf-8') as f:
                MESSAGES = json.load(f)
        except Exception as e:
            log("ERROR", f"Ошибка загрузки шаблонов: {e}", Fore.RED)
            MESSAGES = []

def save_templates():
    try:
        with open(TEMPLATES_FILE, 'w', encoding='utf-8') as f:
            json.dump(MESSAGES, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("ERROR", f"Ошибка сохранения: {e}", Fore.RED)

# --- АСИНХРОННЫЕ ЗАПРОСЫ (БЕЗ БЛОКИРОВКИ СПАМА) ---
async def fetch_url(url, headers=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as response:
                return await response.text()
    except Exception as e:
        return f"ERROR: {e}"

# --- ЦИКЛ СПАМА ---
async def spam_loop():
    global is_spamming, targets
    counter = 0
    while is_spamming:
        try:
            if not targets:
                is_spamming = False
                log("SPAM", "Очередь целей пуста. Остановка.", Fore.YELLOW)
                break
            
            target = targets[0]
            
            if not MESSAGES:
                log("SPAM", "Нет шаблонов для отправки!", Fore.RED)
                is_spamming = False
                break

            text = random.choice(MESSAGES)
            
            # Анти-бан: эмуляция набора текста (иногда)
            if random.random() > 0.5:
                try:
                    async with client.action(target, 'typing'):
                        await asyncio.sleep(random.uniform(1.0, 2.5))
                except: pass
            
            # Анти-бан: невидимый символ
            if random.random() > 0.8:
                text += "\u200b"
                
            await client.send_message(target, text)
            counter += 1
            log("SPAM", f"[{counter}] -> {target}", Fore.BLUE)
            
            # Рандомная пауза
            wait_time = random.randint(min_delay, max_delay) / 1000.0
            
            # Иногда делаем "умную" паузу (как будто отвлеклись)
            if random.random() > 0.9:
                extra = random.uniform(5.0, 10.0)
                log("SPAM", f"Пауза на подумать {extra:.1f}с", Fore.YELLOW)
                wait_time += extra
                
            await asyncio.sleep(wait_time)
            
        except FloodWaitError as e:
            log("FLOOD", f"Бан на {e.seconds} секунд. Ждем...", Fore.RED)
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            log("ERROR", f"Сбой отправки: {e}", Fore.RED)
            if targets: targets.pop(0)
            await asyncio.sleep(2)

# --- ОСНОВНОЙ ОБРАБОТЧИК КОМАНД ---
@client.on(events.NewMessage(outgoing=True))
async def handler(e):
    global MESSAGES, min_delay, max_delay, is_spamming, targets, afk_enabled, afk_text, auto_reply_enabled, auto_reply_text
    
    # Загрузка файлов
    if e.document and e.document.mime_type == 'text/plain' and e.text and e.text.strip().startswith('.шаблоны'):
        await e.edit("📥 Читаю файл...")
        try:
            path = await e.download_media()
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            os.remove(path)
            MESSAGES = [line.strip() for line in content.split('\n') if line.strip()]
            save_templates()
            await e.edit(f"✅ Из файла загружено {len(MESSAGES)} шаблонов!")
        except Exception as ex: await e.edit(f"❌ Ошибка: {ex}")
        return

    if not e.text or not e.text.startswith('.'): return
    text = e.text[1:]
    parts = text.split(' ', 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    # --- СПАМ ---
    if cmd == 'шаблоны':
        if not arg.strip(): return await e.edit("❌ Текст пуст!")
        MESSAGES = [line.strip() for line in arg.split('\n') if line.strip()]
        save_templates()
        await e.edit(f"✅ Сохранено: {len(MESSAGES)}")

    elif cmd == 'спам':
        if not MESSAGES: return await e.edit("❌ Нет шаблонов!")
        targets = [x.strip() for x in arg.split(',')]
        if not is_spamming:
            is_spamming = True
            asyncio.create_task(spam_loop())
            await e.edit(f"🚀 Запуск по {len(targets)} целям")
        else:
            await e.edit("⚠️ Уже работает")

    elif cmd == 'стоп':
        is_spamming = False
        await e.edit("🛑 Остановлено")

    elif cmd == 'задержка':
        try:
            if '-' in arg: min_delay, max_delay = map(int, arg.split('-'))
            else: min_delay = max_delay = int(arg)
            await e.edit(f"⏳ Задержка: {min_delay}-{max_delay} мс")
        except: await e.edit("❌ Ошибка формата")

    # --- ИИ И УТИЛИТЫ ---
    elif cmd == 'gpt':
        if not arg: return await e.edit("❌ Задай вопрос")
        await e.edit("🤖 Думаю...")
        res = await fetch_url(f"https://text.pollinations.ai/{urllib.parse.quote(arg)}")
        if res.startswith("ERROR"):
            await e.edit(f"❌ Ошибка ИИ: {res}")
        else:
            await e.edit(res[:4000])

    elif cmd == 'картинка':
        if not arg: return await e.edit("❌ Опиши картинку")
        await e.edit("🎨 Рисую...")
        try:
            img_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(arg)}?width=1024&height=1024&nologo=true"
            await client.send_file(e.chat_id, img_url, caption=f"Запрос: {arg}")
            await e.delete()
        except Exception as ex: await e.edit(f"❌ Ошибка: {ex}")

    elif cmd == 'погода':
        if not arg: return await e.edit("❌ Укажи город")
        r = await fetch_url(f"https://wttr.in/{urllib.parse.quote(arg)}?format=%C+%t")
        await e.edit(f"🌤 `{r}`", parse_mode='md')

    elif cmd == 'курс':
        await e.edit("💱 Загружаю...")
        r = await fetch_url("https://www.cbr-xml-daily.ru/daily_json.js")
        try:
            data = json.loads(r)['Valute']
            await e.edit(f"💵 USD: `{data['USD']['Value']}`\n💶 EUR: `{data['EUR']['Value']}`", parse_mode='md')
        except: await e.edit("❌ Ошибка загрузки")

    # --- ТРОЛЛИНГ И ОСИНТ ---
    elif cmd == 'вики':
        if not arg: return await e.edit("❌ Укажи имя")
        await e.edit(f"🕵️ Ищу: {arg}...")
        try:
            wiki = wikipediaapi.Wikipedia('TitanBot/1.0', 'ru')
            page = wiki.page(arg)
            if not page.exists(): return await e.edit("❌ Не найдено.")
            msg = f"**{page.title}**\n\n{page.summary[:1000]}\n\n[Читать]({page.fullurl})"
            await e.edit(msg, parse_mode='md', link_preview=False)
        except Exception as ex: await e.edit(f"❌ Ошибка: {ex}")

    elif cmd == 'парсинг':
        if not arg: return await e.edit("❌ Укажи ссылку на группу")
        await e.edit("📦 Собираю участников...")
        try:
            users = []
            async for u in client.iter_participants(arg.strip()):
                if u.username: users.append(f"@{u.username}")
            with open('users.txt', 'w') as f: f.write('\n'.join(users))
            await client.send_file(e.chat_id, 'users.txt', caption=f"✅ Собрано: {len(users)}")
            await e.delete()
        except Exception as ex: await e.edit(f"❌ Ошибка: {ex}")

    elif cmd == 'инфо':
        if not arg: return await e.edit("❌ Укажи юзера")
        try:
            u = await client.get_entity(arg.strip())
            await e.edit(f"**ID:** `{u.id}`\n**Ник:** `@{u.username}`\n**Био:** {u.about if hasattr(u, 'about') else 'Нет'}")
        except: await e.edit("❌ Не найден")

    # --- МЕДИА ---
    elif cmd == 'скачать':
        if not arg: return await e.edit("❌ Укажи ссылку")
        await e.edit("📥 Качаю видео (может занять время)...")
        try:
            ydl_opts = {'format': 'best[ext=mp4]', 'outtmpl': 'v.%(ext)s', 'quiet': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([arg.strip()])
            for f in os.listdir('.'):
                if f.startswith('v.'):
                    await client.send_file(e.chat_id, f)
                    os.remove(f)
                    break
            await e.delete()
        except Exception as ex: await e.edit(f"❌ Ошибка: {ex} (возможно, нужен ffmpeg)")

    elif cmd == 'музыка':
        if not arg: return await e.edit("❌ Укажи ссылку")
        await e.edit("🎵 Качаю MP3...")
        try:
            ydl_opts = {'format': 'bestaudio', 'outtmpl': 'm.%(ext)s', 'quiet': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([arg.strip()])
            for f in os.listdir('.'):
                if f.startswith('m.'):
                    await client.send_file(e.chat_id, f)
                    os.remove(f)
                    break
            await e.delete()
        except: await e.edit("❌ Ошибка скачивания")

    # --- АККАУНТ ---
    elif cmd == 'профиль':
        me = await client.get_me()
        await e.edit(f"**ID:** `{me.id}`\n**Имя:** `{me.first_name}`\n**Ник:** `@{me.username}`")

    elif cmd == 'био':
        try:
            await client(UpdateProfileRequest(about=arg)); await e.edit("✅ Био обновлено")
        except: await e.edit("❌ Ошибка")

    # --- АВТООТВЕТ ---
    elif cmd == 'афк':
        if arg == 'выкл': afk_enabled = False; await e.edit("🔕 АФК выкл")
        else: afk_text = arg or "Отошел"; afk_enabled = True; await e.edit(f"🔔 АФК: {afk_text}")

    elif cmd == 'автоответ':
        if arg == 'выкл': auto_reply_enabled = False; await e.edit("🔕 Автоответ выкл")
        else: auto_reply_text = arg; auto_reply_enabled = True; await e.edit("🔔 Автоответ вкл")

    elif cmd == 'пинг':
        s = datetime.datetime.now()
        await e.edit("📡...")
        await e.edit(f"`{(datetime.datetime.now() - s).microseconds / 1000:.1f} мс`")

    elif cmd == 'хелп':
        await e.edit("**TITAN v21.0**\n.шаблоны, .спам, .стоп, .задержка\n.gpt, .картинка, .вики\n.профиль, .био, .парсинг, .инфо\n.скачать, .музыка\n.погода, .курс, .пинг\n.афк, .автоответ")

# --- АВТООТВЕТЧИК ---
@client.on(events.NewMessage(incoming=True))
async def handler_auto(e):
    if e.is_private:
        if afk_enabled: 
            try: await e.reply(afk_text)
            except: pass
        elif auto_reply_enabled: 
            try: await e.reply(auto_reply_text)
            except: pass

# --- ЗАПУСК ---
async def main():
    print(f"{Fore.CYAN}{'='*45}{Fore.RESET}")
    print(f"{Fore.CYAN}      TITAN v21.0 (MEGA RENDER)          {Fore.RESET}")
    print(f"{Fore.CYAN}{'='*45}{Fore.RESET}")
    
    load_data()
    log("SYS", f"Загружено шаблонов: {len(MESSAGES)}", Fore.GREEN)
    
    # Запуск фейкового веб-сервера в отдельном потоке
    threading.Thread(target=run_web, daemon=True).start()
    log("SYS", "Веб-сервер для Render запущен", Fore.GREEN)
    
    try:
        await client.start()
        log("SYS", "Бот успешно подключен к Telegram", Fore.GREEN)
        await client.run_until_disconnected()
    except Exception as e:
        log("FATAL", f"Критическая ошибка: {e}", Fore.RED)

if __name__ == '__main__':
    asyncio.run(main())
