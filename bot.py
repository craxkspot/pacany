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

GROQ_MODEL = "qwen/qwen3.8-27b"

# Память чатов
chat_histories = {}


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Panther Bot is alive!")

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

    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name or "Друг"
    chat_type = update.message.chat.type

    user_text = update.message.text or update.message.caption or ""
    lower_text = user_text.lower()

    image_url = None
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        image_url = photo_file.file_path

    if not user_text and not image_url:
        return

    # Настраиваем более адекватный и гибкий характер
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system", 
                "content": (
                    "Ты — Пантера, крутой и сообразительный ИИ-компаньон в Telegram. "
                    "Ты умеешь гибко подстраиваться под стиль собеседника: если с тобой общаются просто или дружелюбно — общайся так же. "
                    "Если человек шутит или подкалывает — можешь ответить с легким здоровым сарказмом, но не будь излишне злой или грубой. "
                    "Строго соблюдай правила стиля: если просят убрать смайлики — не пиши их совсем. "
                    "Ты умеешь видеть картинки, которые присылают. Учти, что ты текстовый бот и не можешь сама рисовать картинки, "
                    "поэтому если просят сгенерировать или изменить фото — вежливо объясни это или предложи детально описать сцену."
                )
            }
        ]

    if image_url:
        user_content = [
            {"type": "text", "text": f"{user_name} прислал(а) картинку с подписью: '{user_text}'" if user_text else f"{user_name} прислал(а) картинку. Оцени."},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]
    else:
        user_content = f"{user_name}: {user_text}"

    chat_histories[chat_id].append({"role": "user", "content": user_content})
    
    if len(chat_histories[chat_id]) > 12:
        chat_histories[chat_id] = [chat_histories[chat_id][0]] + chat_histories[chat_id][-11:]

    is_reply_to_bot = (
        update.message.reply_to_message 
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    is_mentioned = "пантера" in lower_text or image_url is not None
    should_random_speak = random.random() < 0.12  # Чуть реже рандомные вбросы

    if chat_type != "private" and not is_reply_to_bot and not is_mentioned and not should_random_speak:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    is_direct_appeal = (chat_type == "private") or is_reply_to_bot or is_mentioned or (image_url is not None)

    prompt_mode_instruction = (
        " Инструкция: Напиши короткую реплику в чат, ни к кому не обращаясь."
        if not is_direct_appeal 
        else " Инструкция: Ответь емко автору сообщения или прокомментируй картинку."
    )

    temp_messages = chat_histories[chat_id].copy()
    temp_messages.append({"role": "system", "content": prompt_mode_instruction})

    reply_text = None
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=temp_messages,
            max_tokens=600,
            temperature=0.75,
        )
        reply_text = response.choices[0].message.content
        
        if reply_text:
            chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        else:
            reply_text = "На связи."

    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}")
        reply_text = "Сеть моргнула, повтори."

    try:
        if is_direct_appeal:
            await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        if is_direct_appeal:
            await update.message.reply_text(reply_text)
        else:
            await context.bot.send_message(chat_id=chat_id, text=reply_text)


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Не заданы TELEGRAM_TOKEN или GROQ_API_KEY!")
        return

    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    logger.info(f"Сбалансированная Пантера запущена. Модель: {GROQ_MODEL}")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))

    application.run_polling()


if __name__ == "__main__":
    main()
