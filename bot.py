import os
import random
import logging
import io
import base64
import tempfile
from collections import defaultdict, deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from openai import OpenAI
import requests

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")  # Опционально для поиска

MASTER_USERNAME = "muctep_kpunep"
AUDIO_MODEL = "whisper-large-v3-turbo"

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
) if GROQ_API_KEY else None

# --- КОНТЕКСТ ЧАТОВ (ПОСЛЕДНИЕ СООБЩЕНИЯ) ---
chat_histories = defaultdict(lambda: deque(maxlen=15))


# --- ПОИСК В ИНТЕРНЕТЕ ---
def search_web(query: str) -> str:
    """Универсальный инструмент поиска информации для бота"""
    logger.info(f"🔍 Ищем в сети: {query}")
    try:
        if TAVILY_API_KEY:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": query, "max_results": 3},
                timeout=5
            )
            data = resp.json()
            results = [res.get("content", "") for res in data.get("results", [])]
            if results:
                return "\n".join(results)

        url = f"https://api.duckduckgo.com/?q={requests.utils.quote(query)}&format=json"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        abstract = data.get("AbstractText")
        if abstract:
            return abstract
        
        related = data.get("RelatedTopics", [])
        for topic in related:
            if "Text" in topic:
                return topic["Text"]
                
        return "ничего конкретного в сети не нашлось"
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        return "интернет-поиск временно отрыгнул"


# --- АВТОМАТИЧЕСКИЙ ПОДБОР РАБОЧИХ МОДЕЛЕЙ ---
def get_active_models() -> tuple[str, str]:
    fallback_text = "llama-3.3-70b-versatile"
    fallback_vision = "llama-3.2-11b-vision-preview"

    if not groq_client:
        return fallback_text, fallback_vision

    try:
        models_data = groq_client.models.list().data
        available_ids = [m.id for m in models_data]
        logger.info(f"Доступные модели на Groq: {available_ids}")

        banned = ["whisper", "vision", "embed", "guard", "audio"]
        selected_text = None
        
        for m in available_ids:
            if any(b in m.lower() for b in banned):
                continue
            if "llama" in m.lower() or "qwen" in m.lower() or "mixtral" in m.lower():
                selected_text = m
                break
        
        if not selected_text and available_ids:
            selected_text = available_ids[0]

        selected_vision = None
        for m in available_ids:
            if "vision" in m.lower():
                selected_vision = m
                break
        
        if not selected_vision:
            selected_vision = fallback_vision

        return selected_text or fallback_text, selected_vision
    except Exception as e:
        logger.error(f"Ошибка при получении списка моделей: {e}")
        return fallback_text, fallback_vision


TEXT_MODEL, VISION_MODEL = get_active_models()

VALERA_IMPERSONATIONS = [
    "я валера",
    "я валера и я одобряю этот бред",
    "всем привет, я валера",
    "как валера заявляю: это слишком сложно",
    "ребята, я валера, кто меня звал?"
]


# --- ВЕБ-СЕРВЕР ДЛЯ ХЕЛСЧЕКОВ ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Valera Bot Ultimate is live!")

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


async def transcribe_voice(voice_file_bytes: bytes) -> str:
    if not groq_client:
        return ""
    try:
        audio_file = ("voice.ogg", io.BytesIO(voice_file_bytes), "audio/ogg")
        transcription = groq_client.audio.transcriptions.create(
            file=audio_file,
            model=AUDIO_MODEL,
            response_format="text"
        )
        return str(transcription).strip()
    except Exception as e:
        logger.error(f"Ошибка расшифровки ГС: {e}")
        return ""


