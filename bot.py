import os
import random
import logging
import io
import re
import urllib.parse
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI
from duckduckgo_search import DDGS

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

GROQ_MODEL = "qwen/qwen3.8-27b"
chat_histories = {}


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


async def generate_image(prompt: str) -> bytes | None:
    """Генерация картинок через Pollinations Flux"""
    encoded_prompt = urllib.parse.quote(prompt)
    seed = random.randint(1, 1000000)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&model=flux"
    
    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
        try:
            response = await client.get(image_url)
            if response.status_code == 200:
                return response.content
            else:
                logger.error(f"Ошибка от сервера картинок: {response.status_code}")
        except Exception as e:
            logger.error(f"Ошибка генерации картинки: {e}")
    return None


def search_web(query: str) -> str:
    """Поиск в интернете через DuckDuckGo"""
    clean_query = re.sub(
        r'(?i)\b(пантера|pantera|ты знаешь|кто такой|кто такая|что за|расскажи про|найти|загугли|гугл)\b', 
        '', 
        query
    ).strip()
    
    if not clean_query:
        clean_query = query

    try:
        with DDGS() as ddgs:
            results = [r.get('body', '') for r in ddgs.text(clean_query, max_results=3)]
            if results:
                return "\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
    return ""


def is_image_request(text: str) -> bool:
    """Проверка запроса на генерацию изображения"""
    keywords = [
        "нарисуй", "сделай картинку", "сгенерируй", "создай изображение", 
        "сделай фото", "замути", "отрисуй", "покажи", "изобрази", "запили фотку", "запили картинку"
    ]
    return any(kw in text.lower() for kw in keywords)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name or "Пользователь"
    chat_type = update.message.chat.type

    user_text = update.message.text or update.message.caption or ""
    lower_text = user_text.lower()

    image_url = None
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        image_url = photo_file.file_path

    if not user_text and not image_url:
        return

    # 1. ГЕНЕРАЦИЯ КАРТИНОК
    if is_image_request(lower_text):
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        
        prompt_for_image = lower_text
        remove_words = [
            "нарисуй", "сделай", "сгенерируй", "создай", "замути", "отрисуй", 
            "покажи", "пантера", "pantera", "фотку", "фото", "картинку", "изображение"
        ]
        for word in remove_words:
            prompt_for_image = re.sub(r'\b' + re.escape(word) + r'\b', '', prompt_for_image, flags=re.IGNORECASE).strip()
            
        if not prompt_for_image:
            prompt_for_image = user_text

        # Промпт-инжиниринг с жестким распределением объектов по планам
        try:
            enh_resp = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "system", 
                        "content": (
                            "You are an expert prompt engineer for AI image generators (Flux/Stable Diffusion). "
                            "Translate the user's request into a descriptive English prompt with clear composition: "
                            "1. Foreground/Subject: Describe main characters or actions. "
                            "2. Midground/Background: Describe structures, yurts, buildings, environment. "
                            "3. Specific Attachments: Clearly state if flags/symbols are attached TO specific objects (e.g. 'an American flag mounted on the side of a Kazakh yurt'). "
                            "Understand Russian gaming and meme slang ('пудж' = Pudge Dota 2, 'скуф' = sloppy middle-aged man, 'альтушка' = alt girl). "
                            "Output ONLY the English prompt text."
                        )
                    },
                    {"role": "user", "content": prompt_for_image}
                ],
                max_tokens=180
            )
            detailed_prompt = enh_resp.choices[0].message.content.strip()
            logger.info(f"Сгенерированный промпт для картинки: {detailed_prompt}")
        except Exception:
            detailed_prompt = prompt_for_image

        image_bytes = await generate_image(detailed_prompt)
        if image_bytes:
            photo_stream = io.BytesIO(image_bytes)
            photo_stream.name = "image.jpg"
            await update.message.reply_photo(photo=photo_stream)
            return
        else:
            await update.message.reply_text("Сервер генерации сейчас перегружен, попробуй еще раз.")
            return

    # 2. ИНИЦИАЛИЗАЦИЯ ИСТОРИИ ЧАТА
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system", 
                "content": (
                    "Ты — адекватный, живой ИИ-собеседник. Ты общаешься естественно, подстраиваешься под вайб чата, понимаешь сленг и иронию.\n"
                    "ПРАВИЛА ОТВЕТА:\n"
                    "1. Если в запросе или результатах поиска нет информации про человека, никнейм или место — ЧЕСТНО скажи, что не знаешь.\n"
                    "2. СТРОГО ЗАПРЕЩЕНО выдумывать фейковые биографии людей и вымышленные факты.\n"
                    "3. Никогда не говори 'я не могу гуглить' или 'у меня нет сети'. Поиск работает автоматически.\n"
                    "4. Пиши ТОЛЬКО обычным плоским текстом без звездочек и разметки."
                )
            }
        ]

    if image_url:
        msg_content = f"{user_name} прислал картинку. Текст: {user_text}" if user_text else f"{user_name} прислал картинку."
    else:
        msg_content = f"{user_name}: {user_text}"

    chat_histories[chat_id].append({"role": "user", "content": msg_content})
    
    # Ограничение длины истории (сохраняем ровно 1 системный промпт + последние сообщения)
    if len(chat_histories[chat_id]) > 12:
        system_prompt = chat_histories[chat_id][0]
        recent_msgs = chat_histories[chat_id][-11:]
        chat_histories[chat_id] = [system_prompt] + recent_msgs

    is_reply_to_bot = (
        update.message.reply_to_message 
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    is_mentioned = "пантера" in lower_text or image_url is not None
    
    should_random_speak = random.random() < 0.10
    if chat_type != "private" and not is_reply_to_bot and not is_mentioned and not should_random_speak:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # 3. ПОИСК И ПОДГОТОВКА ЗАПРОСА К GROQ
    search_context = ""
    needs_search_keywords = ["кто", "что", "где", "когда", "почему", "знаешь", "знаешь ли", "найди", "инфа", "инфу", "загугли", "гугл"]
    should_search = any(kw in lower_text for kw in needs_search_keywords) or len(user_text.split()) <= 3

    if should_search:
        search_result = search_web(user_text)
        if search_result:
            search_context = f"\n\n[ДАННЫЕ ИЗ ПОИСКА В ИНТЕРНЕТЕ]:\n{search_result}"
        else:
            search_context = "\n\n[ДАННЫЕ ИЗ ПОИСКА]: Информация в сети не найдена. Если не знаешь ответа — честно скажи об этом."

    # Собираем временный массив сообщений для отправки в API (чтобы не портить роли в истории)
    messages_to_send = list(chat_histories[chat_id])
    if search_context:
        # Внедряем результаты поиска строго в системный промпт (индекс 0)
        messages_to_send[0] = {
            "role": "system",
            "content": chat_histories[chat_id][0]["content"] + search_context
        }

    # 4. ГЕНЕРАЦИЯ ОТВЕТА
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages_to_send,
            max_tokens=400,
            temperature=0.6,
        )
        reply_text = response.choices[0].message.content
        
        if reply_text:
            reply_text = reply_text.replace("*", "")
            chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        else:
            reply_text = "Не совсем понял запрос, повтори еще раз."
    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}")
        reply_text = "Сервер временно недоступен."

    try:
        await update.message.reply_text(reply_text)
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
