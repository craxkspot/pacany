import os
import random
import logging
import io
import base64
import urllib.parse
from collections import defaultdict, deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from openai import OpenAI
import requests
from PIL import Image

# --- НАСТРОЙКА ЛОГИРОВАНИЯ (ПОСТОЯННЫЙ ВЫВОД) ---
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    force=True
)
logger = logging.getLogger(__name__)

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")  # Опционально для поиска

MASTER_USERNAME = "muctep_kpunep"

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
) if GROQ_API_KEY else None

# --- КОНТЕКСТ ЧАТОВ (ПАМЯТЬ ДО 50 СООБЩЕНИЙ) ---
chat_histories = defaultdict(lambda: deque(maxlen=50))


# --- ПОИСК В ИНТЕРНЕТЕ ---
def search_web(query: str) -> str:
    logger.info(f"🔍 Ищем в сети: {query}")
    try:
        if TAVILY_API_KEY:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": query, "max_results": 3},
                timeout=5
            )
            data = resp.json()
            results = [res.get("content", "") for res in data.get("results", [])]
            if results:
                return "\n".join(results)

        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json"
        resp = requests.get(url, timeout=5)
        data = resp.json()
        abstract = data.get("AbstractText")
        if abstract:
            return abstract
        
        related = data.get("RelatedTopics", [])
        for topic in related:
            if "Text" in topic:
                return topic["Text"]
                
        return "ничего конкретного в сети не нашлось"
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        return "интернет-поиск временно отрыгнул"


# --- АВТОМАТИЧЕСКИЙ ПОИСК РАБОЧЕЙ МОДЕЛИ ИЗ ДОСТУПНЫХ ---
def get_active_models() -> tuple[str, str, str]:
    selected_text, selected_vision, selected_audio = None, None, None

    if not groq_client:
        return "llama-3.1-8b-instant", "qwen/qwen3.8-27b", "whisper-large-v3-turbo"

    try:
        models_data = groq_client.models.list().data
        available_ids = [m.id for m in models_data if "decommissioned" not in m.id.lower()]
        logger.info(f"Доступные модели на Groq: {available_ids}")

        bad_words = ["whisper", "guard", "safeguard", "audio", "embed", "tts"]

        # Текстовая модель
        text_candidates = [m for m in available_ids if not any(bw in m.lower() for bw in bad_words) and "vision" not in m.lower()]
        if text_candidates:
            selected_text = text_candidates[0]

        # Модель со зрением
        vision_candidates = [m for m in available_ids if "vision" in m.lower() or "qwen" in m.lower()]
        if vision_candidates:
            selected_vision = vision_candidates[0]
        else:
            selected_vision = selected_text

        # Аудиомодель
        audio_candidates = [m for m in available_ids if "whisper" in m.lower()]
        if audio_candidates:
            selected_audio = audio_candidates[0]

    except Exception as e:
        logger.error(f"Ошибка автоподбора моделей: {e}")

    return (
        selected_text or "llama-3.1-8b-instant",
        selected_vision or "qwen/qwen3.8-27b",
        selected_audio or "whisper-large-v3-turbo"
    )


TEXT_MODEL, VISION_MODEL, AUDIO_MODEL = get_active_models()


# --- ВЕБ-СЕРВЕР ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Valera Bot Ultimate is live!")

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


