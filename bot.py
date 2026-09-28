import os
import asyncio
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
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
# Если вы используете старую библиотеку (google-generativeai), этот код может отличаться.
# Для современной официальной библиотеки используется genai.Client()
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

    # Отключаем лишний вывод логов сервера в консоль
    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health-check сервер запущен на порту {port}")
    server.serve_forever()


# 2. Функция обработки сообщений от пользователей
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_name = update.effective_user.first_name
    logger.info(f"Получено сообщение от {user_name}: {user_message}")

    try:
        # Отправляем запрос к Gemini
        # Используем актуальную модель gemini-2.5-flash (или gemini-1.5-flash)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_message,
        )
        
        reply_text = response.text
        await update.message.reply_text(reply_text)

    except errors.APIError as e:
        # Ошибка со стороны API Google (например, неверный ключ, квоты)
        logger.error(f"Ошибка Gemini API: {e}")
        await update.message.reply_text(f"Ошибка со стороны ИИ: {e}")
    except Exception as e:
        # Любая другая непредвиденная ошибка (выведет полный стектрейс в логи Render)
        import traceback
        logger.error(f"Критическая ошибка:\n{traceback.format_exc()}")
        await update.message.reply_text(f"Ой, мои мыслительные процессы сломались! Ошибка: {e}")


# 3. Главная функция запуска бота
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Не задан TELEGRAM_TOKEN в переменных окружения!")
        return
    if not GEMINI_API_KEY:
        logger.error("Не задан GEMINI_API_KEY в переменных окружения!")
        return

    # Запускаем веб-сервер в отдельном потоке, чтобы Render видел открытый порт
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # Запуск Telegram бота
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

д    # Регистрируем обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    logger.info("Бот запущен и ожидает сообщения...")
    application.run_polling()


if __name__ == "__main__":
    main()
