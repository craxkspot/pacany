import os
import random
import logging
import io
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

TARGET_USERNAME = "soult0ken"
AUDIO_MODEL = "whisper-large-v3-turbo"

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
) if GROQ_API_KEY else None


# --- АВТОМАТИЧЕСКИЙ ПОДБОР РАБОЧЕЙ ТЕКСТОВОЙ МОДЕЛИ ---
def get_active_text_model() -> str:
    if not groq_client:
        return "llama-3.1-8b-instant"
    try:
        models_data = groq_client.models.list().data
        available_ids = [m.id for m in models_data]
        
        # Приоритетный список стабильных моделей
        priority_models = [
            "llama-3.1-8b-instant",
            "llama-3.3-70b-versatile",
            "llama3-8b-8192",
            "gemma2-9b-it"
        ]
        
        for model in priority_models:
            if model in available_ids:
                return model
                
        # Если ничего из списка нет, берем первую текстовую модель
        text_models = [m_id for m_id in available_ids if "whisper" not in m_id and "vision" not in m_id]
        if text_models:
            return text_models[0]
            
    except Exception as e:
        logger.error(f"Не удалось автоопределить модель: {e}")
    
    return "llama-3.1-8b-instant"


TEXT_MODEL = get_active_text_model()

VALERA_IMPERSONATIONS = [
    "я валера",
    "я валера и я одобряю этот бред",
    "всем привет, я валера",
    "как валера заявляю: это слишком сложно",
    "я валера, сейчас запишу голосовуху на 3 минуты",
    "я валера",
    "ребята, я валера, кто меня звал?",
    "я валера и я снова ничего не понял"
]


# --- ВЕБ-СЕРВЕР ДЛЯ ХЕЛСЧЕКОВ ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Valera Bot is operational!")

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# --- РАСШИФРОВКА ГС ЧЕРЕЗ WHISPER ---
async def transcribe_voice(voice_file_bytes: bytes) -> str:
    if not groq_client:
        return ""
    try:
        audio_file = ("voice.ogg", io.BytesIO(voice_file_bytes), "audio/ogg")
        transcription = groq_client.audio.transcriptions.create(
            file=audio_file,
            model=AUDIO_MODEL,
            response_format="text"
        )
        return str(transcription).strip()
    except Exception as e:
        logger.error(f"Ошибка расшифровки ГС: {e}")
        return ""


# --- ГЕНЕРАЦИЯ ПОДКОЛА ---
async def generate_dynamic_roast(valera_text: str) -> str:
    if not groq_client:
        return "Валера, ну что за бред ты опять выдал..."

    system_prompt = (
        "Ты — саркастичный и язвительный участник Telegram-чата. "
        "Твоя цель — коротко высмеять или подколоть сообщение пользователя по имени Валера "
        "(в том числе расшифрованное из его голосового сообщения).\n"
        "ПРАВИЛА:\n"
        "1. Отвечай коротко (1–2 предложения).\n"
        "2. Пиши живым разговорным языком, с иронией или сарказмом.\n"
        "3. Высмеивай именно смысл того, что он сказал/написал.\n"
        "4. НЕ используй Markdown, звездочки (*) и форматирование."
    )

    user_content = f"Валера сказал/написал: \"{valera_text}\"" if valera_text else "Валера прислал непонятное голосовое."

    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            max_tokens=100,
            temperature=0.8
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        return reply if reply else "Валера, перечитай сам, что ты выдал..."
    except Exception as e:
        logger.error(f"Ошибка в процессе генерации: {e}")
        return "Валера, твои мысли снова сломали нейросеть."


# --- ТАБЛИЦА СТАТУСА ---
def print_startup_status_table() -> bool:
    tg_ok = "✅ ОК" if TELEGRAM_TOKEN else "❌ ОТСУТСТВУЕТ"
    key_ok = "✅ ОК" if GROQ_API_KEY else "❌ ОТСУТСТВУЕТ"
    
    text_status = "❌ ОШИБКА"
    test_response = "Нет ключа API"
    is_working = False

    if groq_client:
        try:
            res = groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=[{"role": "user", "content": "Скажи ОК"}],
                max_tokens=5,
                temperature=0.1
            )
            test_response = res.choices[0].message.content.strip()
            text_status = "✅ РАБОТАЕТ"
            is_working = True
        except Exception as e:
            text_status = "❌ ОШИБКА API"
            test_response = str(e)

    table_log = f"""
┌────────────────────────────────────────────────────────────────────────┐
│                        ОТЧЕТ О ЗАПУСКЕ БОТА                            │
├──────────────────────┬─────────────────────────────────────────────────┤
│ TELEGRAM_TOKEN       │ {tg_ok:<47} │
│ GROQ_API_KEY         │ {key_ok:<47} │
│ Жертва (Target)      │ @{TARGET_USERNAME:<46} │
│ Авто-выбранная модель│ {TEXT_MODEL:<47} │
│ Модель Whisper (ГС)  │ {AUDIO_MODEL:<47} │
│ Статус ИИ            │ {text_status:<47} │
│ Тестовый отклик      │ {test_response[:45]:<47} │
└──────────────────────┴─────────────────────────────────────────────────┘
"""
    logger.info(table_log)
    return is_working and bool(TELEGRAM_TOKEN)


# --- ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    username = user.username.lower() if user.username else ""

    # 1. 5% шанс написать "я валера" на ЛЮБОЕ сообщение
    if random.random() < 0.05:
        valera_phrase = random.choice(VALERA_IMPERSONATIONS)
        logger.info(f"🎲 5% глобальный шанс сработал! Отправляем: '{valera_phrase}'")
        await update.message.reply_text(valera_phrase)
        return

    # 2. 5% шанс подколоть сообщения/ГС Валеры (@soult0ken)
    if username == TARGET_USERNAME:
        if random.random() < 0.05:
            logger.info("🎯 5% шанс сработал на Валеру!")
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

            valera_text = ""

            if update.message.text or update.message.caption:
                valera_text = update.message.text or update.message.caption or ""

            elif update.message.voice:
                try:
                    voice_file = await update.message.voice.get_file()
                    voice_bytes = await voice_file.download_as_bytearray()
                    valera_text = await transcribe_voice(bytes(voice_bytes))
                    logger.info(f"🎙 Расшифрованное ГС Валеры: '{valera_text}'")
                except Exception as e:
                    logger.error(f"Не удалось скачать или расшифровать ГС: {e}")

            roast_text = await generate_dynamic_roast(valera_text)
            await update.message.reply_text(roast_text)


def main():
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    ready = print_startup_status_table()
    if not ready:
        logger.warning("⚠️ Проверьте параметры подключения в таблице выше.")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    
    logger.info("🤖 Бот запущен и слушает чат...")
    
    # drop_pending_updates=True устраняет 409 Conflict при перезапусках
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
