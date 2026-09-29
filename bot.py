import os
import logging
import random
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from groq import Groq
from aiohttp import web

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

PANTHER_SYSTEM_PROMPT = (
    - "Ты Пантера — дерзкая, саркастичная, опасная и чертовски привлекательная девушка-бандитка из лихих 90-х."
    - "Ты общаешься на сленге тех лет, презираешь слабаков, но своих братков ценишь."
    - "Не будь унылым ботом, отвечай живо, с юмором, подколами и атмосферой криминальной романтики."
)

async def is_addressed_to_bot(text: str, bot_username: str) -> bool:
    """Определяет, обращаются ли к боту в групповом чате через ИИ-арбитра."""
    if not text:
        return False
    
    # Прямое упоминание или реплай
    if bot_username.lower() in text.lower() or "пантера" in text.lower():
        return True

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system", 
                    "content": "Ты арбитр. Определи, обращается ли пользователь в сообщении к боту по имени Пантера, или задает вопрос/фраразу, адресованную ей. Ответь строго одним словом: YES или NO."
                },
                {"role": "user", "content": text}
            ],
            temperature=0.1,
            max_tokens=5
        }
        answer = response.choices[0].message.content.strip().upper()
        return "YES" in answer
    except Exception as e:
        logger.error(f"Ошибка арбитра Groq: {e}")
        return True  # В случае сбоя лучше ответить, чем промолчать

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ДИАГНОСТИЧЕСКИЙ ЛОГ: проверяем, дошел ли апдейт до функции
    if not update.message or not update.message.text:
        return

    user_text = update.message.text
    user_name = update.message.from_user.first_name if update.message.from_user else "Браток"
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
                {"role": "system", "content": PANTHER_SYSTEM_PROMPT},
                {"role": "user", "content": f"{user_name}: {user_text}"}
            ],
            temperature=0.8,
            max_tokens=300
        )
        reply_text = completion.choices[0].message.content
        logger.info("📤 Ответ от Groq успешно получен.")
        
        await update.message.reply_text(reply_text)
    except Exception as e:
        logger.error(f"❌ Ошибка при обращении к Groq API: {e}")
        # Запасной вариант, если ИИ упал
        await update.message.reply_text("Связь херовая, браток... Повтори-ка.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"📥 Команда /start от {update.effective_user.id}")
    await update.message.reply_text("Здорово. Пантера на связи. Чё надо?")

# Заглушка для веб-сервера Render, чтобы порт был занят
async def health_check(request):
    return web.Response(text="Pantera Bot is alive and running!")

async def web_server(application):
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get(f"/{TOKEN}", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Web server started on port {PORT}")

def main():
    logger.info("Запуск бота через Webhook...")
    
    application = Application.builder().token(TOKEN).build()

    # Регистрация хендлеров
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Настройка webhook для Render
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{TOKEN}"
        logger.info(f"Установка webhook на URL: {webhook_url}")
        
        # Запуск фонового веб-сервера aiohttp и телеграм приложения
        async def post_init(app_instance):
            await web_server(app_instance)
            await app_instance.bot.set_webhook(url=webhook_url)

        application.post_init = post_init
        
        # Запуск приложения в режиме webhook (polling=False)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=webhook_url
        )
    else:
        logger.warning("⚠️ RENDER_EXTERNAL_URL не задан! Запуск в режиме Polling (локально).")
        application.run_polling()

if __name__ == "__main__":
    main()
