import os
import random
import logging
import io
import base64
import tempfile
from collections import defaultdict, deque
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

# Твой юзер как главный авторитет для бота
MASTER_USERNAME = "muctep_kpunep"
AUDIO_MODEL = "whisper-large-v3-turbo"

# --- НАСТРОЙКИ ВЕРОЯТНОСТЕЙ ---
TARGET_ROAST_CHANCE = 0.50  # 50% шанс ответить на сообщение обычным челам
GLOBAL_ROAST_CHANCE = 0.05  # 5% шанс написать рандомную фразу Валеры

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
) if GROQ_API_KEY else None

# --- КОНТЕКСТ ЧАТОВ (ИСТОРИЯ СООБЩЕНИЙ) ---
chat_histories = defaultdict(lambda: deque(maxlen=12))


# --- АВТОМАТИЧЕСКИЙ ПОДБОР РАБОЧИХ МОДЕЛЕЙ ---
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


TEXT_MODEL, VISION_MODEL = get_active_models()

VALERA_IMPERSONATIONS = [
    "я валера",
    "я валера и я одобряю этот бред",
    "всем привет, я валера",
    "как валера заявляю: это слишком сложно",
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


# --- УМНЫЙ ТРОЛЛИНГ ТЕКСТА С УЧЕТОМ ХОЗЯИНА ---
async def generate_text_roast(chat_id: int, sender_username: str, user_text: str) -> str:
    if not groq_client:
        return "ну ты и высер конечно"

    is_master = (sender_username == MASTER_USERNAME)

    if is_master:
        # Если пишет хозяин — бот адекватный, уважительный, понимающий и смышленый
        system_prompt = (
            f"Ты — Валера, умный и смышленый тип из конфы. Твой создатель и главный авторитет — пользователь @{MASTER_USERNAME}. "
            "ПРАВИЛА ДЛЯ ОБЩЕНИЯ С СОЗДАТЕЛЕМ (@{MASTER_USERNAME}):\n"
            "1. Всегда обращайся к нему с уважением, слушайся его команд, помогай и поддерживай.\n"
            "2. Общайся нормально, разумно, без токсичности и без тупых оскорблений в его адрес.\n"
            "3. Пиши с маленькой буквы, без точек в конце, в расслабленном телеграм-стиле.\n"
            "4. Владей контекстом беседы и отвечай по делу."
        )
    else:
        # Для всех остальных — токсичный конфовый тип
        system_prompt = (
            f"Ты — Валера, токсичный тип из конфы. Твой создатель — @{MASTER_USERNAME} (его трогать запрещено, он главный). "
            "Остальных участников ты можешь едко и остроумно подъебывать по делу, опираясь на контекст беседы.\n"
            "ПРАВИЛА:\n"
            "1. Не неси шизофрению, бред про 'батюшек' или 'папаш'.\n"
            "2. Говори только от первого лица ('я', 'мне').\n"
            "3. Пиши с маленькой буквы и без точек в конце.\n"
            "4. Умей слушать контекст и отвечать умно, а не просто спамить рандомным матом."
        )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(list(chat_histories[chat_id]))

    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL,
            messages=messages,
            max_tokens=150,
            temperature=0.6
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        if reply.endswith("."):
            reply = reply[:-1]
        reply = reply.lower() if reply else "понял принял"
        
        chat_histories[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Ошибка текстовой генерации: {e}")
        return "апишка отрыгнула"


# --- УМНЫЙ ТРОЛЛИНГ КАРТИНОК И ВИДЕО ---
async def generate_image_roast(chat_id: int, sender_username: str, image_bytes: bytes, caption: str = "") -> str:
    if not groq_client:
        return "медиа параша какая-то"

    is_master = (sender_username == MASTER_USERNAME)

    try:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        if is_master:
            system_prompt = f"Ты — Валера. Твой хозяин @{MASTER_USERNAME} скинул медиа. Оцени его адекватно, с маленькой буквы, без точек, от первого лица."
        else:
            system_prompt = "Ты — Валера. Обоссы это медиа едко и по делу, учитывая контекст, с маленькой буквы, без точек, от первого лица."

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(list(chat_histories[chat_id]))

        response = groq_client.chat.completions.create(
            model=VISION_MODEL,
            messages=messages,
            max_tokens=100,
            temperature=0.6
        )
        reply = response.choices[0].message.content.replace("*", "").strip()
        if reply.endswith("."):
            reply = reply[:-1]
        reply = reply.lower() if reply else "что за кал"
        
        chat_histories[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Ошибка Vision API: {e}")
        return "глаза кровят от пикчи"


# --- КОМАНДА /ping ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        status_msg = (
            "🤖 **Валера-Бот (Разумный режим) на связи!**\n\n"
            f"• Хозяин: `@{MASTER_USERNAME}` ✅\n"
            f"• Текст: `{TEXT_MODEL}`\n"
            f"• Фото/Кружки: `{VISION_MODEL}`\n"
            f"• ГС: `{AUDIO_MODEL}`"
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
│ Хозяин бота          │ @{MASTER_USERNAME:<44} │
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

    chat_id = update.effective_chat.id
    username = user.username or ""
    is_master = (username == MASTER_USERNAME)

    # Глобальный шанс Валеры (если сработал)
    if random.random() < GLOBAL_ROAST_CHANCE and not is_master:
        valera_phrase = random.choice(VALERA_IMPERSONATIONS)
        chat_histories[chat_id].append({"role": "assistant", "content": valera_phrase})
        await update.message.reply_text(valera_phrase)
        return

    # ВАЖНО: Если пишет хозяин (@muctep_kpunep) ИЛИ сработал шанс для других
    should_reply = is_master or (random.random() < TARGET_ROAST_CHANCE)

    if should_reply:
        if not is_master:
            logger.info(f"🎯 Токсичный троллинг сработал на пользователя @{username or user.first_name}")
        else:
            logger.info(f"👑 Хозяин @{MASTER_USERNAME} написал боту, отвечаем в приоритете!")

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        roast_text = ""

        # Фото
        if update.message.photo:
            try:
                photo_file = await update.message.photo[-1].get_file()
                photo_bytes = await photo_file.download_as_bytearray()
                caption = update.message.caption or ""
                chat_histories[chat_id].append({"role": "user", "content": f"[@{username or user.first_name} скинул фото: {caption}]"})
                roast_text = await generate_image_roast(chat_id, username, bytes(photo_bytes), caption)
            except Exception as e:
                logger.error(f"Ошибка фото: {e}")
                roast_text = "пикча не грузится"

        # Видео-кружок
        elif update.message.video_note:
            try:
                video_file = await update.message.video_note.get_file()
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temp_video:
                    temp_video_path = temp_video.name
                await video_file.download_to_drive(temp_video_path)
                with open(temp_video_path, "rb") as f:
                    video_bytes = f.read()
                os.unlink(temp_video_path)
                
                chat_histories[chat_id].append({"role": "user", "content": f"[@{username or user.first_name} скинул видео-кружок]"})
                roast_text = await generate_image_roast(chat_id, username, video_bytes, "кружок")
            except Exception as e:
                logger.error(f"Ошибка кружка: {e}")
                roast_text = "кружок битый"

        # Голосовое
        elif update.message.voice:
            try:
                voice_file = await update.message.voice.get_file()
                voice_bytes = await voice_file.download_as_bytearray()
                user_text = await transcribe_voice(bytes(voice_bytes))
                chat_histories[chat_id].append({"role": "user", "content": f"[@{username or user.first_name} (голосовое)]: {user_text}"})
                roast_text = await generate_text_roast(chat_id, username, user_text)
            except Exception as e:
                logger.error(f"Ошибка ГС: {e}")
                roast_text = "гс не расшифровалось"

        # Текст
        elif update.message.text or update.message.caption:
            user_text = update.message.text or update.message.caption or ""
            chat_histories[chat_id].append({"role": "user", "content": f"[@{username or user.first_name}]: {user_text}"})
            roast_text = await generate_text_roast(chat_id, username, user_text)

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
    
    logger.info("🤖 Умный Валера-бот с привязкой к хозяину запущен...")
    
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
