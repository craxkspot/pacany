import os
import random
import asyncio
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from google import genai

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Panther Bot is alive and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health-check сервер запущен на порту {port}")
    server.serve_forever()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_message = update.message.text
    user_name = update.effective_user.first_name or "Друг"
    user_username = update.effective_user.username or "без username"
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    lower_text = user_message.lower()

    logger.info(f"Сообщение в [{chat_type}] от @{user_username} (ID: {user_id}, Имя: {user_name}): {user_message}")

    # Логика для групповых чатов (чтобы не отвечать на абсолютно каждое сообщение)
    if chat_type != "private":
        is_mentioned = "пантера" in lower_text
        is_reply_to_bot = (
            update.message.reply_to_message 
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        
        # Если не назвали по имени и не ответили на ее сообщение — 
        # даем шанс случайного вмешательства (например, в 25% случаев поддержать беседу/пошутить)
        should_random_reply = random.random() < 0.25

        if not is_mentioned and not is_reply_to_bot and not should_random_reply:
            return

    # Сообщаем пользователю, что Пантера печатает
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    reply_text = None
    max_retries = 3
    retry_delay = 3

    # Интеллектуальный запрос с авто-повтором при перегрузках
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=(
                    f"Ты — Пантера, остроумный и живой участник чата. "
                    f"Пользователь {user_name} (@{user_username}) пишет: '{user_message}'. "
                    f"Поддержи разговор, ответь на вопрос или пошути в своем стиле."
                ),
            )
            reply_text = response.text
            break
        except Exception as e:
            logger.warning(f"Попытка {attempt + 1} неудачна. Ошибка: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                reply_text = "Серверы Google сейчас сильно перегружены, я чуть позже вернусь к этой теме! 🐾"

    try:
        await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(reply_text)


def main():
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        logger.error("Не заданы TELEGRAM_TOKEN или GEMINI_API_KEY!")
        return

    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    logger.info("Бот 'Пантера' запущен и участвует в жизни чата...")
    application.run_polling()


if __name__ == "__main__":
    main()
