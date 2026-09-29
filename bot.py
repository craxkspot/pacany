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

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    force=True
)
logger = logging.getLogger(__name__)

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

MASTER_USERNAME = "muctep_kpunep"

# Подключаемся к OpenRouter
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=API_KEY
) if API_KEY else None

# --- КОНТЕКСТ ЧАТОВ (ПАМЯТЬ ДО 50 СООБЩЕНИЙ) ---
chat_histories = defaultdict(lambda: deque(maxlen=50))


# --- АВТОМАТИЧЕСКИЙ ПОИСК ЛУЧШИХ БЕСПЛАТНЫХ МОДЕЛЕЙ ---
def get_active_models() -> tuple[str, str, str]:
    selected_text, selected_vision, selected_audio = None, None, None

    if not client:
        return "meta-llama/llama-3.1-8b-instruct:free", "meta-llama/llama-3.1-8b-instruct:free", None

    try:
        logger.info("🔍 Запрашиваем список доступных моделей с OpenRouter...")
        models_data = client.models.list().data
        available_ids = [m.id for m in models_data]
        logger.info(f"Всего доступно моделей: {len(available_ids)}")

        # Ищем бесплатные модели (с суффиксом :free)
        free_models = [m for m in available_ids if ":free" in m.lower()]
        
        # Стоп-слова для отсеивания служебного мусора
        bad_words = ["embed", "tts", "guard", "safeguard", "audio", "code", "coder", "math"]

        # 1. Текстовая модель (адекватная и умная)
        text_candidates = [m for m in free_models if not any(bw in m.lower() for bw in bad_words) and "vision" not in m.lower()]
        if text_candidates:
            # Приоритет отдаем Llama или Gemma, если они есть
            preferred = [m for m in text_candidates if "llama" in m.lower() or "gemma" in m.lower()]
            selected_text = preferred[0] if preferred else text_candidates[0]

        # 2. Модель со зрением (Vision)
        vision_candidates = [m for m in free_models if ("vision" in m.lower() or "vl" in m.lower() or "qwen" in m.lower()) and not any(bw in m.lower() for bw in bad_words)]
        if vision_candidates:
            selected_vision = vision_candidates[0]
        else:
            selected_vision = selected_text  # Фоллбэк на текст

        # 3. Аудиомодель (если есть в бесплатных)
        audio_candidates = [m for m in free_models if "whisper" in m.lower() or "audio" in m.lower()]
        if audio_candidates:
            selected_audio = audio_candidates[0]

    except Exception as e:
        logger.error(f"❌ Ошибка автоподбора моделей: {e}")

    # Финальные фоллбэки, если автоподбор не сработал
    fallback_text = "meta-llama/llama-3.1-8b-instruct:free"
    return (
        selected_text or fallback_text,
        selected_vision or selected_text or fallback_text,
        selected_audio
    )


TEXT_MODEL, VISION_MODEL, AUDIO_MODEL = get_active_models()


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


# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Valera OpenRouter Bot is live!")

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


# --- ГЕНЕРАТОР ТЕКСТА ---
async def generate_text_roast(chat_id: int, sender_username: str, user_text: str) -> str:
    if not client:
        return "мозги отключены, нет апи ключа"

    search_keywords = ["кто такой", "что такое", "когда", "где", "найди", "погугли", "курс", "цена", "почему", "сколько"]
    search_data = ""
    if any(kw in user_text.lower() for kw in search_keywords):
        search_data = search_web(user_text)

    system_prompt = (
        f"Ты — Валера, живой, токсичный и едкий тип из конфы. Твой создатель — {MASTER_USERNAME}. "
        "Внимательно анализируй контекст разговора, историю беседы и то, что скидывают пользователи. "
        "Отвечай осмысленно, опираясь на реальную ситуацию и диалог, не неси рандомный бред. "
        "Можешь использовать слова вроде 'чо' или 'чож', если это уместно, и изредка тегать участников беседы, если ситуация располагает, но делай это естественно. "
        "С создателем ({MASTER_USERNAME}) общайся с особым пристрастием и едкими подколами, признавая его авторитет. "
        "Остальных участников жестко стеби, души за глупости и ставь на место по фактам из переписки. "
        "Пиши естественно, с маленькой буквы."
    )
    if search_data:
        system_prompt += f"\n\nФАКТЫ ИЗ ИНТЕРНЕТА:\n{search_data}"

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(list(chat_histories[chat_id]))
    messages.append({"role": "user", "content": user_text})

    try:
        logger.info(f"Отправляем запрос в модель {TEXT_MODEL}...")
        response = client.chat.completions.create(
            model=TEXT_MODEL, messages=messages, max_tokens=300, temperature=0.7
        )
        
        choice = response.choices[0]
        reply = choice.message.content if choice.message and choice.message.content else ""
        reply = reply.replace("*", "").strip()

        if not reply:
            logger.error(f"❌ Модель {TEXT_MODEL} вернула пустой контент! Finish reason: {choice.finish_reason}")
            return "у меня пустой бак, модель прислала пустоту"

        if reply.endswith("."):
            reply = reply[:-1]
        reply = reply.lower()

        chat_histories[chat_id].append({"role": "user", "content": user_text})
        chat_histories[chat_id].append({"role": "assistant", "content": reply})
        return reply
        
    except Exception as e:
        logger.error(f"❌ Ошибка API при генерации текста: {e}", exc_info=True)
        return "у меня словесный понос, апи лагает"


