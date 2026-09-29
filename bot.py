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

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

TEXT_MODEL = "qwen/qwen3.8-27b"
VISION_MODEL = "llama-3.2-11b-vision-preview"

CACHE_FILE = "user_cache.json"
chat_histories = {}
last_bot_message_time = {}  # Для отслеживания ответов вдогонку


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Pantera Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# --- ДОЛГОВРЕМЕННАЯ ПАМЯТЬ (JSON) ---
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


def get_user_full_name(user):
    if not user:
        return "Пользователь"
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    username = f" (@{user.username})" if user.username else ""
    full_name = f"{first_name} {last_name}{username}".strip()
    if not full_name:
        full_name = "Аноним"

    user_id = str(user.id)
    if user_id not in user_cache["users"]:
        user_cache["users"][user_id] = {
            "name": full_name,
            "facts": [],
            "last_interaction": ""
        }
        save_cache(user_cache)
    return full_name


async def extract_and_verify_fact(text: str, author_name: str) -> str | None:
    """ИИ-фильтр: отделяет шутки, сарказм и бред от реальных фактов о пользователях"""
    prompt = (
        f"Автор сообщения '{author_name}' написал: \"{text}\".\n"
        "Содержит ли этот текст РЕАЛЬНЫЙ, конкретный факт о ком-то из людей (например: профессия, возраст, хобби, домашние животные, реальные события из жизни)?\n"
        "ПРАВИЛА:\n"
        "1. Игнорируй шутки, сарказм, метафоры, оскорбления в шутливой форме, мемы и очевидный бред.\n"
        "2. Если это шутка или пустые слова, ответь строго одним словом: NO.\n"
        "3. Если это реальный факт, сформулируй его коротко на русском языке. Выдай ТОЛЬКО этот факт без лишних слов."
    )
    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0.1
        )
        result = response.choices[0].message.content.strip()
        if "NO" in result or len(result) < 3:
            return None
        return result
    except Exception as e:
        logger.error(f"Ошибка фильтра фактов: {e}")
        return None


# --- ГЕНЕРАЦИЯ КАРТИНОК С FALLBACK ---
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
                logger.warning(f"Таймаут или ошибка генерации, переключаем модель...")
    return None


# --- ПОИСК TAVILY ---
def search_web_tavily(query: str) -> str:
    if not tavily_client:
        return ""
    
    clean_query = re.sub(
        r'(?i)\b(пантера|pantera|ты знаешь|кто такой|кто такая|что за|расскажи про|найди|загугли|гугл|найди)\b', 
        '', 
        query
    ).strip()
    
    if not clean_query:
        clean_query = query

    try:
        response = tavily_client.search(query=clean_query, search_depth="basic", max_results=3)
        results = [item['content'] for item in response.get('results', [])]
        if results:
            return "\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка поиска Tavily: {e}")
    return ""


def is_image_request(text: str) -> bool:
    keywords = ["нарисуй", "сделай картинку", "сгенерируй", "замути", "отрисуй", "покажи", "сделай фото"]
    return any(kw in text.lower() for kw in keywords)


