from telethon import TelegramClient, events, functions, types
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.account import UpdateProfileRequest
import asyncio, random, re, os, datetime, string, requests, yt_dlp, qrcode, pytz, json, wikipediaapi, urllib.parse
from PIL import Image
from colorama import init, Fore

init(autoreset=True)

API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
client = TelegramClient('my_account', API_ID, API_HASH)

TEMPLATES_FILE = 'templates.json'
MESSAGES = []
# СКОРОСТЬ: 200-500 мс (быстро, но не бан)
min_delay = 200
max_delay = 500
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
    counter = 0
    while is_spamming:
        try:
            if not targets: 
                is_spamming = False
                log("SPAM", "Очередь пуста", Fore.YELLOW)
                break
            
            t = targets[0]
            text = random.choice(MESSAGES)
            
            # Без "typing", просто отправка
            if random.random() > 0.8: text += "\u200b"
            
            await client.send_message(t, text)
            counter += 1
            log("SPAM", f"[{counter}] -> {t}", Fore.BLUE)
            
            wait_time = random.randint(min_delay, max_delay) / 1000.0
            await asyncio.sleep(wait_time)
            
        except Exception as e:
            if "FloodWait" in str(e):
                w = int(re.search(r'(\d+) seconds', str(e)).group(1))
                log("WARN", f"FloodWait {w}с", Fore.YELLOW)
                await asyncio.sleep(w + 5)
            else:
                targets.pop(0)

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
            log("CMD", f"Файл загружен: {len(MESSAGES)} строк", Fore.GREEN)
        except Exception as ex:
            await e.edit(f"Ошибка: {ex}")
        return

    if not e.text or not e.text.startswith('.'): return
    text = e.text[1:]
    parts = text.split(' ', 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == 'gpt':
        if not arg.strip(): return await e.edit("Задай вопрос: `.gpt Сколько будет 2+2?`")
        await e.edit("Думаю...")
        try:
            url = f"https://text.pollinations.ai/{urllib.parse.quote(arg)}"
            res = requests.get(url, timeout=30)
            if res.status_code == 200:
                await e.edit(res.text[:4000])
                log("CMD", f"GPT: {arg[:20]}...", Fore.GREEN)
            else:
                await e.edit("Ошибка нейросети (перегружена).")
        except: await e.edit("Нет интернета или таймаут.")

    elif cmd == 'картинка':
        if not arg.strip(): return await e.edit("Опиши картинку: `.картинка кот в космосе`")
        await e.edit("Рисую...")
        try:
            img_url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(arg)}?width=1024&height=1024&nologo=true"
            await client.send_file(e.chat_id, img_url, caption=f"Запрос: {arg}")
            await e.delete()
            log("CMD", f"Картинка: {arg[:20]}...", Fore.GREEN)
        except Exception as ex: await e.edit(f"Ошибка: {ex}")

    elif cmd == 'шаблоны':
        if not arg.strip(): return await e.edit("Текст пуст!")
        MESSAGES = [line.strip() for line in arg.split('\n') if line.strip()]
        save_templates()
        await e.edit(f"✅ Сохранено: {len(MESSAGES)}")
        log("CMD", f"Шаблоны: {len(MESSAGES)}", Fore.GREEN)

    elif cmd == 'спам':
        if not MESSAGES: return await e.edit("Нет шаблонов!")
        targets = [x.strip() for x in arg.split(',')]
        if not is_spamming:
            is_spamming = True
            asyncio.create_task(spam_loop())
            await e.edit(f"Запуск по {len(targets)}")
            log("SPAM", f"Запуск по {len(targets)}", Fore.YELLOW)

    elif cmd == 'стоп':
        is_spamming = False
        await e.edit("Остановлено")
        log("SPAM", "Остановлен", Fore.RED)

    elif cmd == 'задержка':
        try:
            if '-' in arg: min_delay, max_delay = map(int, arg.split('-'))
            else: min_delay = max_delay = int(arg)
            await e.edit(f"Задержка: {min_delay}-{max_delay} мс")
        except: pass

    elif cmd == 'вики':
        name = arg.strip()
        if not name: return await e.edit("Укажи имя: `.вики Олег Кузнецов`")
        await e.edit(f"Ищу в Википедии: {name}...")
        try:
            wiki = wikipediaapi.Wikipedia('TitanBot/1.0', 'ru')
            page = wiki.page(name)
            if not page.exists(): return await e.edit("Статья не найдена.")
            text = page.summary[:1000]
            url = page.fullurl
            img_url = None
            if page.images:
                for img in page.images:
                    low = img.lower()
                    if 'logo' not in low and 'icon' not in low and 'commons' not in low and 'star' not in low:
                        img_url = img; break
            msg = f"**{page.title}**\n\n{text}\n\n[Читать полностью]({url})"
            if img_url:
                try:
                    await client.send_file(e.chat_id, img_url, caption=msg, parse_mode='md')
                    await e.delete()
                except: await e.edit(msg, parse_mode='md', link_preview=False)
            else: await e.edit(msg, parse_mode='md', link_preview=False)
            log("CMD", f"Вики: {name}", Fore.GREEN)
        except Exception as ex: await e.edit(f"Ошибка: {ex}")

    elif cmd == 'профиль':
        me = await client.get_me()
        await e.edit(f"**ID:** `{me.id}`\n**Имя:** `{me.first_name}`\n**Ник:** `@{me.username}`")

    elif cmd == 'био':
        try:
            await client(UpdateProfileRequest(about=arg)); await e.edit("Био обновлено")
        except: await e.edit("Ошибка")

    elif cmd == 'имя':
        p = arg.split(' ')
        try:
            await client(UpdateProfileRequest(first_name=p[0], last_name=p[1] if len(p)>1 else "")); await e.edit("Имя обновлено")
        except: await e.edit("Ошибка")

    elif cmd == 'афк':
        if arg == 'выкл': afk_enabled = False; await e.edit("АФК выкл")
        else: afk_text = arg or "Отошел"; afk_enabled = True; await e.edit(f"АФК: {afk_text}")

    elif cmd == 'автоответ':
        if arg == 'выкл': auto_reply_enabled = False; await e.edit("Выкл")
        else: auto_reply_text = arg; auto_reply_enabled = True; await e.edit("Вкл")

    elif cmd == 'парсинг':
        await e.edit("Сбор...")
        try:
            users = []
            async for u in client.iter_participants(arg.strip()):
                if u.username: users.append(f"@{u.username}")
            with open('users.txt', 'w') as f: f.write('\n'.join(users))
            await client.send_file(e.chat_id, 'users.txt', caption=f"Собрано: {len(users)}")
            await e.delete()
        except Exception as ex: await e.edit(f"Ошибка: {ex}")

    elif cmd == 'инфо':
        try:
            u = await client.get_entity(arg.strip())
            await e.edit(f"**ID:** `{u.id}`\n**Ник:** `@{u.username}`\n**Био:** {u.about if hasattr(u, 'about') else 'Нет'}")
        except: await e.edit("Не найден")

    elif cmd == 'id':
        if not arg: await e.edit(f"Твой ID: `{e.sender_id}`")
        else:
            try:
                u = await client.get_entity(arg.strip()); await e.edit(f"ID `{arg}`: `{u.id}`")
            except: await e.edit("Не найден")

    elif cmd == 'группы':
        res = "**Группы:**\n"
        async for d in client.iter_dialogs():
            if d.is_group: res += f"- {d.name} (`{d.id}`)\n"
        await e.edit(res)

    elif cmd == 'вступить':
        try:
            await client(JoinChannelRequest(arg.strip())); await e.edit("Вступил")
        except: await e.edit("Ошибка")

    elif cmd == 'выйти':
        try:
            await client(LeaveChannelRequest(e.chat_id)); await e.edit("Вышел")
        except: await e.edit("Ошибка")

    elif cmd == 'очистить':
        await e.edit("Удаляю...")
        async for m in client.iter_messages(e.chat_id, limit=200):
            try: await m.delete()
            except: pass

    elif cmd == 'скачать':
        await e.edit("Качаю...")
        try:
            with yt_dlp.YoutubeDL({'format': 'best', 'outtmpl': 'v.%(ext)s', 'quiet': True}) as ydl: ydl.download([arg.strip()])
            for f in os.listdir('.'):
                if f.startswith('v.'):
                    await client.send_file(e.chat_id, f); os.remove(f); break
            await e.delete()
        except: await e.edit("Ошибка")

    elif cmd == 'музыка':
        await e.edit("Качаю MP3...")
        try:
            with yt_dlp.YoutubeDL({'format': 'bestaudio/best', 'outtmpl': 'm.%(ext)s', 'quiet': True}) as ydl: ydl.download([arg.strip()])
            for f in os.listdir('.'):
                if f.startswith('m.'):
                    await client.send_file(e.chat_id, f); os.remove(f); break
            await e.delete()
        except: await e.edit("Ошибка")

    elif cmd == 'стикер':
        if not e.reply_to_msg_id: return await e.edit("Ответь на фото")
        msg = await e.get_reply_message()
        if not (msg.photo or msg.document): return await e.edit("Нет фото")
        await e.edit("Делаю...")
        try:
            path = await msg.download_media()
            img = Image.open(path).convert("RGBA")
            img.thumbnail((512, 512))
            img.save("s.webp", "WEBP")
            await client.send_file(e.chat_id, "s.webp")
            os.remove(path); os.remove("s.webp"); await e.delete()
        except: await e.edit("Ошибка")

    elif cmd == 'погода':
        try:
            r = requests.get(f"https://wttr.in/{arg.strip()}?format=%C+%t")
            await e.edit(f"`{r.text}`")
        except: await e.edit("Ошибка сети")

    elif cmd == 'курс':
        try:
            r = requests.get("https://www.cbr-xml-daily.ru/daily_json.js").json()['Valute']
            await e.edit(f"**USD:** `{r['USD']['Value']}`\n**EUR:** `{r['EUR']['Value']}`")
        except: await e.edit("Нет интернета")

    elif cmd == 'qr':
        qr = qrcode.QRCode(box_size=1, border=0)
        qr.add_data(arg.strip()); qr.make()
        txt = "\n".join(["".join(["██" if c else "  " for c in row]) for row in qr.get_matrix()])
        await e.edit(f"```\n{txt}\n```")

    elif cmd == 'пароль':
        l = int(arg.strip() or 16)
        p = ''.join(random.choice(string.ascii_letters + string.digits + "!@#$%^&*") for _ in range(l))
        await e.edit(f"`{p}`")

    elif cmd == 'время':
        try:
            tz = pytz.timezone(arg.strip())
            await e.edit(f"`{datetime.datetime.now(tz).strftime('%H:%M:%S')}`")
        except: await e.edit("Ошибка (Europe/Moscow)")

    elif cmd == 'рандом':
        try:
            a, b = map(int, arg.split(' '))
            await e.edit(f"`{random.randint(a, b)}`")
        except: await e.edit("Формат: .рандом 1 100")

    elif cmd == 'монетка':
        await e.edit(f"`{random.choice(['Орел', 'Решка'])}`")

    elif cmd == 'пинг':
        s = datetime.datetime.now()
        await e.edit("...")
        await e.edit(f"`{(datetime.datetime.now() - s).microseconds / 1000:.1f} мс`")

    elif cmd == 'хелп':
        txt = """
**TITAN v18.0 (FAST)**
**Нейросети:** .gpt, .картинка
**Спам:** .шаблоны, .спам, .стоп, .задержка
**Троллинг:** .вики
**Профиль:** .профиль, .био, .имя, .id
**Чаты:** .парсинг, .инфо, .группы, .вступить, .выйти, .очистить
**Медиа:** .скачать, .музыка, .стикер
**Утилиты:** .погода, .курс, .qr, .пароль, .время, .рандом, .монетка, .пинг
**Режимы:** .афк, .автоответ
"""
        await e.edit(txt)

@client.on(events.NewMessage(incoming=True))
async def handler_auto(e):
    if e.is_private:
        if afk_enabled: await e.reply(afk_text)
        elif auto_reply_enabled: await e.reply(auto_reply_text)

async def main():
    print(f"{Fore.CYAN}{'='*45}{Fore.RESET}")
    print(f"{Fore.CYAN}      TITAN v18.0 (FAST EDITION)         {Fore.RESET}")
    print(f"{Fore.CYAN}{'='*45}{Fore.RESET}")
    log("SYS", f"Загружено шаблонов: {len(MESSAGES)}", Fore.GREEN)
    await client.start()
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
