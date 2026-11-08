from telethon import TelegramClient, events, functions, types
from telethon.tl.functions.channels import JoinChannelRequest
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

API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
TEMPLATES_FILE = 'templates.json'

MESSAGES = []
is_spamming = False
targets = []
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
    
    if e.document and e.document.mime_type == 'text/plain' and e.text and e.text.strip().startswith('.шаблоны'):
        await e.edit("📥 Читаю...")
        try:
            path = await e.download_media()
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            os.remove(path)
            MESSAGES = [line.strip() for line in content.split('\n') if line.strip()]
            save_templates()
            await e.edit(f"✅ {len(MESSAGES)}")
        except Exception as ex: await e.edit(f"❌ {ex}")
        return

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
        
        raw_targets = [x.strip() for x in arg.split(',')]
        targets = []
        
        for t in raw_targets:
            try:
                if 't.me/c/' in t:
                    chat_id = int(t.split('t.me/c/')[1].split('/')[0])
                    targets.append(int(f"-100{chat_id}"))
                elif t.startswith('tg://'):
                    match = re.search(r'id=(\d+)', t)
                    if match:
                        raw_id = int(match.group(1))
                        if raw_id > 0 and len(str(raw_id)) < 12:
                            targets.append(int(f"-100{raw_id}"))
                        else:
                            targets.append(raw_id)
                    else:
                        targets.append(t)
                elif t.startswith('-') and t[1:].isdigit():
                    targets.append(int(t))
                else:
                    targets.append(t)
            except:
                targets.append(t)

        if not is_spamming:
            is_spamming = True
            asyncio.create_task(spam_loop())
            await e.edit(f"🚀 Ебашу по {len(targets)}")
        else:
            await e.edit("⚠️ Уже работает")

    elif cmd == 'стоп':
        is_spamming = False
        await e.edit("🛑 СТОП")

    elif cmd == 'задержка':
        try:
            min_delay, max_delay = map(int, arg.split('-'))
            await e.edit(f"⏳ {min_delay}-{max_delay} мс")
        except: pass

    elif cmd == 'gpt':
        await e.edit("🤖...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://text.pollinations.ai/{urllib.parse.quote(arg)}") as res:
                    await e.edit((await res.text())[:4000])
        except: await e.edit("❌ Ошибка")

    elif cmd == 'картинка':
        await e.edit("🎨...")
        try:
            img_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(arg)}?width=1024&height=1024&nologo=true"
            await client.send_file(e.chat_id, img_url, caption=f"Запрос: {arg}")
            await e.delete()
        except Exception as ex: await e.edit(f"❌ {ex}")

    elif cmd == 'вики':
        await e.edit("🕵️...")
        try:
            wiki = wikipediaapi.Wikipedia('TitanBot/1.0', 'ru')
            page = wiki.page(arg)
            if page.exists():
                await e.edit(f"**{page.title}**\n\n{page.summary[:1000]}\n\n[Читать]({page.fullurl})", parse_mode='md', link_preview=False)
            else: await e.edit("❌ Нет статьи")
        except: pass

    elif cmd == 'скачать':
        await e.edit("📥...")
        for f in glob.glob("video.*"): os.remove(f)
        try:
            ydl_opts = {'format': 'best[ext=mp4]/best', 'outtmpl': 'video.%(ext)s', 'quiet': True, 'ffmpeg_location': FFMPEG_PATH}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([arg])
            for f in glob.glob("video.*"):
                await client.send_file(e.chat_id, f)
                os.remove(f)
                break
            await e.delete()
        except Exception as ex: await e.edit(f"❌ {ex}")

    elif cmd == 'парсинг':
        await e.edit("📦...")
        try:
            users = []
            async for u in client.iter_participants(arg):
                if u.username: users.append(f"@{u.username}")
            with open('users.txt', 'w') as f: f.write('\n'.join(users))
            await client.send_file(e.chat_id, 'users.txt', caption=f"✅ {len(users)}")
            await e.delete()
        except Exception as ex: await e.edit(f"❌ {ex}")

    elif cmd == 'пинг':
        s = datetime.datetime.now()
        await e.edit("📡")
        await e.edit(f"`{(datetime.datetime.now() - s).microseconds / 1000:.1f} мс`")

    elif cmd == 'хелп':
        await e.edit("**.шаблоны** (текст/файл)\n**.спам** (ссылки/id/ники)\n**.стоп**\n**.задержка** (10-50)\n**.gpt**\n**.картинка**\n**.вики**\n**.скачать** (YouTube)\n**.парсинг**")

@client.on(events.NewMessage(incoming=True))
async def handler_auto(e):
    pass # Автоответ отключен для скорости

async def main():
    print(f"{Fore.CYAN}TITAN v36.0 (TURBO) ЗАПУЩЕН{Fore.RESET}")
    load_data()
    print(f"{Fore.GREEN}Шаблонов: {len(MESSAGES)}{Fore.RESET}")
    threading.Thread(target=run_web, daemon=True).start()
    await client.start()
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
