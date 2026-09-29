import os
import random
import logging
import io
import base64
import tempfile
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
TARGET_ROAST_CHANCE = 0.50  # 50% шанс ответить на сообщение
GLOBAL_ROAST_CHANCE = 0.05  # 5% шанс написать рандомную фразу Валеры

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
) if GROQ_API_KEY else None


# --- АВТОМАТИЧЕСКИЙ ПОДБОР РАБОЧИХ МОДЕЛЕЙ (БЕЗ ОШИБОК 404) ---
def get_active_models() -> tuple[str, str]:
    fallback_text = "llama-3.3-70b-versatile"
    fallback_vision = "llama-3.2-11b-vision-preview"

    if not groq_client:
        return fallback_text, fallback_vision

    try:
        models_data = groq_client.models.list().data
        available_ids = [m.id for m in models_data]
        logger.info(f"Доступные модели на Groq: {available_ids}")

        banned = ["whisper", "vision", "embed", "guard", "audio"]
        selected_text = None
        
        for m in available_ids:
            if any(b in m.lower() for b in banned):
                continue
            if "llama" in m.lower() or "qwen" in m.lower() or "mixtral" in m.lower():
                selected_text = m
                break
        
        if not selected_text and available_ids:
            selected_text = available_ids[0]

        selected_vision = None
        for m in available_ids:
            if "vision" in m.lower():
                selected_vision = m
                break
        
        if not selected_vision:
            selected_vision = fallback_vision

        return selected_text or fallback_text, selected_vision
    except Exception as e:
        logger.error(f"Ошибка при получении списка моделей: {e}")
        return fallback_text, fallback_vision


# Динамически определяем рабочие модели при старте
TEXT_MODEL, VISION_MODEL = get_active_models()

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


# --- ЖИВОЙ СЛЕНГОВЫЙ ТРОЛЛИНГ ТЕКСТА/ГС ---
async def generate_text_roast(user_text: str) -> str:
    if not groq_client:
        return "ну ты и высер конечно"

    system_prompt = (
        "ты свой тип из токсичной конфы. твоя задача — остроумно, едко и по факту опустить чела за его слова. "
        "никакого занудства, никакой морали, никаких 'сам дурак' и никаких 'я не валера'. ты котируешь валеру или сам валера, тебе похуй. "
        "ПРАВИЛА СТИЛЯ:\n"
        "1. пиши ВСЕГДА с маленькой буквы и без точек в конце.\n"
        "2. забивай хуй на знаки препинания, пиши как в телеге на расслабоне.\n"
        "3. будь язвительным, цепляйся за смысл слов, стеби за тупость или душноту, отвечай в кассу.\n"
        "4. длина: 1-2 коротких предложения, живой сленг, можно с матом, но без шизофрении.\n"
        "примеры:\n"
        "- бля чел хорош хуйню нести\n"
        "- ты когда это писал сам понял че высрал\n"
        "- сидит блять из себя умного строит а сам два слова связать не может\n"
        "- ладно уговорил ты клоун"
    )

    user_content = f"чел написал: \"{user_text}\"" if user_text else "чел прислал какую-то пустую хуйню"

    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            max_tokens=60,
            temperature=0.75  # Живой креатив, чтобы базарил как в конфе
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        if reply.endswith("."):
            reply = reply[:-1]
        return reply.lower() if reply else "ну и кринж ты выдал"
    except Exception as e:
        logger.error(f"Ошибка текстовой генерации: {e}")
        return "от твоего текста у меня апи упало"


# --- ЖИВОЙ СЛЕНГОВЫЙ ТРОЛЛИНГ КАРТИНОК И ВИДЕО ---
async def generate_image_roast(image_bytes: bytes, caption: str = "") -> str:
    if not groq_client:
        return "медиа параша какая-то"

    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        prompt_text = (
            "посмотри на это. обоссы максимально едко и по-человечески, как в конфе. "
            "пиши с маленькой буквы, без точек в конце, без дурацких метафор. 1 предложение."
        )
        if caption:
            prompt_text += f" подпись к каллу: \"{caption}\"."

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
            max_tokens=60,
            temperature=0.75
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        if reply.endswith("."):
            reply = reply[:-1]
        return reply.lower() if reply else "что за кал ты скинул"
    except Exception as e:
        logger.error(f"Ошибка Vision API: {e}")
        return "глаза кровят от твоей пикчи"


