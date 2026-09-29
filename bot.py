import os
import random
import logging
import io
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
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

# --- НАСТРОЙКИ ВЕРОЯТНОСТЕЙ ---
TARGET_ROAST_CHANCE = 0.30  # 30% шанс подколоть Валеру (0.30 = 30%, 1.0 = 100%)
GLOBAL_ROAST_CHANCE = 0.05  # 5% шанс написать "я валера" на сообщения других людей

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
) if GROQ_API_KEY else None


# --- НАДЕЖНЫЙ ПОДБОР РАБОЧИХ МОДЕЛЕЙ GROQ ---
def get_active_models() -> tuple[str, str]:
    fallback_text = "llama-3.3-70b-versatile"
    fallback_vision = "llama-3.2-11b-vision-preview"

    if not groq_client:
        return fallback_text, fallback_vision

    try:
        models_data = groq_client.models.list().data
        available_ids = [m.id for m in models_data]

        priority_text = [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "qwen-2.5-32b",
            "qwen-2.5-72b",
            "llama3-8b-8192",
            "gemma2-9b-it"
        ]

        banned_keywords = ["openai", "gpt-oss", "whisper", "vision", "guard", "embed", "safetensors"]

        selected_text = None
        
        # 1. Проверяем приоритетные разговорные модели
        for model in priority_text:
            if model in available_ids:
                try:
                    res = groq_client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": "OK"}],
                        max_tokens=5,
                        temperature=0.1
                    )
                    if res.choices[0].message.content.strip():
                        selected_text = model
                        break
                except Exception:
                    continue

        # 2. Поиск любой работающей альтернативы
        if not selected_text:
            for m in available_ids:
                if not any(bad in m.lower() for bad in banned_keywords):
                    try:
                        res = groq_client.chat.completions.create(
                            model=m,
                            messages=[{"role": "user", "content": "OK"}],
                            max_tokens=5,
                            temperature=0.1
                        )
                        if res.choices[0].message.content.strip():
                            selected_text = m
                            break
                    except Exception:
                        continue

        if not selected_text:
            selected_text = fallback_text

        # 3. Подбор Vision-модели
        priority_vision = [
            "llama-3.2-11b-vision-preview",
            "llama-3.2-90b-vision-preview"
        ]

        selected_vision = None
        for model in priority_vision:
            if model in available_ids:
                selected_vision = model
                break

        if not selected_vision:
            selected_vision = fallback_vision

        return selected_text, selected_vision

    except Exception as e:
        logger.error(f"Не удалось получить список моделей через API Groq: {e}")
        return fallback_text, fallback_vision


TEXT_MODEL, VISION_MODEL = get_active_models()

# Фразы для глобального шанса в чате
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
        self.wfile.write(b"Valera Bot is live!")

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


# --- ГЕНЕРАЦИЯ ПОДКОЛА ДЛЯ ТЕКСТА/ГС ---
async def generate_text_roast(valera_text: str) -> str:
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

    user_content = f"Валера сказал/написал: \"{valera_text}\"" if valera_text else "Валера прислал что-то непонятное."

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
        logger.error(f"Ошибка текстовой генерации: {e}")
        return "Валера, твои мысли снова сломали нейросеть."


# --- АНАЛИЗ КАРТИНКИ И ПОДКОЛ ЧЕРЕЗ VISION API ---
async def generate_image_roast(image_bytes: bytes, caption: str = "") -> str:
    if not groq_client:
        return "Валера, у меня картинка не загрузилась, но уверен — там ерунда."

    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        prompt_text = (
            "Посмотри на это изображение, которое прислал пользователь Валера в чат. "
            "Коротко и с сарказмом подколи его за то, что нарисовано или изображено на этой картинке. "
            "Отвечай коротко (1-2 предложения), живым разговорным языком. Без Markdown и звездочек (*)."
        )
        if caption:
            prompt_text += f" Подпись к картинке от Валеры: \"{caption}\"."

        response = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=120,
            temperature=0.8
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        return reply if reply else "Валера, ну и картинку ты нашёл..."
    except Exception as e:
        logger.error(f"Ошибка Vision API: {e}")
        return "Валера, даже нейросеть с глазами в шоке от твоей картинки."


