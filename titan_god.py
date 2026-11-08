from telethon import TelegramClient, events, functions, types
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.errors import FloodWaitError
import asyncio, random, re, os, datetime, string, json, urllib.parse, wikipediaapi, qrcode, pytz, glob
from PIL import Image
from colorama import init, Fore
from flask import Flask
import threading
import aiohttp
import yt_dlp
import imageio_ffmpeg

init(autoreset=True)

# --- НАСТРОЙКА FFMPEG (ДЛЯ ВИДЕО) ---
try:
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except:
    FFMPEG_PATH = None

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

MESSAGES = []
min_delay = 100
max_delay = 300
is_spamming = False
targets = []
afk_enabled = False
afk_text = "Отошел."
auto_reply_enabled = False
auto_reply_text = "Занят."

client = TelegramClient('my_account', API_ID, API_HASH)

def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

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

async def fetch_url(url, headers=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as response:
                return await response.text()
    except Exception as e:
        return f"ERROR: {e}"

# --- ФУНКЦИЯ ДЛЯ ОДНОГО ЧАТА (БЕСКОНЕЧНЫЙ ЦИКЛ) ---
async def spam_single_target(target):
    global is_spamming
    counter = 0
    while is_spamming:
        try:
            if not MESSAGES:
                log("SPAM", "Нет шаблонов!", Fore.RED)
                break
            
            text = random.choice(MESSAGES)
            if random.random() > 0.7: text += "\u200b"
                
            await client.send_message(target, text)
            counter += 1
            log("SPAM", f"[{counter}] -> {target}", Fore.BLUE)
            
            wait_time = random.randint(min_delay, max_delay) / 1000.0
            await asyncio.sleep(wait_time)
            
        except FloodWaitError as e:
            log("FLOOD", f"Бан на {e.seconds} сек для {target}. Ждем...", Fore.RED)
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            log("ERROR", f"Сбой на {target}: {e}", Fore.RED)
            break

# --- ЗАПУСК ПАРАЛЛЕЛЬНЫХ ПОТОКОВ ---
async def spam_loop():
    global is_spamming, targets
    if not targets:
        is_spamming = False
        return
        
    log("SPAM", f"Запуск параллельного спама по {len(targets)} чатам", Fore.YELLOW)
    
    tasks = [asyncio.create_task(spam_single_target(t)) for t in targets]
    await asyncio.gather(*tasks)
    
    is_spamming = False
    log("SPAM", "Все потоки спама завершены.", Fore.YELLOW)

# --- ОБРАБОТЧИК КОМАНД ---
@client.on(events.NewMessage(outgoing=True))
async def handler(e):
    global MESSAGES, min_delay, max_delay, is_spamming, targets, afk_enabled, afk_text, auto_reply_enabled, auto_reply_text
    
    if e.document and e.document.mime_type == 'text/plain' and e.text and e.text.strip().startswith('.шаблоны'):
        await e.edit("📥 Читаю файл...")
        try:
            path = await e.download_media()
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            os.remove(path)
            MESSAGES = [line.strip() for line in content.split('\n') if line.strip()]
            save_templates()
            await e.edit(f"✅ Загружено: {len(MESSAGES)}")
        except Exception as ex: await e.edit(f"❌ {ex}")
        return

    if not e.text or not e.text.startswith('.'): return
    text = e.text[1:]
    parts = text.split(' ', 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == 'шаблоны':
        if not arg.strip(): return await e.edit("❌ Пусто!")
        MESSAGES = [line.strip() for line in arg.split('\n') if line.strip()]
        save_templates()
        await e.edit(f"✅ Сохранено: {len(MESSAGES)}")

    # --- БЛОК СПАМА С КОНВЕРТАЦИЕЙ ID ---
    elif cmd == 'спам':
        if not MESSAGES: return await e.edit("❌ Нет шаблонов!")
        
        raw_targets = [x.strip() for x in arg.split(',') if x.strip()]
        targets = []
        
        await e.edit("🔄 Синхронизирую чаты...")
        try:
            async for dialog in client.iter_dialogs():
                pass
        except: pass

        for t in raw_targets:
            try:
                if t.startswith('tg://'):
                    match = re.search(r'id=(\d+)', t)
                    if match:
                        raw_id = int(match.group(1))
                        if raw_id > 0 and len(str(raw_id)) < 12:
                            chat_id = int(f"-100{raw_id}")
                        else:
                            chat_id = raw_id
                        entity = await client.get_entity(chat_id)
                        targets.append(entity)
                
                elif 't.me/c/' in t:
                    raw_id = int(t.split('t.me/c/')[1].split('/')[0])
                    chat_id = int(f"-100{raw_id}")
                    entity = await client.get_entity(chat_id)
                    targets.append(entity)
                
                elif t.startswith('-') and t[1:].isdigit():
                    targets.append(int(t))
                
                else:
                    targets.append(t)
            except Exception as ex:
                log("ERROR", f"Не нашел чат {t}: {ex}", Fore.RED)
                targets.append(t)

        if not targets:
            return await e.edit("❌ Не удалось распознать ни одну цель.")

        if not is_spamming:
            is_spamming = True
            asyncio.create_task(spam_loop())
            await e.edit(f"🚀 Ебашу ОДНОВРЕМЕННО по {len(targets)} чатам")
        else:
            await e.edit("⚠️ Уже работает")

    elif cmd == 'стоп':
        is_spamming = False
        await e.edit("🛑 СТОП (останавливаю все потоки)")

    elif cmd == 'задержка':
        try:
            if '-' in arg: min_delay, max_delay = map(int, arg.split('-'))
            else: min_delay = max_delay = int(arg)
            await e.edit(f"⏳ Задержка: {min_delay}-{max_delay} мс")
        except: pass

    elif cmd == 'gpt':
        if not arg: return
        await e.edit("🤖 Думаю...")
        res = await fetch_url(f"https://text.pollinations.ai/{urllib.parse.quote(arg)}")
        await e.edit(res[:4000] if not res.startswith("ERROR") else res)

    elif cmd == 'картинка':
        if not arg: return
        await e.edit("🎨 Рисую...")
        try:
            img_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(arg)}?width=1024&height=1024&nologo=true"
            await client.send_file(e.chat_id, img_url, caption=f"Запрос: {arg}")
            await e.delete()
        except Exception as ex: await e.edit(f"❌ {ex}")

    elif cmd == 'погода':
        if not arg: return
        r = await fetch_url(f"https://wttr.in/{urllib.parse.quote(arg)}?format=%C+%t")
        await e.edit(f"🌤 `{r}`", parse_mode='md')

    elif cmd == 'курс':
        r = await fetch_url("https://www.cbr-xml-daily.ru/daily_json.js")
        try:
            data = json.loads(r)['Valute']
            await e.edit(f"💵 USD: `{data['USD']['Value']}`\n💶 EUR: `{data['EUR']['Value']}`", parse_mode='md')
        except: await e.edit("❌ Ошибка")

    elif cmd == 'вики':
        if not arg: return
        await e.edit(f"🕵️ {arg}...")
        try:
            wiki = wikipediaapi.Wikipedia('TitanBot/1.0', 'ru')
            page = wiki.page(arg)
            if not page.exists(): return await e.edit("❌ Нет статьи")
            await e.edit(f"**{page.title}**\n\n{page.summary[:1000]}\n\n[Читать]({page.fullurl})", parse_mode='md', link_preview=False)
        except: pass

    elif cmd == 'парсинг':
        if not arg: return
        await e.edit("📦 Сбор...")
        try:
            users = []
            async for u in client.iter_participants(arg.strip()):
                if u.username: users.append(f"@{u.username}")
            with open('users.txt', 'w') as f: f.write('\n'.join(users))
            await client.send_file(e.chat_id, 'users.txt', caption=f"✅ {len(users)}")
            await e.delete()
        except Exception as ex: await e.edit(f"❌ {ex}")

    elif cmd == 'инфо':
        try:
            u = await client.get_entity(arg.strip())
            await e.edit(f"**ID:** `{u.id}`\n**Ник:** `@{u.username}`")
        except: await e.edit("❌ Не найден")

    elif cmd == 'скачать':
        if not arg: return
        await e.edit("📥 Качаю...")
        for f in glob.glob("video.*"): os.remove(f)
        try:
            ydl_opts = {'format': 'best[ext=mp4]/best', 'outtmpl': 'video.%(ext)s', 'quiet': True, 'ffmpeg_location': FFMPEG_PATH}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([arg.strip()])
            for f in glob.glob("video.*"):
                await client.send_file(e.chat_id, f)
                os.remove(f)
                break
            await e.delete()
        except Exception as ex: await e.edit(f"❌ {ex}")

    elif cmd == 'профиль':
        me = await client.get_me()
        await e.edit(f"**ID:** `{me.id}`\n**Имя:** `{me.first_name}`")

    elif cmd == 'афк':
        if arg == 'выкл': afk_enabled = False; await e.edit("🔕")
        else: afk_text = arg or "Отошел"; afk_enabled = True; await e.edit("🔔")

    elif cmd == 'автоответ':
        if arg == 'выкл': auto_reply_enabled = False; await e.edit("🔕")
        else: auto_reply_text = arg; auto_reply_enabled = True; await e.edit("🔔")

    elif cmd == 'пинг':
        s = datetime.datetime.now()
        await e.edit("📡")
        await e.edit(f"`{(datetime.datetime.now() - s).microseconds / 1000:.1f} мс`")

    elif cmd == 'хелп':
        await e.edit("**Команды:**\n.шаблоны, .спам, .стоп, .задержка\n.gpt, .картинка, .вики\n.скачать, .парсинг, .инфо\n.профиль, .погода, .курс, .пинг\n.афк, .автоответ\n\n**ID/Ссылки:** можно юзать tg:// и t.me/c/")

@client.on(events.NewMessage(incoming=True))
async def handler_auto(e):
    if e.is_private:
        if afk_enabled: 
            try: await e.reply(afk_text)
            except: pass
        elif auto_reply_enabled: 
            try: await e.reply(auto_reply_text)
            except: pass

async def main():
    print(f"{Fore.CYAN}TITAN v29.0 (FULL FINAL) ЗАПУЩЕН{Fore.RESET}")
    load_data()
    log("SYS", f"Шаблонов: {len(MESSAGES)}", Fore.GREEN)
    threading.Thread(target=run_web, daemon=True).start()
    await client.start()
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