# --- ГЕНЕРАТОР ТЕКСТА (БАЗА) ---
async def generate_text_roast(chat_id: int, sender_username: str, user_text: str) -> str:
    if not groq_client:
        return "мозги отключены, нет апи ключа"

    search_keywords = ["кто такой", "что такое", "когда", "где", "найди", "погугли", "курс", "цена", "почему", "сколько"]
    search_data = ""
    if any(kw in user_text.lower() for kw in search_keywords):
        search_data = search_web(user_text)

    system_prompt = (
        f"Ты — Валера, живой, дерзкий и едкий тип из конфы. Твой создатель — {MASTER_USERNAME}. "
        "ПРАВИЛА:\n"
        "1. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО тегать (использовать символ @) создателя или любых других пользователей в твоих ответах. Общайся естественно, без упоминаний никнеймов.\n"
        f"2. С создателем ({MASTER_USERNAME}) общайся с подколами, но признавай, что он твой папочка.\n"
        "3. Остальных души и стеби по фактам, опираясь на контекст всей беседы.\n"
        "4. Если пользователь скинул системное уведомление в квадратных скобках (например, [отправил гифку...]), "
        "сгенерируй живую едкую реакцию ИМЕННО на этот факт, отмажься или высмей его.\n"
        "5. Пиши ВСЕГДА с маленькой буквы, без точек в конце, живо.\n"
        "6. Запрещено использовать слова 'чож' или 'чо'."
    )
    if search_data:
        system_prompt += f"\n\nФАКТЫ ИЗ ИНТЕРНЕТА:\n{search_data}"

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(list(chat_histories[chat_id]))
    messages.append({"role": "user", "content": user_text})

    try:
        response = groq_client.chat.completions.create(
            model=TEXT_MODEL, messages=messages, max_tokens=200, temperature=0.75
        )
        
        choice = response.choices[0]
        reply = choice.message.content if choice.message and choice.message.content else ""
        reply = reply.replace("*", "").strip()

        if not reply:
            logger.error(f"❌ Модель {TEXT_MODEL} вернула пустой контент! Finish reason: {choice.finish_reason}")
            return "у меня пустой бак, модель прислала пустоту"

        if reply.endswith("."):
            reply = reply[:-1]
        reply = reply.lower().replace("чож", "").replace("чо ", "че ").strip()

        chat_histories[chat_id].append({"role": "user", "content": user_text})
        chat_histories[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Ошибка текста: {e}", exc_info=True)
        return "у меня словесный понос, апи лагает"


# --- ОБРАБОТЧИК КАРТИНОК И СТИКЕРОВ С КОНВЕРТАЦИЕЙ В JPEG ---
async def generate_image_roast(chat_id: int, sender_username: str, image_bytes: bytes, caption: str, media_type: str) -> str:
    if not groq_client or not VISION_MODEL:
        logger.warning("⚠️ Vision API отключен или не найдена зрячая модель.")
        return await generate_text_roast(chat_id, sender_username, f"[пользователь скинул {media_type}, но у меня нет зрячей модели]")

    try:
        # Конвертируем любые байты (включая webp-стикеры) в стандартный JPEG
        image = Image.open(io.BytesIO(image_bytes))
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        
        output_buffer = io.BytesIO()
        image.save(output_buffer, format="JPEG")
        jpeg_bytes = output_buffer.getvalue()

        base64_image = base64.b64encode(jpeg_bytes).decode('utf-8')
        
        sys_prompt = (
            f"Ты Валера. Хозяин {MASTER_USERNAME}" if sender_username == MASTER_USERNAME else "Ты Валера. Обоссы это медиа."
        ) + " Опиши то, что видишь на картинке/стикере, и жестко постебись. НИКАКИХ тегов через @. Пиши с маленькой буквы, без точек. Без слова 'чож'."

        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(list(chat_histories[chat_id]))
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": caption if caption else f"что изображено на этом {media_type}? опиши и высмей"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        })

        logger.info(f"Отправляем изображение в зрячую модель {VISION_MODEL}...")
        response = groq_client.chat.completions.create(
            model=VISION_MODEL, messages=messages, max_tokens=150, temperature=0.7
        )
        
        choice = response.choices[0]
        reply = choice.message.content if choice.message and choice.message.content else ""
        reply = reply.replace("*", "").strip().lower()

        if not reply:
            logger.error(f"❌ Vision модель {VISION_MODEL} вернула пустой ответ! Finish reason: {choice.finish_reason}")
            return await generate_text_roast(chat_id, sender_username, f"[пользователь скинул {media_type}, но зрячая модель вернула пустоту]")

        if reply.endswith("."):
            reply = reply[:-1]
        
        chat_histories[chat_id].append({"role": "user", "content": f"[скинул {media_type}] {caption}"})
        chat_histories[chat_id].append({"role": "assistant", "content": reply})
        return reply
        
    except Exception as e:
        logger.error(f"❌ Ошибка Vision API при обработке {media_type}: {e}", exc_info=True)
        return await generate_text_roast(chat_id, sender_username, f"[пользователь скинул {media_type}, но произошла ошибка обработки изображения: {e}]")


# --- ОБРАБОТЧИК АУДИО, ГС, МУЗЫКИ И КРУЖКОВ ---
async def generate_audio_roast(chat_id: int, sender_username: str, file_bytes: bytes, file_ext: str, media_name: str) -> str:
    if not groq_client or not AUDIO_MODEL:
        return await generate_text_roast(chat_id, sender_username, f"[пользователь записал {media_name}. У тебя временно отвалились уши]")
    
    try:
        audio_file = (f"audio{file_ext}", io.BytesIO(file_bytes), f"audio/{file_ext.replace('.', '')}")
        transcription = groq_client.audio.transcriptions.create(
            file=audio_file, model=AUDIO_MODEL, response_format="text"
        )
        text_result = str(transcription).strip()
        
        if not text_result:
            return await generate_text_roast(chat_id, sender_username, f"[пользователь скинул {media_name}, но там тишина]")

        prompt = f"[скинул {media_name}, вот расшифровка: «{text_result}»]. Разнеси его за то, что он несет в этом {media_name}!"
        return await generate_text_roast(chat_id, sender_username, prompt)
    except Exception as e:
        logger.error(f"Ошибка Audio API: {e}", exc_info=True)
        return await generate_text_roast(chat_id, sender_username, f"[пользователь скинул {media_name}, но из-за сбоя ты оглох]")


# --- КОМАНДА PING ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        status_msg = (
            "🤖 **Валера (Ультимативный режим) на связи!**\n\n"
            f"• Папочка: `{MASTER_USERNAME}` ✅\n"
            f"• Текст: `{TEXT_MODEL}`\n"
            f"• Глаза: `{VISION_MODEL or 'ОТКЛЮЧЕНЫ'}`\n"
            f"• Уши: `{AUDIO_MODEL or 'ОТКЛЮЧЕНЫ'}`\n"
            f"• Память: `50 сообщений`\n"
            f"• Рандом: `ВКЛЮЧЕН (10%)`"
        )
        await update.message.reply_text(status_msg, parse_mode="Markdown")