# --- КОМАНДА /ping ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        status_msg = (
            "🤖 **Валера-Бот (Конфный стиль) на связи!**\n\n"
            f"• Статус ИИ: ✅ Готов душить\n"
            f"• Текст: `{TEXT_MODEL}`\n"
            f"• Фото/Кружки: `{VISION_MODEL}`\n"
            f"• ГС: `{AUDIO_MODEL}`\n"
            f"• Шанс ответа: `{TARGET_ROAST_CHANCE*100}%`"
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
                messages=[{"role": "user", "content": "привет"}],
                max_tokens=5,
                temperature=0.1
            )
            test_response = res.choices[0].message.content.strip()
            if test_response:
                text_status = "✅ РАБОТАЕТ"
                is_working = True
            else:
                test_response = "Пустой ответ"
        except Exception as e:
            text_status = "❌ ОШИБКА API"
            test_response = str(e)[:45]

    table_log = f"""
┌────────────────────────────────────────────────────────────────────────┐
│                        ОТЧЕТ О ЗАПУСКЕ БОТА                            │
├──────────────────────┬─────────────────────────────────────────────────┤
│ TELEGRAM_TOKEN       │ {tg_ok:<47} │
│ GROQ_API_KEY         │ {key_ok:<47} │
│ Текстовая модель     │ {TEXT_MODEL:<47} │
│ Зрячая модель        │ {VISION_MODEL:<47} │
│ Статус ИИ            │ {text_status:<47} │
│ Отклик модели        │ {test_response:<47} │
└──────────────────────┴─────────────────────────────────────────────────┘
"""
    logger.info(table_log)
    return is_working and bool(TELEGRAM_TOKEN)


# --- ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    if user.is_bot:
        return

    # 1. ГЛОБАЛЬНЫЙ ШАНС (написать "я валера" на ЛЮБОЕ сообщение в чате)
    if random.random() < GLOBAL_ROAST_CHANCE:
        valera_phrase = random.choice(VALERA_IMPERSONATIONS)
        logger.info(f"🎲 Глобальный шанс сработал! Отправляем: '{valera_phrase}'")
        await update.message.reply_text(valera_phrase)
        return

    # 2. ТЕСТОВЫЙ ШАНС ДЛЯ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ
    if random.random() < TARGET_ROAST_CHANCE:
        logger.info(f"🎯 Токсичный троллинг сработал на пользователя @{user.username or user.first_name}!")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        roast_text = ""

        # А) Если прислали фото
        if update.message.photo:
            try:
                photo_file = await update.message.photo[-1].get_file()
                photo_bytes = await photo_file.download_as_bytearray()
                caption = update.message.caption or ""
                logger.info("🖼 Скачиваем картинку для разноса...")
                roast_text = await generate_image_roast(bytes(photo_bytes), caption)
            except Exception as e:
                logger.error(f"Не удалось обработать фото: {e}")
                roast_text = "твоя пикча даже не грузится"

        # Б) Если прислали видео-кружок (video_note)
        elif update.message.video_note:
            try:
                video_file = await update.message.video_note.get_file()
                logger.info("🎥 Скачиваем видео-кружок для разноса...")
                
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temp_video:
                    temp_video_path = temp_video.name
                
                await video_file.download_to_drive(temp_video_path)
                
                with open(temp_video_path, "rb") as f:
                    video_bytes = f.read()
                    
                os.unlink(temp_video_path)
                
                roast_text = await generate_image_roast(video_bytes, "видео-кружок")
            except Exception as e:
                logger.error(f"Не удалось обработать видео-кружок: {e}")
                roast_text = "твой кружок параша полная"

        # В) Если прислали голосовое сообщение
        elif update.message.voice:
            try:
                voice_file = await update.message.voice.get_file()
                voice_bytes = await voice_file.download_as_bytearray()
                user_text = await transcribe_voice(bytes(voice_bytes))
                logger.info(f"🎙 Расшифрованная голосовуха: '{user_text}'")
                roast_text = await generate_text_roast(user_text)
            except Exception as e:
                logger.error(f"Не удалось скачать или расшифровать ГС: {e}")
                roast_text = "ты даже голосовуху нормально записать не можешь"

        # Г) Если написали обычный текст или прислали подпись
        elif update.message.text or update.message.caption:
            user_text = update.message.text or update.message.caption or ""
            roast_text = await generate_text_roast(user_text)

        if roast_text:
            await update.message.reply_text(roast_text)


def main():
    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    ready = print_startup_status_table()
    if not ready:
        logger.warning("⚠️ Проверьте параметры подключения в таблице выше.")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    
    logger.info("🤖 Валера-бот с вайбом конфы запущен...")
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