# --- ОБРАБОТЧИК КАРТИНОК И СТИКЕРОВ ---
async def generate_image_roast(chat_id: int, sender_username: str, image_bytes: bytes, caption: str, media_type: str) -> str:
    if not client or not VISION_MODEL:
        return await generate_text_roast(chat_id, sender_username, f"[пользователь скинул {media_type}, но у меня нет зрячей модели]")

    try:
        image = Image.open(io.BytesIO(image_bytes))
        if image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        
        output_buffer = io.BytesIO()
        image.save(output_buffer, format="JPEG")
        jpeg_bytes = output_buffer.getvalue()

        base64_image = base64.b64encode(jpeg_bytes).decode('utf-8')
        
        sys_prompt = (
            f"Ты Валера, токсичный тип из конфы. Создатель — {MASTER_USERNAME}. "
            f"Посмотри на этот {media_type}, пойми контекст и жестко, едко постеби пользователя. "
            "Пиши с маленькой буквы, без точек."
        )

        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(list(chat_histories[chat_id]))
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": caption if caption else f"что на этом {media_type}? разнеси это"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]
        })

        logger.info(f"Отправляем изображение в модель {VISION_MODEL}...")
        response = client.chat.completions.create(
            model=VISION_MODEL, messages=messages, max_tokens=200, temperature=0.7
        )
        
        choice = response.choices[0]
        reply = choice.message.content if choice.message and choice.message.content else ""
        reply = reply.replace("*", "").strip().lower()

        if not reply:
            return await generate_text_roast(chat_id, sender_username, f"[пользователь скинул {media_type}, но модель вернула пустоту]")

        if reply.endswith("."):
            reply = reply[:-1]
        
        chat_histories[chat_id].append({"role": "user", "content": f"[скинул {media_type}] {caption}"})
        chat_histories[chat_id].append({"role": "assistant", "content": reply})
        return reply
        
    except Exception as e:
        logger.error(f"❌ Ошибка Vision API при обработке {media_type}: {e}", exc_info=True)
        return await generate_text_roast(chat_id, sender_username, f"[пользователь скинул {media_type}, но зрячая модель не справилась]")


# --- КОМАНДА PING ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        status_msg = (
            "🤖 **Валера (Auto-Model OpenRouter) на связи!**\n\n"
            f"• Папочка: `{MASTER_USERNAME}` ✅\n"
            f"• Текст: `{TEXT_MODEL}`\n"
            f"• Зрение: `{VISION_MODEL}`\n"
            f"• Слух: `{AUDIO_MODEL or 'текстовый фолбэк'}`\n"
            f"• Память: `50 сообщений`"
        )
        await update.message.reply_text(status_msg, parse_mode="Markdown")


def print_startup_status_table() -> bool:
    table_log = f"""
┌────────────────────────────────────────────────────────────────────────┐
│         ОТЧЕТ О ЗАПУСКЕ ВАЛЕРЫ (АВТОПОДБОР МОДЕЛЕЙ)                    │
├──────────────────────┬─────────────────────────────────────────────────┤
│ Текстовая модель     │ {TEXT_MODEL:<47} │
│ Зрячая модель        │ {VISION_MODEL:<47} │
│ Аудиомодель          │ {str(AUDIO_MODEL):<47} │
└──────────────────────┴─────────────────────────────────────────────────┘
"""
    logger.info(table_log)
    return bool(TELEGRAM_TOKEN)


# --- ГЛАВНЫЙ АЛГОРИТМ ПРИНЯТИЯ РЕШЕНИЯ ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    if update.message.from_user.is_bot:
        return

    chat_id = update.effective_chat.id
    username = update.message.from_user.username or update.message.from_user.first_name
    text = update.message.text or update.message.caption or ""

    user_tag = username  

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
        if not text and (update.message.photo or update.message.sticker or update.message.video or update.message.voice or update.message.animation):
             chat_histories[chat_id].append({"role": "user", "content": f"[{user_tag} отправил медиафайл]"})
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    roast_text = ""

    try:
        if update.message.photo:
            logger.info("Обрабатываем фото...")
            f = await update.message.photo[-1].get_file()
            b = await f.download_as_bytearray()
            roast_text = await generate_image_roast(chat_id, username, bytes(b), text, "фото")
            
        elif update.message.sticker:
            logger.info("Обрабатываем стикер...")
            if update.message.sticker.is_animated or update.message.sticker.is_video:
                roast_text = await generate_text_roast(chat_id, username, f"[пользователь скинул анимированный стикер. Обосри его за эти картинки]")
            else:
                f = await update.message.sticker.get_file()
                b = await f.download_as_bytearray()
                roast_text = await generate_image_roast(chat_id, username, bytes(b), text or "стикер", "стикер")
                
        elif update.message.animation or update.message.video:
            media = "гифку" if update.message.animation else "видео"
            logger.info(f"Обрабатываем {media}...")
            roast_text = await generate_text_roast(chat_id, username, f"[пользователь скинул {media}. Пройдись по нему за это]")
            
        elif update.message.voice or update.message.video_note or update.message.audio:
            media_name = "голосовуху" if update.message.voice else ("кружок" if update.message.video_note else "музыку")
            logger.info(f"Обрабатываем аудио ({media_name})...")
            roast_text = await generate_text_roast(chat_id, username, f"[пользователь записал {media_name}. Высмей его за это]")
            
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
    
    logger.info("🤖 Валера с автоподбором моделей запущен!")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
