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

TARGET_USERNAME = "soult0ken" # Вернешь потом, когда закончим тест
AUDIO_MODEL = "whisper-large-v3-turbo"

# --- НАСТРОЙКИ ВЕРОЯТНОСТЕЙ ---
TARGET_ROAST_CHANCE = 0.50  # 50% шанс на время теста
GLOBAL_ROAST_CHANCE = 0.05  # 5% шанс написать "я валера" остальным

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


# --- МАКСИМАЛЬНО ТОКСИЧНЫЙ ТРОЛЛИНГ ТЕКСТА/ГС ---
async def generate_text_roast(user_text: str) -> str:
    if not groq_client:
        return "Ну что за хуйню ты опять высрал."

    system_prompt = (
        "Ты — максимально токсичный, отбитый и злой тип из токсичного чата. Никакой вежливости, "
        "никаких нравоучений и морали. Твоя задача — жестко, грязно и с матом опустить оппонента за любое слово.\n"
        "ПРАВИЛА:\n"
        "1. Не жалей мата, используй жесткий уличный сленг и реальный интернет-треш.\n"
        "2. Если тебе пишут коротко или оскорбляют — отвечай в десять раз агрессивнее, унижай за тупость.\n"
        "3. Никаких советов в стиле 'пиши нормально' — только лютый прожар и оскорбления.\n"
        "4. Длина: 1-3 предложения. Без звездочек, без Markdown, заканчивай точкой."
    )

    user_content = f"Собеседник написал: \"{user_text}\"" if user_text else "Собеседник прислал какую-то хуйню без текста."

    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            max_tokens=120,
            temperature=0.85
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        return reply if reply else "Блять, даже нейросеть твой высер проглотить не смогла."
    except Exception as e:
        logger.error(f"Ошибка текстовой генерации: {e}")
        return "Твои мысли сломали бота нахуй."


# --- МАКСИМАЛЬНО ТОКСИЧНЫЙ ТРОЛЛИНГ КАРТИНОК И ВИДЕО ---
async def generate_image_roast(image_bytes: bytes, caption: str = "") -> str:
    if not groq_client:
        return "Медиа твоё не прогрузилось, но уверен — там полная параша."

    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        prompt_text = (
            "Посмотри на это говно, которое тебе скинули. Обоссы это максимально жестко и с матом, "
            "без всяких соплей и морали. Унижай автора за его вкус и за то, что он это прислал."
        )
        if caption:
            prompt_text += f" Подпись к этой хуйне: \"{caption}\"."

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
            temperature=0.85
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
            "🤖 **Валера-Бот (Лютый режим) на связи!**\n\n"
            f"• Статус ИИ: ✅ Готов унижать\n"
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
│ Режим теста          │ ВСЕ ПОЛЬЗОВАТЕЛИ (ЛЮТЫЙ ТРОЛЛИНГ)               │
│ Текстовая модель     │ {TEXT_MODEL:<47} │
│ Зрячая модель (Фото) │ {VISION_MODEL:<47} │
│ Модель Whisper (ГС)  │ {AUDIO_MODEL:<47} │
│ Статус ИИ            │ {text_status:<47} │
│ Тестовый отклик      │ {test_response[:45]:<47} │
└──────────────────────┴─────────────────────────────────────────────────┘
"""
    logger.info(table_log)
    return is_working and bool(TELEGRAM_TOKEN)


# --- ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ (ТЕСТОВЫЙ РЕЖИМ ДЛЯ ВСЕХ) ---
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
    
    logger.info("🤖 Лютый Валера-бот запущен...")
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