# --- ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    user_name = get_user_full_name(update.effective_user)
    user_id_str = str(update.effective_user.id)
    chat_type = update.message.chat.type

    user_text = update.message.text or update.message.caption or ""
    lower_text = user_text.lower()

    # Обновляем таймстамп и проверяем факты через ИИ-фильтр
    if user_id_str in user_cache["users"]:
        user_cache["users"][user_id_str]["last_interaction"] = update.message.date.isoformat()
        
        if user_text and len(user_text.split()) > 1:
            verified_fact = await extract_and_verify_fact(user_text, user_name)
            if verified_fact:
                if verified_fact not in user_cache["users"][user_id_str]["facts"]:
                    user_cache["users"][user_id_str]["facts"].append(verified_fact)
                    save_cache(user_cache)
                    logger.info(f"Записан факт о {user_name}: {verified_fact}")

    # Обработка вложений (Vision) — поддержка фото и прикрепленных медиа
    base64_image = None
    if update.message.photo:
        try:
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            base64_image = base64.b64encode(photo_bytes).decode('utf-8')
        except Exception as e:
            logger.error(f"Ошибка Vision: {e}")

    if not user_text and not base64_image:
        return

    # Генерация картинок
    if is_image_request(lower_text):
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        
        prompt_for_image = lower_text
        for kw in ["нарисуй", "сделай", "сгенерируй", "замути", "пантера", "фото", "картинку"]:
            prompt_for_image = prompt_for_image.replace(kw, "").strip()
            
        if not prompt_for_image:
            prompt_for_image = user_text

        try:
            enh_resp = groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=[
                    {
                        "role": "system", 
                        "content": (
                            "You are an expert prompt engineer. Translate to a detailed English prompt. "
                            "Handle Russian slang ORGANICALLY: 'пудж' = Pudge Dota 2, 'скуф' = unkempt middle aged man, 'альтушка' = alt girl. "
                            "Output ONLY the English prompt."
                        )
                    },
                    {"role": "user", "content": prompt_for_image}
                ],
                max_tokens=150
            )
            detailed_prompt = enh_resp.choices[0].message.content.strip()
        except Exception:
            detailed_prompt = prompt_for_image

        image_bytes = await generate_image(detailed_prompt)
        if image_bytes:
            photo_stream = io.BytesIO(image_bytes)
            photo_stream.name = "image.jpg"
            await update.message.reply_photo(photo=photo_stream)
            return
        else:
            await update.message.reply_text("Не получилось сгенерировать, сервер перегружен.")
            return

    # Инициализация оперативной истории
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system", 
                "content": (
                    "Ты — адекватный, живой ИИ-собеседник. Ты подстраиваешься под вайб чата. "
                    "ПРАВИЛА:\n"
                    "1. Используй факты о пользователях из переданного JSON контекста, если они уместны.\n"
                    "2. Не придумывай лишней конспирологии, понимай сленг (например 'ЗБС' — это сокращение от 'заебись' / круто).\n"
                    "3. Пиши ТОЛЬКО обычным плоским текстом без звездочек и Markdown."
                )
            }
        ]

    msg_content = f"{user_name}: {user_text}" if user_text else f"{user_name}: [ПРИСЛАЛ ФОТО]"
    chat_histories[chat_id].append({"role": "user", "content": msg_content})
    
    if len(chat_histories[chat_id]) > 14:
        system_prompt = chat_histories[chat_id][0]
        recent_msgs = chat_histories[chat_id][-13:]
        chat_histories[chat_id] = [system_prompt] + recent_msgs

    # Условия активации бота в группе (разрешаем реагировать на реплаи/упоминания даже от других ботов)
    is_reply_to_bot = (
        update.message.reply_to_message 
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    is_mentioned = "пантера" in lower_text or base64_image is not None
    recent_bot_activity = (chat_id in last_bot_message_time) and (time.time() - last_bot_message_time[chat_id] < 30)

    if chat_type != "private" and not is_reply_to_bot and not is_mentioned and not recent_bot_activity and random.random() < 0.88:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Сборка долговременной памяти (фактов о пользователе)
    known_users_context = []
    if user_id_str in user_cache["users"]:
        u_data = user_cache["users"][user_id_str]
        known_users_context.append({
            "name": u_data["name"],
            "real_facts": u_data["facts"]
        })

    # Поиск информации, если нужен
    search_context = ""
    if not base64_image:
        needs_search_keywords = ["кто", "что", "знаешь", "найди", "загугли", "гугл", "инфа", "расскажи про"]
        should_search = any(kw in lower_text for kw in needs_search_keywords) or len(user_text.split()) <= 3

        if should_search:
            search_context = search_web_tavily(user_text)

    messages_to_send = list(chat_histories[chat_id])
    messages_to_send[0] = {
        "role": "system",
        "content": (
            chat_histories[chat_id][0]["content"] +
            f"\n[ДОЛГОВРЕМЕННАЯ ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ]: {json.dumps(known_users_context, ensure_ascii=False)}\n" +
            (f"\n[ДАННЫЕ ИЗ ПОИСКА]: {search_context}" if search_context else "")
        )
    }

    active_model = TEXT_MODEL
    if base64_image:
        active_model = VISION_MODEL
        prompt_text = user_text if user_text else "Опиши, что на этой картинке."
        messages_to_send[-1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{user_name} прикрепил фото с текстом: {prompt_text}"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                }
            ]
        }

    try:
        response = groq_client.chat.completions.create(
            model=active_model,
            messages=messages_to_send,
            max_tokens=400,
            temperature=0.25,
        )
        reply_text = response.choices[0].message.content
        
        if reply_text:
            reply_text = reply_text.replace("*", "")
            chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        else:
            reply_text = "Что-то процессор перегрелся, не понял запрос."
    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}")
        reply_text = "Сервер временно недоступен."

    try:
        sent_msg = await update.message.reply_text(reply_text)
        last_bot_message_time[chat_id] = time.time()
    except Exception as e:
        logger.error(f"Ошибка отправки Telegram: {e}")


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Токены не заданы! Проверь переменные окружения.")
        return

    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    application.run_polling()


if __name__ == "__main__":
    main()
