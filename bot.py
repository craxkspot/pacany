import os
import random
import asyncio
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI  # Groq использует стандарт OpenAI

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Получаем ключи из переменных окружения Render
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# Инициализация клиента Groq (указываем их базовый URL и ключ)
client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

# Используем супер-быструю модель Llama 3 от Groq
GROQ_MODEL = "llama-3.3-70b-versatile"


# 1. Веб-сервер для Health-check (чтобы Render не усыплял бота)
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
    logger.info(f"Health-check сервер запущен на порту {port}")
    server.serve_forever()


# 2. Функция обработки сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_message = update.message.caption or update.message.text or ""
    user_name = update.effective_user.first_name or "Друг"
    user_username = update.effective_user.username or "без username"
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    lower_text = user_message.lower()

    logger.info(f"[{chat_type}] от @{user_username}: {user_message}")

    # Логика для групповых чатов (чтобы не спамить)
    if chat_type != "private":
        is_mentioned = "пантера" in lower_text
        is_reply_to_bot = (
            update.message.reply_to_message 
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        should_random_reply = random.random() < 0.25  # 25% шанс поддержать разговор

        if not is_mentioned and not is_reply_to_bot and not should_random_reply:
            return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Проверка наличия файлов (фото, голос, видео)
    file_description = ""
    try:
        if update.message.photo:
            file_description = "[Пользователь прикрепил фотографию]"
        elif update.message.voice:
            file_description = "[Пользователь прислал голосовое сообщение]"
        elif update.message.video_note:
            file_description = "[Пользователь прислал видео-кружочек]"
        elif update.message.video:
            file_description = "[Пользователь прислал видео]"
        elif update.message.document:
            file_description = "[Пользователь прислал документ]"
    except Exception as e:
        logger.error(f"Ошибка при обработке вложения: {e}")

    # Формируем запрос к модели
    full_prompt = f"Ты — Пантера, остроумный, живой и дерзкий участник чата. Пользователь {user_name} (@{user_username}) пишет: '{user_message} {file_description}'."

    reply_text = None
    try:
        # Запрос к Groq API (работает мгновенно и без лимитов Google)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Ты — Пантера, крутой ИИ-компаньон в Telegram. Отвечай емко, интересно, с характером."},
                {"role": "user", "content": full_prompt}
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


# 3. Главная функция
def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Не заданы TELEGRAM_TOKEN или GROQ_API_KEY!")
        return

    # Запускаем веб-сервер для Render в отдельном потоке
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))

    logger.info("Бот 'Пантера' на базе Groq запущен!")
    application.run_polling()


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
