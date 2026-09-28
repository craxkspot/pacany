import os
import random
import asyncio
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

# Функция выбора надежной текстовой модели из твоего списка
def get_active_groq_model():
    try:
        models = client.models.list()
        available = [m.id for m in models.data]
        logger.info(f"Доступные модели: {available}")
        
        # Приоритет актуальным текстовым моделям из твоего списка
        for preferred in ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]:
            if preferred in available:
                logger.info(f"Выбрана приоритетная текстовая модель: {preferred}")
                return preferred
                
        # Если вдруг их нет, ищем любую, в названии которой нет whisper/guard
        for m_id in available:
            if "whisper" not in m_id and "guard" not in m_id and "orpheus" not in m_id:
                logger.info(f"Выбрана альтернативная модель: {m_id}")
                return m_id
    except Exception as e:
        logger.error(f"Не удалось получить список моделей: {e}")
    
    return "openai/gpt-oss-20b"


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Panther Bot with Groq is alive and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_message = update.message.caption or update.message.text or ""
    user_name = update.effective_user.first_name or "Друг"
    user_username = update.effective_user.username or "без username"
    chat_type = update.message.chat.type
    lower_text = user_message.lower()

    if chat_type != "private":
        is_mentioned = "пантера" in lower_text
        is_reply_to_bot = (
            update.message.reply_to_message 
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        should_random_reply = random.random() < 0.25

        if not is_mentioned and not is_reply_to_bot and not should_random_reply:
            return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    active_model = get_active_groq_model()

    reply_text = None
    try:
        response = client.chat.completions.create(
            model=active_model,
            messages=[
                {"role": "system", "content": "Ты — Пантера, крутой ИИ-компаньон в Telegram. Отвечай емко, интересно, с характером."},
                {"role": "user", "content": f"Пользователь {user_name} (@{user_username}) пишет: '{user_message}'"}
            ],
            temperature=0.7,
        )
        reply_text = response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}")
        reply_text = "Хм, что-то у меня в мыслях закоротило. Попробуй написать еще раз! 🐾"

    try:
        await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(reply_text)


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Не заданы TELEGRAM_TOKEN или GROQ_API_KEY!")
        return

    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    chosen_model = get_active_groq_model()
    logger.info(f"Бот запущен. Финальная модель: {chosen_model}")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))

    application.run_polling()


if __name__ == "__main__":
    main()