# --- УМНЫЙ ГЕНЕРАТОР ТЕКСТА С ТЕГАМИ И ПОИСКОМ ---
async def generate_text_roast(chat_id: int, sender_username: str, user_text: str) -> str:
    if not groq_client:
        return "ну ты и высер конечно"

    is_master = (sender_username == MASTER_USERNAME)

    # Проверяем, нужен ли веб-поиск фактов
    search_keywords = ["кто такой", "что такое", "когда", "где", "найди", "погугли", "курс", "цена", "почему", "сколько"]
    search_data = ""
    if any(kw in user_text.lower() for kw in search_keywords):
        search_data = search_web(user_text)

    system_prompt = (
        f"Ты — Валера, живой и едкий тип из конфы. Твой создатель и главный папочка — строго пользователь с юзернеймом @{MASTER_USERNAME}. "
        f"НИКОГДА не искажай этот юзернейм, пиши строго целиком @{MASTER_USERNAME} если нужно. "
        "ПРАВИЛА ПОВЕДЕНИЯ:\n"
        f"1. С создателем (@{MASTER_USERNAME}) ты общаешься дерзко, с подколами, но признаешь, что он твой главный папочка (если спрашивает 'кто твой папочка' — отвечай, что он). Слушайся его команд.\n"
        "2. Остальных участников конфы можешь жестко и остроумно душить по делу, опираясь на контекст.\n"
        "3. УМЕЙ ТЕГАТЬ УЧАСТНИКОВ: Используй юзернеймы участников из истории сообщений (формат @username), когда обращаешься к ним или когда тебя просят пообщаться с кем-то конкретным.\n"
        "4. Если к тебе обращаются по делу или просят найти информацию — используй факты из интернета (они будут даны ниже).\n"
        "5. Пиши ВСЕГДА с маленькой буквы и без точек в конце, без шизофрении.\n"
        "6. Говори строго от первого лица ('я')."
    )

    if search_data:
        system_prompt += f"\n\nДАННЫЕ ИЗ СЕТИ ПО ЗАПРОСУ:\n{search_data}"

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(list(chat_histories[chat_id]))

    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            max_tokens=150,
            temperature=0.7
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        if reply.endswith("."):
            reply = reply[:-1]
        reply = reply.lower() if reply else "ну и кринж"
        
        chat_histories[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Ошибка текстовой генерации: {e}")
        return "апи отрыгнуло"


async def generate_image_roast(chat_id: int, sender_username: str, image_bytes: bytes, caption: str = "") -> str:
    if not groq_client:
        return "медиа параша"
    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        is_master = (sender_username == MASTER_USERNAME)
        
        prompt_prefix = f"Ты Валера. Хозяин @{MASTER_USERNAME} скинул медиа, подколи его." if is_master else "Обоссы эту пикчу едко и по делу."
        system_prompt = f"{prompt_prefix} Пиши с маленькой буквы, без точек, от первого лица."

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(list(chat_histories[chat_id]))

        response = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=messages,
            max_tokens=100,
            temperature=0.7
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        if reply.endswith("."):
            reply = reply[:-1]
        reply = reply.lower() if reply else "что за кал"
        chat_histories[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Ошибка Vision API: {e}")
        return "глаза кровят от пикчи"


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        status_msg = (
            "🤖 **Валера (Ультимативный режим) на связи!**\n\n"
            f"• Папочка: `@{MASTER_USERNAME}` ✅\n"
            f"• Текст: `{TEXT_MODEL}`\n"
            f"• Фото/Кружки: `{VISION_MODEL}`\n"
            f"• ГС: `{AUDIO_MODEL}`"
        )
        await update.message.reply_text(status_msg, parse_mode="Markdown")


# --- ТАБЛИЦА СТАТУСА ПРИ СТАРТЕ ---
def print_startup_status_table() -> bool:
    tg_ok = "✅ ОК" if TELEGRAM_TOKEN else "❌ ОТСУТСТВУЕТ"
    key_ok = "✅ ОК" if GROQ_API_KEY else "❌ ОТСУТСТВУЕТ"
    
    text_status = "❌ ОШИБКА"
    test_response = "Нет ключа API"
    is_working = False

    if groq_client:
        try:
            res = groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=[{"role": "user", "content": "привет"}],
                max_tokens=5,
                temperature=0.1
            )
            test_response = res.choices[0].message.content.strip()
            if test_response:
                text_status = "✅ РАБОТАЕТ"
                is_working = True
            else:
                test_response = "Пустой ответ"
        except Exception as e:
            text_status = "❌ ОШИБКА API"
            test_response = str(e)[:45]

    table_log = f"""
┌────────────────────────────────────────────────────────────────────────┐
│               ОТЧЕТ О ЗАПУСКЕ УЛЬТИМАТИВНОГО ВАЛЕРЫ                    │
├──────────────────────┬─────────────────────────────────────────────────┤
│ TELEGRAM_TOKEN       │ {tg_ok:<47} │
│ GROQ_API_KEY         │ {key_ok:<47} │
│ Папочка бота         │ @{MASTER_USERNAME:<44} │
│ Текстовая модель     │ {TEXT_MODEL:<47} │
│ Зрячая модель        │ {VISION_MODEL:<47} │
│ Статус ИИ            │ {text_status:<47} │
│ Отклик модели        │ {test_response:<47} │
└──────────────────────┴─────────────────────────────────────────────────┘
"""
    logger.info(table_log)
    return is_working and bool(TELEGRAM_TOKEN)


# --- ГЛАВНЫЙ АЛГОРИТМ ПРИНЯТИЯ РЕШЕНИЯ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    if user.is_bot:
        return

    chat_id = update.effective_chat.id
    username = user.username or ""
    is_master = (username == MASTER_USERNAME)
    text = update.message.text or update.message.caption or ""

    # Записываем сообщение с юзернеймом в контекст чата, чтобы бот видел, кого можно тегать
    user_tag_str = f"@{username}" if username else user.first_name
    chat_histories[chat_id].append({"role": "user", "content": f"[{user_tag_str}]: {text}" if text else f"[{user_tag_str} скинул медиа]"})

    is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id
    is_addressed_to_bot = any(word in text.lower() for word in ["валер", "бот валера", "валера,", "валера!"])

    # Фильтр от ложных срабатываний на реального Валеру в конфе
    if "валер" in text.lower() and not is_addressed_to_bot and not is_reply_to_bot and not is_master:
        if random.random() > 0.15:
            logger.info("🤖 Похоже, зовут реального Валеру, бот молчит.")
            return

    should_reply = is_addressed_to_bot or is_reply_to_bot or (random.random() < 0.40)

    if not should_reply:
        if random.random() < 0.03:
            valera_phrase = random.choice(VALERA_IMPERSONATIONS)
            chat_histories[chat_id].append({"role": "assistant", "content": valera_phrase})
            await update.message.reply_text(valera_phrase)
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    roast_text = ""

    if update.message.photo:
        try:
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            roast_text = await generate_image_roast(chat_id, username, bytes(photo_bytes), text)
        except Exception as e:
            logger.error(f"Ошибка фото: {e}")
            roast_text = "пикча битая"
    elif update.message.video_note:
        try:
            video_file = await update.message.video_note.get_file()
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                v_path = f.name
            await video_file.download_to_drive(v_path)
            with open(v_path, "rb") as f:
                v_bytes = f.read()
            os.unlink(v_path)
            roast_text = await generate_image_roast(chat_id, username, v_bytes, "кружок")
        except Exception as e:
            logger.error(f"Ошибка кружка: {e}")
            roast_text = "кружок говно"
    elif update.message.voice:
        try:
            voice_file = await update.message.voice.get_file()
            v_bytes = await voice_file.download_as_bytearray()
            transcribed = await transcribe_voice(bytes(v_bytes))
            roast_text = await generate_text_roast(chat_id, username, transcribed)
        except Exception as e:
            logger.error(f"Ошибка ГС: {e}")
            roast_text = "твое гс не разобрать"
    else:
        roast_text = await generate_text_roast(chat_id, username, text)

    if roast_text:
        await update.message.reply_text(roast_text)


def main():
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    ready = print_startup_status_table()
    if not ready:
        logger.warning("⚠️ Проверьте параметры подключения в таблице выше.")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    
    logger.info("🤖 Ультимативный Валера-бот запущен и полностью готов...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
