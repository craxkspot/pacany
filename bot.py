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
from google.genai import types

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
    if not update.message:
        return

    # Извлекаем текст сообщения (даже если это подпись к картинке/видео)
    user_message = update.message.caption or update.message.text or ""
    user_name = update.effective_user.first_name or "Друг"
    user_username = update.effective_user.username or "без username"
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    lower_text = user_message.lower()

    logger.info(f"[{chat_type}] от @{user_username}: {user_message} (Медиа: {bool(update.message.effective_attachment)})")

    # Логика для групповых чатов
    if chat_type != "private":
        is_mentioned = "пантера" in lower_text
        is_reply_to_bot = (
            update.message.reply_to_message 
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        should_random_reply = random.random() < 0.25

        # Если к нам не обращались, это не реплай и мы не попали в 25% шанса вмешаться — игнорируем
        if not is_mentioned and not is_reply_to_bot and not should_random_reply:
            return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Обработка вложенных файлов (фото, видео, кружочки, голос)
    media_part = None
    try:
        file_id = None
        mime_type = None

        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            mime_type = "image/jpeg"
        elif update.message.voice:
            file_id = update.message.voice.file_id
            mime_type = "audio/ogg"
        elif update.message.video_note:
            file_id = update.message.video_note.file_id
            mime_type = "video/mp4"
        elif update.message.video:
            file_id = update.message.video.file_id
            mime_type = update.message.video.mime_type or "video/mp4"
        elif update.message.document:
            file_id = update.message.document.file_id
            mime_type = update.message.document.mime_type or "application/octet-stream"

        # Если есть файл, скачиваем его в память (ограничение Telegram - 20 МБ)
        if file_id:
            file_info = await context.bot.get_file(file_id)
            file_bytes = await file_info.download_as_bytearray()
            media_part = types.Part.from_bytes(data=bytes(file_bytes), mime_type=mime_type)

    except Exception as e:
        logger.error(f"Ошибка загрузки файла: {e}")
        await update.message.reply_text("Файл слишком большой или недоступен! Я могу читать файлы только до 20 МБ. 😿")
        return

    # Формируем контент для ИИ
    prompt_text = f"Ты — Пантера, остроумный ИИ-ассистент. Пользователь {user_name} (@{user_username}) пишет: '{user_message}'."
    if not user_message and media_part:
        prompt_text = f"Ты — Пантера. Пользователь {user_name} (@{user_username}) прислал файл без текста. Проанализируй и прокомментируй его."

    contents_list = [prompt_text]
    if media_part:
        contents_list.insert(0, media_part)  # Добавляем файл в начало запроса

    reply_text = None
    max_retries = 3
    retry_delay = 3

    # Умный цикл с обходом лимитов Google 429
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=contents_list,
            )
            reply_text = response.text
            break
        except Exception as e:
            error_str = str(e)
            logger.warning(f"Попытка {attempt + 1} неудачна. Ошибка: {error_str}")
            
            if attempt < max_retries - 1:
                # Если уперлись в квоту бесплатного тарифа (429), ждем дольше
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    logger.info("Уперлись в лимит 20 запросов/минуту. Ждем 25 секунд...")
                    await asyncio.sleep(25)
                else:
                    await asyncio.sleep(retry_delay)
            else:
                reply_text = "Я сейчас немножко перегрелась от количества сообщений (Google просит паузу). Напиши через минутку! 🐾"

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
    
    # Теперь бот слушает ВСЕ типы сообщений (фото, аудио, видео и т.д.), кроме команд вроде /start
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))

    logger.info("Бот 'Пантера' запущен. Теперь она всё видит и слышит!")
    application.run_polling()


if __name__ == "__main__":
    main()
