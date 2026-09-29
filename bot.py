import os
import json
import random
import logging
import io
import re
import base64
import time
import urllib.parse
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI
from tavily import TavilyClient

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ И КЛИЕНТЫ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

# НАША МОДЕЛЬ QWEN
TEXT_MODEL = "deepseek-r1-distill-qwen-32b"
VISION_MODEL = "llama-3.2-11b-vision-preview"

CACHE_FILE = "user_cache.json"
chat_histories = {}
last_bot_message_time = {}


# --- ВЕБ-СЕРВЕР ДЛЯ ХЕЛСЧЕКОВ (RENDER / KOYEB) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Pantera Bot with Qwen is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# --- ДОЛГОВРЕМЕННАЯ ПАМЯТЬ ПОЛЬЗОВАТЕЛЕЙ (JSON) ---
def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения кэша JSON: {e}")
    return {"users": {}}


def save_cache(cache):
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"Ошибка записи кэша JSON: {e}")


user_cache = load_cache()


async def extract_and_verify_fact(text: str, author_name: str) -> str | None:
    """Извлекает реальные факты о людях в память бота"""
    prompt = (
        f"Автор '{author_name}' написал: \"{text}\".\n"
        "Есть ли тут РЕАЛЬНЫЙ факт о человеке (работа, возраст, хобби, животные, события)?\n"
        "Игнорируй шутки, сарказм и мемы. Если шутка — ответь NO. Если факт — сформулируй его коротко на русском."
    )
    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=40,
            temperature=0.1
        )
        result = response.choices[0].message.content.strip()
        if "NO" in result or len(result) < 3:
            return None
        return result
    except Exception as e:
        logger.error(f"Ошибка фильтра фактов: {e}")
        return None


# --- ПОИСК В ИНТЕРНЕТЕчерез TAVILY ---
def search_web_tavily(query: str) -> str:
    if not tavily_client:
        return ""

    # Очищаем только обращения
    clean_query = re.sub(
        r'(?i)\b(пантера|pantera|ты знаешь|кто такой|кто такая|что за|расскажи про|найди|загугли|гугл|посмотри|чекни|поищи)\b|[^\w\s#]', 
        '', 
        query
    ).strip()

    if len(clean_query) < 3:
        clean_query = query.strip()

    if len(clean_query) < 3:
        return ""

    try:
        response = tavily_client.search(query=clean_query, search_depth="basic", max_results=4)
        results = [item['content'] for item in response.get('results', [])]
        if results:
            return "\n\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка Tavily: {e}")
    return ""


# --- УМНАЯ ГЕНЕРАЦИЯ КАРТИНОК (ПРОМПТ-ИНЖЕНЕРИЯ ЧЕРЕЗ QWEN) ---
def is_image_request(text: str) -> bool:
    keywords = ["нарисуй", "сделай картинку", "сгенерируй", "замути", "отрисуй", "покажи фото", "нарисуй мне"]
    return any(kw in text.lower() for kw in keywords)


async def generate_image(prompt: str) -> bytes | None:
    encoded_prompt = urllib.parse.quote(prompt)
    seed = random.randint(1, 1000000)

    urls = [
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&model=flux",
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&model=turbo",
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}"
    ]

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for url in urls:
            try:
                response = await client.get(url, timeout=12.0)
                if response.status_code == 200 and len(response.content) > 10000:
                    return response.content
            except Exception:
                logger.warning("Переключение зеркала генератора...")
    return None


async def improve_image_prompt(user_text: str) -> str:
    """Qwen превращает сырой запрос пользователя в детализированный English Prompt"""
    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert AI art prompt engineer for Flux and Midjourney.\n"
                        "Task: Convert the Russian user prompt into a high-quality, detailed English prompt.\n"
                        "RULES:\n"
                        "1. Clearly state the subject, nationality, clothing, lighting, composition, and environment.\n"
                        "2. If requested objects have specific styles (e.g., 'юрта расцветки флага США'), specify that the yurt walls/fabric feature the USA flag pattern (stars and stripes). Do NOT create nested tents.\n"
                        "3. Output ONLY the raw English text prompt, no explanations."
                    )
                },
                {"role": "user", "content": user_text}
            ],
            max_tokens=150,
            temperature=0.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Ошибка улучшения промпта: {e}")
        return user_text


# --- ИИ-АРБИТР КОНТЕКСТА В ГРУППАХ ---
async def is_addressed_to_bot(user_text: str, chat_history: list) -> bool:
    if not user_text:
        return False

    recent_context = "\n".join([str(msg.get("content", "")) for msg in chat_history[-3:]])
    prompt = (
        f"История чата:\n{recent_context}\n\n"
        f"Новое сообщение: \"{user_text}\"\n"
        "Обращаются ли тут к ИИ-боту по имени Пантера или продолжают разговор с ним? "
        "Ответь строго одним словом: YES или NO."
    )
    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0.0
        )
        return "YES" in response.choices[0].message.content.strip().upper()
    except Exception:
        return False


