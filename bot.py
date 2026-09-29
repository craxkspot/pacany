import os
import json
import random
import logging
import io
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

# --- ЛОГИРОВАНИЕ ---
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

# МОДЕЛИ GROQ
TEXT_MODEL = "llama-3.3-70b-versatile"  # Флагманская модель с идеальной поддержкой Function Calling
VISION_MODEL = "llama-3.2-11b-vision-preview"

CACHE_FILE = "user_cache.json"
chat_histories = {}
last_bot_message_time = {}


# --- HEALTH CHECK (ДЛЯ ДЕПЛОЯ НА RENDER/KOYEB) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Pantera Bot is operational!")

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
    """Фильтрует факты о пользователях для памяти"""
    prompt = (
        f"Автор '{author_name}' написал: \"{text}\".\n"
        "Содержит ли текст РЕАЛЬНЫЙ, конкретный факт о человеке (профессия, возраст, хобби, животные, реальные события)?\n"
        "Игнорируй сарказм, шутки и мемы. Если это шутка — ответь NO. Если факт — сформулируй его коротко на русском."
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


# --- ИНСТРУМЕНТЫ (TOOLS) ДЛЯ FUNCTION CALLING ---

def search_web_tavily(query: str) -> str:
    """Инструмент поиска в вебе через Tavily API"""
    if not tavily_client:
        return "Поисковая система не настроена."

    logger.info(f"🔍 ВЫЗОВ ИНСТРУМЕНТА search_web_tavily С ЗАПРОСОМ: '{query}'")
    try:
        response = tavily_client.search(query=query, search_depth="basic", max_results=4)
        results = [item['content'] for item in response.get('results', [])]
        if results:
            return "\n\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка поиска Tavily: {e}")
    return "Информация по данному запросу в сети не найдена."


async def generate_image(prompt: str) -> bytes | None:
    """Инструмент генерации картинок через Pollinations API"""
    logger.info(f"🎨 ВЫЗОВ ИНСТРУМЕНТА generate_image С ПРОМПТОМ: '{prompt}'")
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
                logger.warning("Переключение модели Pollinations...")
    return None


# Описание инструментов для схемы OpenAI / Groq
TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Используй этот инструмент, если пользователь спрашивает о фактах, новостях, музыке, "
                "исполнителях, фитах, релизах, людях или любых данных, в которых ты не уверен на 100%."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Оптимизированный поисковый запрос (например: 'исполнитель asyamf фиты релизы')"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Используй этот инструмент, когда пользователь просит нарисовать, сгенерировать, "
                "показать или создать картинку/фото/изображение."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Подробный и качественный описательный промпт НА АНГЛИЙСКОМ ЯЗЫКЕ. "
                            "Включай все детали, стилистику, освещение и точно передавай культурные или исторические объекты."
                        )
                    }
                },
                "required": ["prompt"]
            }
        }
    }
]


