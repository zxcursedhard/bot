from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
import asyncio, random, json, os, datetime, re
from colorama import init, Fore
from flask import Flask
import threading
import aiohttp

init(autoreset=True)

app = Flask(__name__)

@app.route('/')
def home():
    return "TITAN IS ALIVE"

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
TEMPLATES_FILE = 'templates.json'

MESSAGES = []
is_spamming = False
targets = []

# ТУРБО-ЗАДЕРЖКА
min_delay = 10
max_delay = 50

client = TelegramClient('my_account', API_ID, API_HASH)

def load_data():
    global MESSAGES
    if os.path.exists(TEMPLATES_FILE):
        try:
            with open(TEMPLATES_FILE, 'r', encoding='utf-8') as f:
                MESSAGES = json.load(f)
        except: MESSAGES = []

def save_templates():
    try:
        with open(TEMPLATES_FILE, 'w', encoding='utf-8') as f:
            json.dump(MESSAGES, f, ensure_ascii=False, indent=2)
    except: pass

async def spam_loop():
    global is_spamming, targets
    while is_spamming:
        for target in targets:
            if not is_spamming: break
            try:
                text = random.choice(MESSAGES)
                if random.random() > 0.8: text += "\u200b"
                await client.send_message(target, text)
                print(f"{Fore.BLUE}[SPAM]{Fore.RESET} -> {target}")
                await asyncio.sleep(random.randint(min_delay, max_delay) / 1000.0)
            except FloodWaitError as e:
                print(f"{Fore.RED}[FLOOD] {e.seconds}с{Fore.RESET}")
                await asyncio.sleep(e.seconds + 2)
            except Exception as e:
                print(f"{Fore.RED}[ERR] {e}{Fore.RESET}")

@client.on(events.NewMessage(outgoing=True))
async def handler(e):
    global MESSAGES, is_spamming, targets, min_delay, max_delay
    
    if not e.text or not e.text.startswith('.'): return
    cmd, *args = e.text[1:].split(' ', 1)
    cmd = cmd.lower()
    arg = args[0] if args else ""

    if cmd == 'шаблоны':
        MESSAGES = [line.strip() for line in arg.split('\n') if line.strip()]
        save_templates()
        await e.edit(f"✅ {len(MESSAGES)}")

    elif cmd == 'спам':
        if not MESSAGES: return await e.edit("❌ Нет шаблонов!")
        targets = [x.strip() for x in arg.split(',')]
        if not is_spamming:
            is_spamming = True
            asyncio.create_task(spam_loop())
            await e.edit(f"🚀 Ебашу по {len(targets)}")

    elif cmd == 'стоп':
        is_spamming = False
        await e.edit("🛑 СТОП")

    elif cmd == 'задержка':
        try:
            min_delay, max_delay = map(int, arg.split('-'))
            await e.edit(f"⏳ {min_delay}-{max_delay} мс")
        except: pass

    elif cmd == 'хелп':
        await e.edit(".шаблоны, .спам, .стоп, .задержка")

async def main():
    load_data()
    print(f"{Fore.CYAN}TITAN v35.0 ULTRA-TURBO{Fore.RESET}")
    threading.Thread(target=run_web, daemon=True).start()
    await client.start()
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
