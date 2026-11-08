from telethon import TelegramClient, events, functions, types
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.account import UpdateProfileRequest
import asyncio, random, re, os, datetime, string, requests, yt_dlp, qrcode, pytz, json, wikipediaapi, urllib.parse
from PIL import Image
from colorama import init, Fore
from flask import Flask
import threading

init(autoreset=True)

# --- ФЕЙКОВЫЙ СЕРВЕР ДЛЯ RENDER (чтобы не засыпал) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_web():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# --- ОСНОВНОЙ КОД БОТА ---
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
client = TelegramClient('my_account', API_ID, API_HASH)

TEMPLATES_FILE = 'templates.json'
MESSAGES = []
min_delay = 1000
max_delay = 3000
is_spamming = False
targets = []
afk_enabled = False
afk_text = "Отошел"
auto_reply_enabled = False
auto_reply_text = "Занят"

def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

def load_data():
    global MESSAGES
    if os.path.exists(TEMPLATES_FILE):
        with open(TEMPLATES_FILE, 'r', encoding='utf-8') as f:
            MESSAGES = json.load(f)

def save_templates():
    with open(TEMPLATES_FILE, 'w', encoding='utf-8') as f:
        json.dump(MESSAGES, f, ensure_ascii=False, indent=2)

load_data()

async def spam_loop():
    global is_spamming, targets
    while is_spamming:
        try:
            if not targets: 
                is_spamming = False
                log("SPAM", "Очередь пуста", Fore.YELLOW)
                break
            t = targets[0]
            text = random.choice(MESSAGES)
            try:
                async with client.action(t, 'typing'):
                    await asyncio.sleep(random.uniform(1.5, 3.5))
            except: pass
            if random.random() > 0.8: text += "\u200b"
            await client.send_message(t, text)
            log("SPAM", f"-> {t}", Fore.BLUE)
            await asyncio.sleep(random.randint(min_delay, max_delay) / 1000)
        except Exception as e:
            if "FloodWait" in str(e):
                w = int(re.search(r'(\d+) seconds', str(e)).group(1))
                log("WARN", f"FloodWait {w}с", Fore.YELLOW)
                await asyncio.sleep(w + 5)
            else: targets.pop(0)

@client.on(events.NewMessage(outgoing=True))
async def handler(e):
    global MESSAGES, min_delay, max_delay, is_spamming, targets, afk_enabled, afk_text, auto_reply_enabled, auto_reply_text
    
    if e.document and e.document.mime_type == 'text/plain' and e.text and e.text.strip().startswith('.шаблоны'):
        await e.edit("Читаю файл...")
        try:
            path = await e.download_media()
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            os.remove(path)
            MESSAGES = [line.strip() for line in content.split('\n') if line.strip()]
            save_templates()
            await e.edit(f"✅ Из файла загружено {len(MESSAGES)} шаблонов!")
        except Exception as ex: await e.edit(f"Ошибка: {ex}")
        return

    if not e.text or not e.text.startswith('.'): return
    text = e.text[1:]
    parts = text.split(' ', 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == 'шаблоны':
        if not arg.strip(): return await e.edit("Текст пуст!")
        MESSAGES = [line.strip() for line in arg.split('\n') if line.strip()]
        save_templates()
        await e.edit(f"✅ Сохранено: {len(MESSAGES)}")

    elif cmd == 'спам':
        if not MESSAGES: return await e.edit("Нет шаблонов!")
        targets = [x.strip() for x in arg.split(',')]
        if not is_spamming:
            is_spamming = True
            asyncio.create_task(spam_loop())
            await e.edit(f"Запуск по {len(targets)}")

    elif cmd == 'стоп':
        is_spamming = False
        await e.edit("Остановлено")

    elif cmd == 'задержка':
        try:
            if '-' in arg: min_delay, max_delay = map(int, arg.split('-'))
            else: min_delay = max_delay = int(arg)
            await e.edit(f"Задержка: {min_delay}-{max_delay} мс")
        except: pass

    elif cmd == 'хелп':
        await e.edit("**Команды:** .шаблоны, .спам, .стоп, .задержка, .профиль")

# ЗАПУСК
async def main():
    print(f"{Fore.CYAN}{'='*45}{Fore.RESET}")
    print(f"{Fore.CYAN}      TITAN v19.0 (RENDER EDITION)       {Fore.RESET}")
    print(f"{Fore.CYAN}{'='*45}{Fore.RESET}")
    log("SYS", f"Загружено шаблонов: {len(MESSAGES)}", Fore.GREEN)
    
    # Запускаем фейковый веб-сервер в отдельном потоке
    threading.Thread(target=run_web, daemon=True).start()
    
    await client.start()
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
