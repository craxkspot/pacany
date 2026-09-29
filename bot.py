import os
import random
import logging
import io
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
    """Генерация картинок через Pollinations"""
    encoded_prompt = urllib.parse.quote(prompt)
    seed = random.randint(1, 1000000)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&nologo=true"
    
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        try:
            response = await client.get(image_url)
            if response.status_code == 200:
                return response.content
        except Exception as e:
            logger.error(f"Ошибка генерации картинки: {e}")
    return None


def search_web(query: str) -> str:
    """Поиск в интернете через DuckDuckGo"""
    try:
        with DDGS() as ddgs:
            results = [r.get('body', '') for r in ddgs.text(query, max_results=3)]
            if results:
                return "\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
    return ""


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

    # Обработка команд генерации картинок
    image_triggers = ["нарисуй", "сделай картинку", "сгенерируй", "создай изображение", "сделай фото"]
    if any(kw in lower_text for kw in image_triggers):
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        
        # Вычищаем триггеры, слово "фото" и имя бота, чтобы они не ломали смысл картинки
        prompt_for_image = lower_text
        for kw in image_triggers + ["пантера", "pantera", "фото", "картинку"]:
            prompt_for_image = prompt_for_image.replace(kw, "").strip()
            
        if not prompt_for_image:
            prompt_for_image = user_text

        try:
            enh_resp = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": "You are an expert prompt engineer for AI image generators. The user is asking a bot to draw something. Extract ONLY the core subject the user wants to see. Translate it into a highly detailed, cinematic English prompt for Midjourney. Output ONLY the English prompt. Do not output any conversational text."},
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
            await update.message.reply_text("Не получилось сгенерировать картинку, сервер не отвечает.")
            return

    # Инициализация истории чата (Умный системный промпт с жесткими правилами)
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system", 
                "content": (
                    "Ты — умный, живой и адекватный ИИ-собеседник. Общайся естественно, как эрудированный человек, "
                    "подстраивайся под вайб чата. У тебя ЕСТЬ встроенный доступ к интернету. НИКОГДА не говори, что "
                    "у тебя нет доступа к сети, что ты не можешь гуглить или что ты оффлайн-модель. Поиск работает. "
                    "Пиши ТОЛЬКО обычным текстом. СТРОГО запрещено использовать Markdown-разметку (никаких звездочек)."
                )
            }
        ]

    # Формирование сообщения для истории
    if image_url:
        msg_content = f"{user_name} прислал картинку. Текст: {user_text}" if user_text else f"{user_name} прислал картинку."
    else:
        msg_content = f"{user_name}: {user_text}"

    chat_histories[chat_id].append({"role": "user", "content": msg_content})
    
    # Безопасное ограничение длины истории
    if len(chat_histories[chat_id]) > 14:
        system_prompt = chat_histories[chat_id][0]
        recent_msgs = chat_histories[chat_id][-13:]
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

    # Умная проверка на необходимость поиска
    try:
        check_resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Does this message mention a specific person, artist, brand, fact, news, or require looking up real-time information? Answer ONLY 'YES' or 'NO'."},
                {"role": "user", "content": user_text}
            ],
            max_tokens=5
        )
        decision = check_resp.choices[0].message.content.strip().upper()
        if "YES" in decision:
            search_result = search_web(user_text)
            if search_result:
                chat_histories[chat_id].append({
                    "role": "system", 
                    "content": f"Вот информация из интернета (DuckDuckGo), используй её для ответа, не упоминай сам процесс поиска:\n{search_result}"
                })
    except Exception as e:
        logger.error(f"Ошибка при попытке поиска: {e}")

    # Генерация финального ответа
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=chat_histories[chat_id],
            max_tokens=400,
            temperature=0.75,
        )
        reply_text = response.choices[0].message.content
        
        if reply_text:
            # ЖЕСТКАЯ ЗАЧИСТКА: физически вырезаем все звездочки из ответа перед отправкой
            reply_text = reply_text.replace("*", "")
            chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        else:
            reply_text = "Я тут, но что-то сбилось в мыслях."
    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}")
        reply_text = "Ошибка сети, не могу связаться с мозгом."

    try:
        await update.message.reply_text(reply_text)
    except Exception as e:
        logger.error(f"Ошибка отправки Telegram: {e}")


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Токены не заданы! Проверь переменные окружения.")
        return

    # Запуск веб-сервера для Render
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # Запуск бота
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    application.run_polling()


if __name__ == "__main__":
    main()