# --- ЕДИНСТВЕННАЯ КОМАНДА /ping ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        status_msg = (
            "🤖 **Валера-Бот на связи!**\n\n"
            f"• Статус ИИ: ✅ Работает\n"
            f"• Текст: `{TEXT_MODEL}`\n"
            f"• Фото: `{VISION_MODEL}`\n"
            f"• ГС: `{AUDIO_MODEL}`\n"
            f"• Жертва: @{TARGET_USERNAME}"
        )
        await update.message.reply_text(status_msg, parse_mode="Markdown")


# --- ТАБЛИЦА СТАТУСА ПРИ СТАРТЕ ---
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
                messages=[{"role": "user", "content": "Напиши ровно один символ: OK"}],
                max_tokens=5,
                temperature=0.1
            )
            test_response = res.choices[0].message.content.strip()
            if test_response:
                text_status = "✅ РАБОТАЕТ"
                is_working = True
            else:
                test_response = "Пустой ответ от модели"
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
│ Текстовая модель     │ {TEXT_MODEL:<47} │
│ Зрячая модель (Фото) │ {VISION_MODEL:<47} │
│ Модель Whisper (ГС)  │ {AUDIO_MODEL:<47} │
│ Статус ИИ            │ {text_status:<47} │
│ Тестовый отклик      │ {test_response[:45]:<47} │
└──────────────────────┴─────────────────────────────────────────────────┘
"""
    logger.info(table_log)
    return is_working and bool(TELEGRAM_TOKEN)


# --- ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    username = user.username.lower() if user.username else ""

    # 1. ГЛОБАЛЬНЫЙ ШАНС (написать "я валера" на ЛЮБОЕ сообщение в чате)
    if random.random() < GLOBAL_ROAST_CHANCE:
        valera_phrase = random.choice(VALERA_IMPERSONATIONS)
        logger.info(f"🎲 Глобальный шанс сработал! Отправляем: '{valera_phrase}'")
        await update.message.reply_text(valera_phrase)
        return

    # 2. ПЕРСОНАЛЬНЫЙ ШАНС: подколоть Валеру (@soult0ken)
    if username == TARGET_USERNAME:
        if random.random() < TARGET_ROAST_CHANCE:
            logger.info(f"🎯 Шанс сработал на Валеру ({TARGET_ROAST_CHANCE*100}%)!")
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

            roast_text = ""

            # А) Если Валера прислал фото
            if update.message.photo:
                try:
                    photo_file = await update.message.photo[-1].get_file()
                    photo_bytes = await photo_file.download_as_bytearray()
                    caption = update.message.caption or ""
                    logger.info("🖼 Скачиваем картинку Валеры для анализа...")
                    roast_text = await generate_image_roast(bytes(photo_bytes), caption)
                except Exception as e:
                    logger.error(f"Не удалось обработать фото Валеры: {e}")
                    roast_text = "Валера, твоя картинка не грузится, но мем наверняка баян."

            # Б) Если Валера прислал голосовое сообщение
            elif update.message.voice:
                try:
                    voice_file = await update.message.voice.get_file()
                    voice_bytes = await voice_file.download_as_bytearray()
                    valera_text = await transcribe_voice(bytes(voice_bytes))
                    logger.info(f"🎙 Расшифрованное ГС Валеры: '{valera_text}'")
                    roast_text = await generate_text_roast(valera_text)
                except Exception as e:
                    logger.error(f"Не удалось скачать или расшифровать ГС: {e}")
                    roast_text = "Валера, у тебя даже голосовухи нормально не записываются."

            # В) Если Валера написал обычный текст или прислал подпись
            elif update.message.text or update.message.caption:
                valera_text = update.message.text or update.message.caption or ""
                roast_text = await generate_text_roast(valera_text)

            if roast_text:
                await update.message.reply_text(roast_text)


def main():
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    ready = print_startup_status_table()
    if not ready:
        logger.warning("⚠️ Проверьте параметры подключения в таблице выше.")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Команда для проверки живой ли бот
    application.add_handler(CommandHandler("ping", ping_command))
    
    # Обработчик обычных сообщений
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    
    logger.info("🤖 Бот запущен, слушает чат...")
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
