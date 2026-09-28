import os
import asyncio
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update, ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from google import genai
from google.genai import errors

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Получаем ключи из переменных окружения Render
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Инициализация клиента Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)


# 1. Веб-сервер для Health-check (чтобы Render не усыплял бота)
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


# 2. Функция обработки сообщений от пользователей
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_message = update.message.text
    user_name = update.effective_user.first_name
    logger.info(f"Получено сообщение от {user_name}: {user_message}")

    reply_text = None

    # Попытка №1: через основную модель gemini-3.8-flash
    try:
        response = client.models.generate_content(
            model='gemini-3.8-flash',
            contents=user_message,
        )
        reply_text = response.text
    except Exception as e:
        logger.warning(f"Основная модель недоступна, пробуем запасную. Ошибка: {e}")
        
        # Попытка №2: запасной вариант gemini-1.5-flash
        try:
            response = client.models.generate_content(
                model='gemini-1.5-flash',
                contents=user_message,
            )
            reply_text = response.text
        except Exception as e2:
            logger.error(f"Обе модели недоступны:\n{e2}")
            reply_text = "Серверы Google сейчас перегружены. Попробуй написать еще раз через пару секунд!"

    try:
        # Пытаемся отправить с Markdown-разметкой, чтобы звёздочки превращались в жирный текст
        await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        # Если модель сгенерировала «сломанный» Markdown (незакрытые символы), отправляем без разметки, чтобы не было ошибки
        await update.message.reply_text(reply_text)


# 3. Главная функция запуска бота
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Не задан TELEGRAM_TOKEN в переменных окружения!")
        return
    if not GEMINI_API_KEY:
        logger.error("Не задан GEMINI_API_KEY в переменных окружения!")
        return

    # Запускаем веб-сервер в отдельном потоке
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # Запуск Telegram бота
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Регистрируем обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    logger.info("Бот запущен и ожидает сообщения...")
    application.run_polling()


if __name__ == "__main__":
    main()
