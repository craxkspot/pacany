import os
import logging
import random
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Получение переменных окружения
TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PORT = int(os.getenv("PORT", 10000))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

if not TOKEN:
    logger.error("❌ НЕ УКАЗАН TELEGRAM_TOKEN в переменных окружения!")
if not GROQ_API_KEY:
    logger.error("❌ НЕ УКАЗАН GROQ_API_KEY в переменных окружения!")

# Инициализация клиентов
client = Groq(api_key=GROQ_API_KEY)

# Нейтральный и адекватный системный промпт
NORMAL_SYSTEM_PROMPT = (
    "Ты — Пантера, полезный, умный и адекватный ИИ-ассистент в Telegram-чате. "
    "Отвечай вежливо, по делу, без фальшивого пафоса, сленга и ролевой игры. "
    "Общайся как нормальный современный собеседник."
)

async def is_addressed_to_bot(text: str, bot_username: str) -> bool:
    """Определяет, обращаются ли к боту в групповом чате."""
    if not text:
        return False
    
    if bot_username.lower() in text.lower() or "пантера" in text.lower():
        return True

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system", 
                    "content": "Определи, обращается ли пользователь в сообщении к боту по имени Пантера или к ИИ-ассистенту. Ответь строго одним словом: YES или NO."
                },
                {"role": "user", "content": text}
            ],
            temperature=0.1,
            max_tokens=5
        )
        answer = response.choices[0].message.content.strip().upper()
        return "YES" in answer
    except Exception as e:
        logger.error(f"Ошибка арбитра Groq: {e}")
        return True

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text
    user_name = update.message.from_user.first_name if update.message.from_user else "Пользователь"
    chat_type = update.message.chat.type
    bot_username = context.bot.username or "PanteraBot"

    logger.info(f"📥 ПОЛУЧЕНО СООБЩЕНИЕ [{chat_type}] от {user_name}: {user_text}")

    # Логика для групповых чатов
    if chat_type in ["group", "supergroup"]:
        addressed = await is_addressed_to_bot(user_text, bot_username)
        if not addressed and random.random() < 0.85:
            logger.info("🤫 Сообщение проигнорировано (фильтр группового чата).")
            return

    # Генерация ответа через Groq
    try:
        logger.info("🔄 Отправка запроса к Groq API...")
        completion = client.chat.completions.create(
            model="llama-3.1-70b-versatile",
            messages=[
                {"role": "system", "content": NORMAL_SYSTEM_PROMPT},
                {"role": "user", "content": f"{user_name}: {user_text}"}
            ],
            temperature=0.5,
            max_tokens=300
        )
        reply_text = completion.choices[0].message.content
        logger.info("📤 Ответ от Groq успешно получен.")
        
        await update.message.reply_text(reply_text)
    except Exception as e:
        logger.error(f"❌ Ошибка при обращении к Groq API: {e}")
        await update.message.reply_text("Произошла ошибка при обращении к нейросети. Попробуй позже.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"📥 Команда /start от {update.effective_user.id}")
    await update.message.reply_text("Привет! Я на связи. Чем могу помочь?")

def main():
    logger.info("Запуск бота...")
    
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{TOKEN}"
        logger.info(f"Установка webhook на URL: {webhook_url}")
        
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=webhook_url
        )
    else:
        logger.warning("⚠️ RENDER_EXTERNAL_URL не задан! Запуск в режиме Polling (локально).")
        application.run_polling()

if __name__ == "__main__":
    main()