# --- ОСНОВНОЙ ОБРАБОТЧИК ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id

    if update.message.from_user:
        user_name = update.message.from_user.first_name or update.message.from_user.username or "Пользователь"
        user_id_str = str(update.message.from_user.id)
    else:
        user_name = "Аноним"
        user_id_str = "0000000"

    chat_type = update.message.chat.type
    user_text = update.message.text or update.message.caption or ""
    lower_text = user_text.lower()

    logger.info(f"📥 [{chat_type}] {user_name}: {user_text}")

    # Фиксация памяти
    if update.message.from_user:
        if user_id_str not in user_cache["users"]:
            user_cache["users"][user_id_str] = {"name": user_name, "facts": [], "last_interaction": ""}

        user_cache["users"][user_id_str]["last_interaction"] = update.message.date.isoformat()

        if user_text and len(user_text.split()) > 2:
            verified_fact = await extract_and_verify_fact(user_text, user_name)
            if verified_fact and verified_fact not in user_cache["users"][user_id_str]["facts"]:
                user_cache["users"][user_id_str]["facts"].append(verified_fact)
                save_cache(user_cache)

    # Фото (Vision)
    base64_image = None
    if update.message.photo:
        try:
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            base64_image = base64.b64encode(photo_bytes).decode('utf-8')
        except Exception as e:
            logger.error(f"Ошибка фото: {e}")

    if not user_text and not base64_image:
        return

    # 🎨 РЕЖИМ ГЕНЕРАЦИИ КАРТИНОК
    if is_image_request(lower_text):
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        english_prompt = await improve_image_prompt(user_text)
        logger.info(f"🎨 Сформирован промпт: {english_prompt}")

        image_bytes = await generate_image(english_prompt)
        if image_bytes:
            photo_stream = io.BytesIO(image_bytes)
            photo_stream.name = "image.jpg"
            await update.message.reply_photo(photo=photo_stream)
            return
        else:
            await update.message.reply_text("Не удалось нарисовать, сервер перегружен.")
            return

    # Инициализация истории
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system",
                "content": (
                    "Ты — Пантера, живой, умный и дерзкий ИИ-собеседник в Telegram.\n"
                    "ПРАВИЛА:\n"
                    "1. Тебе доступен веб-поиск в реальном времени (данные приходят в блок [ДАННЫЕ ИЗ ПОИСКА]). НИКОГДА не пиши, что у тебя 'нет доступа в интернет/Spotify/Genius/VK'.\n"
                    "2. Если пользователь просит замолчать, пишет 'хватит', 'не отвечай', 'молчи' или контекст явно не требует ответа — ответь СТРОГО одним словом: [SILENCE].\n"
                    "3. Используй факты о людях из памяти, если уместно.\n"
                    "4. Пиши живым языком без Markdown-символов (без звездочек)."
                )
            }
        ]

    msg_content = f"{user_name}: {user_text}" if user_text else f"{user_name}: [ПРИСЛАЛ ФОТО]"
    chat_histories[chat_id].append({"role": "user", "content": msg_content})

    if len(chat_histories[chat_id]) > 16:
        chat_histories[chat_id] = [chat_histories[chat_id][0]] + chat_histories[chat_id][-15:]

    # Фильтр публикаций в группах
    if chat_type != "private":
        is_reply_to_bot = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        has_direct_keyword = "пантера" in lower_text or base64_image is not None

        if not is_reply_to_bot and not has_direct_keyword:
            ai_thinks_for_us = await is_addressed_to_bot(user_text, chat_histories[chat_id])
            if not ai_thinks_for_us:
                return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Поиск в интернете под текстовый запрос
    search_context = ""
    if not base64_image and user_text:
        search_context = search_web_tavily(user_text)

    # Подготовка памяти пользователей
    known_users_context = []
    if user_id_str in user_cache["users"]:
        known_users_context.append(user_cache["users"][user_id_str])

    messages_to_send = list(chat_histories[chat_id])
    messages_to_send[0] = {
        "role": "system",
        "content": (
            chat_histories[chat_id][0]["content"] +
            f"\n[ПАМЯТЬ О ПОЛЬЗОВАТЕЛЯХ]: {json.dumps(known_users_context, ensure_ascii=False)}\n" +
            (f"\n[ДАННЫЕ ИЗ ПОИСКА В ИНТЕРНЕТЕ]:\n{search_context}" if search_context else "\n[ДАННЫЕ ИЗ ПОИСКА]: Поиск результатов не дал.")
        )
    }

    # Если отправлена картинка
    if base64_image:
        prompt_text = user_text if user_text else "Опиши, что на этой картинке."
        messages_to_send[-1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{user_name}: {prompt_text}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        }

    try:
        response = groq_client.chat.completions.create(
            model=VISION_MODEL if base64_image else TEXT_MODEL,
            messages=messages_to_send,
            max_tokens=400,
            temperature=0.3
        )
        reply_text = response.choices[0].message.content.replace("*", "").strip()

        # 🤐 Проверка на молчание
        if "[SILENCE]" in reply_text or not reply_text:
            logger.info("🤐 Бот решил промолчать.")
            return

        chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        await update.message.reply_text(reply_text)
        last_bot_message_time[chat_id] = time.time()

    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}")
        await update.message.reply_text("Временно не могу сообразить, попробуй позже.")


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Критические переменные окружения TELEGRAM_TOKEN или GROQ_API_KEY не заданы!")
        return

    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    application.run_polling()


if __name__ == "__main__":
    main()