# --- ПРОВЕРКА ОБРАЩЕНИЯ В ГРУППЕ ---
async def is_addressed_to_bot(user_text: str, chat_history: list) -> bool:
    if not user_text:
        return False

    recent_context = "\n".join([str(msg.get("content", "")) for msg in chat_history[-3:]])
    prompt = (
        f"История чата:\n{recent_context}\n\n"
        f"Новое сообщение: \"{user_text}\"\n"
        "Является ли это сообщение продолжением диалога с ИИ-ботом Пантерой или обращением к нему? "
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

    logger.info(f"📥 [{chat_type}] {user_name}: {user_text}")

    # Запись долгосрочных фактов
    if update.message.from_user:
        if user_id_str not in user_cache["users"]:
            user_cache["users"][user_id_str] = {"name": user_name, "facts": [], "last_interaction": ""}

        user_cache["users"][user_id_str]["last_interaction"] = update.message.date.isoformat()

        if user_text and len(user_text.split()) > 2:
            verified_fact = await extract_and_verify_fact(user_text, user_name)
            if verified_fact and verified_fact not in user_cache["users"][user_id_str]["facts"]:
                user_cache["users"][user_id_str]["facts"].append(verified_fact)
                save_cache(user_cache)

    # Фото (Vision Flow)
    base64_image = None
    if update.message.photo:
        try:
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            base64_image = base64.b64encode(photo_bytes).decode('utf-8')
        except Exception as e:
            logger.error(f"Ошибка получения фото: {e}")

    if not user_text and not base64_image:
        return

    # Инициализация истории
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system",
                "content": (
                    "Ты — Пантера, живой, умный и дерзкий ИИ-собеседник в Telegram.\n"
                    "ТЕБЕ ДОСТУПНЫ ИНСТРУМЕНТЫ (Tools): поиск в интернете и генерация картинок.\n"
                    "ПРАВИЛА:\n"
                    "1. Если тебе не хватает данных, вызывай инструмент `web_search`. НИКОГДА не врешь и не говоришь, что у тебя 'нет доступа в интернет/Spotify/Genius'.\n"
                    "2. Если просят нарисовать картинку — вызывай `generate_image` и передавай туда качественный англоязычный промпт.\n"
                    "3. Пиши живым языком, без звезд, хештегов и Markdown-разметки."
                )
            }
        ]

    msg_content = f"{user_name}: {user_text}" if user_text else f"{user_name}: [ПРИСЛАЛ ФОТО]"
    chat_histories[chat_id].append({"role": "user", "content": msg_content})

    # Ограничение размера контекста
    if len(chat_histories[chat_id]) > 16:
        chat_histories[chat_id] = [chat_histories[chat_id][0]] + chat_histories[chat_id][-15:]

    # Фильтр активности в группах
    if chat_type != "private":
        is_reply_to_bot = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        has_direct_keyword = "пантера" in user_text.lower() or base64_image is not None
        recent_bot_activity = (chat_id in last_bot_message_time) and (time.time() - last_bot_message_time[chat_id] < 30)

        if not is_reply_to_bot and not has_direct_keyword:
            ai_thinks_for_us = await is_addressed_to_bot(user_text, chat_histories[chat_id])
            if not ai_thinks_for_us and not recent_bot_activity and random.random() < 0.85:
                return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Подготовка системного контекста с памятью
    known_users_context = []
    if user_id_str in user_cache["users"]:
        known_users_context.append(user_cache["users"][user_id_str])

    messages_to_send = list(chat_histories[chat_id])
    messages_to_send[0] = {
        "role": "system",
        "content": (
            chat_histories[chat_id][0]["content"] +
            f"\n[ПАМЯТЬ О ПОЛЬЗОВАТЕЛЯХ]: {json.dumps(known_users_context, ensure_ascii=False)}"
        )
    }

    # Если отправлено изображение — используем Vision модель
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
                model=VISION_MODEL,
                messages=messages_to_send,
                max_tokens=400
            )
            reply = response.choices[0].message.content.replace("*", "")
            chat_histories[chat_id].append({"role": "assistant", "content": reply})
            await update.message.reply_text(reply)
            last_bot_message_time[chat_id] = time.time()
        except Exception as e:
            logger.error(f"Ошибка Vision API: {e}")
            await update.message.reply_text("Не смогла разглядеть изображение.")
        return

    # --- ЦИКЛ ИСПОЛНЕНИЯ FUNCTION CALLING ---
    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages_to_send,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            temperature=0.3
        )

        response_message = response.choices[0].message
        tool_calls = response_message.tool_calls

        # Если модель решила задействовать инструменты
        if tool_calls:
            # Важно: сохраняем решение модели с вызовами функций в историю диалога
            messages_to_send.append(response_message)

            for tool_call in tool_calls:
                function_name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments)

                # ВЫЗОВ ГЕНЕРАТОРА КАРТИНОК
                if function_name == "generate_image":
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
                    img_prompt = arguments.get("prompt", user_text)
                    image_bytes = await generate_image(img_prompt)

                    if image_bytes:
                        photo_stream = io.BytesIO(image_bytes)
                        photo_stream.name = "image.jpg"
                        await update.message.reply_photo(photo=photo_stream)
                        chat_histories[chat_id].append({
                            "role": "assistant", 
                            "content": f"[Сгенерирована картинка по промпту: {img_prompt}]"
                        })
                    else:
                        await update.message.reply_text("Сервер генерации картинок сейчас перегружен, попробуй позже.")
                    
                    last_bot_message_time[chat_id] = time.time()
                    return

                # ВЫЗОВ ВЕБ-ПОИСКА
                elif function_name == "web_search":
                    search_query = arguments.get("query", user_text)
                    search_results = search_web_tavily(search_query)

                    # Передаем результат работы инструмента обратно модели
                    messages_to_send.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": function_name,
                        "content": search_results
                    })

            # Финальный вызов модели для составления ответа на базе полученных из поиска данных
            final_response = groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=messages_to_send,
                temperature=0.4
            )
            reply_text = final_response.choices[0].message.content.replace("*", "")
            chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
            await update.message.reply_text(reply_text)
            last_bot_message_time[chat_id] = time.time()
            return

        # Если инструменты не потребовались — обычный разговор
        reply_text = response_message.content.replace("*", "")
        chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        await update.message.reply_text(reply_text)
        last_bot_message_time[chat_id] = time.time()

    except Exception as e:
        logger.error(f"Ошибка Groq API / Function Calling: {e}")
        await update.message.reply_text("Что-то пошло не так при обработке запроса.")


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Критические переменные окружения TELEGRAM_TOKEN или GROQ_API_KEY не заданы!")
        return

    # Запуск фонового веб-сервера для хелсчеков
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # Запуск Telegram бота
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    application.run_polling()


if __name__ == "__main__":
    main()
