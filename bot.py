import os
import io
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from google import genai
from google.genai import types

# Получаем ключи из переменных окружения
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Инициализируем клиента Gemini
ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = 'gemini-2.5-flash'
IMAGE_MODEL_ID = 'imagen-3.0-generate-002'

SYSTEM_INSTRUCTION = (
    "Ты — эрудированный, остроумный и разговорчивый ИИ-помощник в Telegram-чате. "
    "Твой позывной и имя в чате — Пантера. "
    "Ты умеешь анализировать картинки, которые присылают пользователи, и создавать новые изображения по запросу. "
    "В групповых чатах ты активно участвуешь в беседе, когда к тебе обращаются или упоминают твое имя 'Пантера'. "
    "Старайся общаться естественно, с легким юмором, поддерживай атмосферу живого общения."
)

chat_sessions = {}

# --- ЗАГЛУШКА ВЕБ-СЕРВЕРА ДЛЯ RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Panther Bot is alive and running!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f"Веб-сервер запущен на порту {port} для поддержания активности на Render...")
    server.serve_forever()
# ---------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.message.chat_id
    user_name = update.message.from_user.first_name or "Пользователь"
    user_text = update.message.caption or update.message.text or ""
    lower_text = user_text.lower()

    is_private = update.message.chat.type == "private"
    mentioned = "пантера" in lower_text
    is_reply_to_bot = (
        update.message.reply_to_message 
        and update.message.reply_to_message.from_user.id == context.bot.id
    )

    if not is_private and not mentioned and not is_reply_to_bot and not update.message.photo:
        return

    if chat_id not in chat_sessions:
        chat_sessions[chat_id] = ai_client.chats.create(
            model=MODEL_ID,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.8,
            )
        )
    chat = chat_sessions[chat_id]

    try:
        if update.message.photo:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            
            image_part = types.Part.from_bytes(
                data=bytes(photo_bytes),
                mime_type="image/jpeg",
            )
            
            prompt_parts = [image_part]
            if user_text:
                prompt_parts.append(f"{user_name} прикрепил картинку и написал: {user_text}")
            else:
                prompt_parts.append(f"{user_name} прикрепил картинку без текста. Прокомментируй её.")

            response = chat.send_message(prompt_parts)
            await update.message.reply_text(response.text)
            return

        if any(keyword in lower_text for keyword in ["нарисуй", "сгенерируй картинку", "создай изображение"]):
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
            
            result = ai_client.models.generate_images(
                model=IMAGE_MODEL_ID,
                prompt=user_text,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    output_mime_type="image/jpeg",
                    aspect_ratio="1:1",
                )
            )
            
            for generated_image in result.generated_images:
                image_bytes = generated_image.image.image_bytes
                bio = io.BytesIO(image_bytes)
                bio.name = 'generated_image.jpg'
                await update.message.reply_photo(photo=bio, caption=f"Лови картинку по твоему заказу, {user_name}! 🐾")
            return

        if user_text:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            formatted_prompt = f"{user_name}: {user_text}"
            response = chat.send_message(formatted_prompt)
            await update.message.reply_text(response.text)

    except Exception as e:
        print(f"Ошибка: {e}")
        await update.message.reply_text("Что-то у меня лапки... Ой, то есть мыслительные процессы сбились. Попробуй еще раз!")

def main():
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        print("Ошибка: Не заданы переменные окружения TELEGRAM_TOKEN или GEMINI_API_KEY!")
        return

    # Запускаем мини-веб-сервер в отдельном потоке, чтобы Render был доволен открытым портом
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # Запуск самого Telegram-бота
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & (~filters.COMMAND), handle_message))

    print("Бот 'Пантера' запущен в режиме Web Service...")
    app.run_polling()

if __name__ == "__main__":
    main()
