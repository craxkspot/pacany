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


# --- УЛЬТРА-ЖЕСТКИЙ ТРОЛЛИНГ ТЕКСТА/ГС (БЕЗ ШИЗЫ, t=0.2) ---
async def generate_text_roast(user_text: str) -> str:
    if not groq_client:
        return "Ну что за хуйню ты опять высрал."

    system_prompt = (
        "Ты — токсичный гопник из чата. Твоя единственная цель — оскорбить человека и унизить за его слова. "
        "Говори только короткими, понятными матерными фразами. Никакой философии, никакой шизофрении, никаких умных слов. "
        "Примеры ответов:\n"
        "- Ты чё, дурак блять?\n"
        "- Иди уроки учи, клоун.\n"
        "- Сказал хуйню и рад, еблан.\n"
        "- Ты бы рот закрыл, от тебя говном несет.\n"
        "Отвечай строго 1 предложением, с матом, без звездочек и без Markdown."
    )

    user_content = f"Чел написал: \"{user_text}\"" if user_text else "Чел прислал пустую парашу."

    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            max_tokens=40,
            temperature=0.2  # Низкая температура убирает бред и шизофрению
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        return reply if reply else "Блять, даже ответить нечего на этот бред."
    except Exception as e:
        logger.error(f"Ошибка текстовой генерации: {e}")
        return "Твои мысли сломали бота нахуй."


# --- УЛЬТРА-ЖЕСТКИЙ ТРОЛЛИНГ КАРТИНОК И ВИДЕО ---
async def generate_image_roast(image_bytes: bytes, caption: str = "") -> str:
    if not groq_client:
        return "Медиа твоё не прогрузилось, но уверен — там полная параша."

    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        prompt_text = (
            "Посмотри на эту картинку/видео. Обоссы это коротко и с матом, как в пацанском чате. "
            "Никакой философии, 1 предложение, без звездочек."
        )
        if caption:
            prompt_text += f" Подпись: \"{caption}\"."

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
            max_tokens=40,
            temperature=0.2
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        return reply if reply else "Ну и кал ты скинул, пиздец."
    except Exception as e:
        logger.error(f"Ошибка Vision API: {e}")
        return "У нейросети глаза кровят от твоей медиа-хуйни."


# --- КОМАНДА /ping ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        status_msg = (
            "🤖 **Валера-Бот (Автоподбор моделей) на связи!**\n\n"
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
                messages=[{"role": "user", "content": "OK"}],
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
                roast_text = "Твоя пикча даже не грузится, такая же бесполезная."

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
                
                roast_text = await generate_image_roast(video_bytes, "Видео-кружок")
            except Exception as e:
                logger.error(f"Не удалось обработать видео-кружок: {e}")
                roast_text = "Твой ебучий кружок даже нейросеть открывать отказалась."

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
                roast_text = "Ты даже голосовуху нормально записать не в состоянии, лузер."

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
    
    logger.info("🤖 Умный Валера-бот запущен...")
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