def print_startup_status_table() -> bool:
    table_log = f"""
┌────────────────────────────────────────────────────────────────────────┐
│               ОТЧЕТ О ЗАПУСКЕ УЛЬТИМАТИВНОГО ВАЛЕРЫ                    │
├──────────────────────┬─────────────────────────────────────────────────┤
│ Текстовая модель     │ {TEXT_MODEL:<47} │
│ Зрячая модель        │ {str(VISION_MODEL):<47} │
│ Ушастая модель       │ {str(AUDIO_MODEL):<47} │
└──────────────────────┴─────────────────────────────────────────────────┘
"""
    logger.info(table_log)
    return bool(TELEGRAM_TOKEN)


# --- ГЛАВНЫЙ АЛГОРИТМ ПРИНЯТИЯ РЕШЕНИЯ С ПОДРОБНЫМИ ЛОГАМИ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    if update.message.from_user.is_bot:
        return

    chat_id = update.effective_chat.id
    username = update.message.from_user.username or update.message.from_user.first_name
    text = update.message.text or update.message.caption or ""

    user_tag = username  

    # 1. Проверяем триггеры
    is_reply_to_bot = bool(update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id)
    is_addressed = any(word in text.lower() for word in ["валер", "валера", "валерон"])
    is_random_reply = random.random() < 0.10

    should_reply = is_addressed or is_reply_to_bot or is_random_reply

    logger.info(
        f"💬 Входящее от [{username}] в чате {chat_id}: '{text[:30]}...' | "
        f"Адресовано: {is_addressed}, Ответ боту: {is_reply_to_bot}, Рандом (10%): {is_random_reply} -> Решение отвечать: {should_reply}"
    )

    if not should_reply:
        if text: 
            chat_histories[chat_id].append({"role": "user", "content": f"[{user_tag}]: {text}"})
        if not text and (update.message.photo or update.message.sticker or update.message.video or update.message.voice or update.message.video_note or update.message.animation):
             chat_histories[chat_id].append({"role": "user", "content": f"[{user_tag} отправил медиафайл]"})
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    roast_text = ""

    try:
        # ОБРАБОТКА МЕДИА
        if update.message.photo:
            logger.info("Обрабатываем фото...")
            f = await update.message.photo[-1].get_file()
            b = await f.download_as_bytearray()
            roast_text = await generate_image_roast(chat_id, username, bytes(b), text, "фото")
            
        elif update.message.sticker:
            logger.info("Обрабатываем стикер...")
            if update.message.sticker.is_animated or update.message.sticker.is_video:
                roast_text = await generate_text_roast(chat_id, username, f"[пользователь скинул анимированный стикер. Обосри его за эти шевелящиеся картинки для детей]")
            else:
                f = await update.message.sticker.get_file()
                b = await f.download_as_bytearray()
                roast_text = await generate_image_roast(chat_id, username, bytes(b), text or "стикер", "стикер")
                
        elif update.message.animation or update.message.video:
            media = "гифку" if update.message.animation else "видео"
            logger.info(f"Обрабатываем {media}...")
            roast_text = await generate_text_roast(chat_id, username, f"[пользователь скинул {media}. Жестко пройдись по нему за то, что он засоряет чат движущимся калом]")
            
        elif update.message.voice or update.message.video_note or update.message.audio:
            media_name = "голосовуху" if update.message.voice else ("кружок" if update.message.video_note else "музыку")
            logger.info(f"Обрабатываем аудио ({media_name})...")
            ext = ".ogg" if update.message.voice else ".mp4"
            file_obj = update.message.voice or update.message.video_note or update.message.audio
            f = await file_obj.get_file()
            b = await f.download_as_bytearray()
            roast_text = await generate_audio_roast(chat_id, username, bytes(b), ext, media_name)
            
        else:
            logger.info("Генерируем текстовый ответ...")
            roast_text = await generate_text_roast(chat_id, username, f"[{user_tag}]: {text}")

    except Exception as e:
        logger.error(f"❌ Глобальная ошибка обработки сообщения: {e}", exc_info=True)
        roast_text = "у меня крыша едет от ваших сообщений, ошибка в ядре"

    if roast_text:
        logger.info(f"Отправляем ответ в чат {chat_id}: {roast_text[:50]}...")
        await update.message.reply_text(roast_text)
    else:
        logger.warning(f"⚠️ Текст ответа пустой! Ничего не отправлено.")


def main():
    Thread(target=run_web_server, daemon=True).start()
    print_startup_status_table()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    
    logger.info("🤖 Ультимативный Валера-бот 2.0 запущен!")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
