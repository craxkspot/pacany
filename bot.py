import os
import random
import logging
import io
import re
import base64
import urllib.parse
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI
from duckduckgo_search import DDGS

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

groq_client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)

TEXT_MODEL = "qwen/qwen3.8-27b"
VISION_MODEL = "llama-3.2-11b-vision-preview"  # Бесплатная модель с поддержкой зрения на Groq

chat_histories = {}


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


async def generate_image(prompt: str) -> bytes | None:
    """Бесплатная генерация картинки через Pollinations с каскадным фоллбэком"""
    encoded_prompt = urllib.parse.quote(prompt)
    seed = random.randint(1, 1000000)
    
    # Резервная цепочка моделей Pollinations
    urls = [
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&model=flux",
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&model=turbo",
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}"
    ]

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        for url in urls:
            try:
                response = await client.get(url)
                if response.status_code == 200 and len(response.content) > 5000:
                    return response.content
            except Exception as e:
                logger.warning(f"Ошибка запроса к Pollinations ({url}): {e}")
    return None


def search_web(query: str) -> str:
    """Поиск в интернете через DuckDuckGo с очисткой поискового запроса"""
    clean_query = re.sub(
        r'(?i)\b(пантера|pantera|ты знаешь|кто такой|кто такая|что за|расскажи про|найти|загугли|гугл)\b', 
        '', 
        query
    ).strip()
    
    if not clean_query:
        clean_query = query

    try:
        with DDGS() as ddgs:
            results = [r.get('body', '') for r in ddgs.text(clean_query, max_results=3)]
            if results:
                return "\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
    return ""


def is_image_request(text: str) -> bool:
    """Проверка, запрашивает ли пользователь генерацию картинки"""
    keywords = [
        "нарисуй", "сделай картинку", "сгенерируй", "создай изображение", 
        "сделай фото", "замути", "отрисуй", "покажи", "изобрази", "запили фотку", "запили картинку"
    ]
    return any(kw in text.lower() for kw in keywords)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name or "Пользователь"
    chat_type = update.message.chat.type

    user_text = update.message.text or update.message.caption or ""
    lower_text = user_text.lower()

    # 1. ОБРАБОТКА И ВЫКАЧИВАНИЕ ИЗОБРАЖЕНИЯ (ЕСЛИ ПРИСЛАНО В ЧАТ)
    base64_image = None
    if update.message.photo:
        try:
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            base64_image = base64.b64encode(photo_bytes).decode('utf-8')
        except Exception as e:
            logger.error(f"Ошибка скачивания картинки из Telegram: {e}")

    if not user_text and not base64_image:
        return

    # 2. ГЕНЕРАЦИЯ КАРТИНОК ПО ЗАПРОСУ
    if is_image_request(lower_text):
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        
        prompt_for_image = lower_text
        remove_words = [
            "нарисуй", "сделай", "сгенерируй", "создай", "замути", "отрисуй", 
            "покажи", "пантера", "pantera", "фотку", "фото", "картинку", "изображение"
        ]
        for word in remove_words:
            prompt_for_image = re.sub(r'\b' + re.escape(word) + r'\b', '', prompt_for_image, flags=re.IGNORECASE).strip()
            
        if not prompt_for_image:
            prompt_for_image = user_text

        try:
            enh_resp = groq_client.chat.completions.create(
                model=TEXT_MODEL,
                messages=[
                    {
                        "role": "system", 
                        "content": (
                            "You are an expert prompt engineer for AI image generators. "
                            "Translate the request into a detailed English prompt with background, objects, and framing details. "
                            "Output ONLY the English prompt."
                        )
                    },
                    {"role": "user", "content": prompt_for_image}
                ],
                max_tokens=150
            )
            detailed_prompt = enh_resp.choices[0].message.content.strip()
        except Exception:
            detailed_prompt = prompt_for_image

        image_bytes = await generate_image(detailed_prompt)
        if image_bytes:
            photo_stream = io.BytesIO(image_bytes)
            photo_stream.name = "image.jpg"
            await update.message.reply_photo(photo=photo_stream)
            return
        else:
            await update.message.reply_text("Сервер генерации перегружен, попробуй позже.")
            return

    # 3. ИНИЦИАЛИЗАЦИЯ ИСТОРИИ ЧАТА
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system", 
                "content": (
                    "Ты — адекватный, живой ИИ-собеседник. Ты общаешься естественно, подстраиваешься под вайб чата.\n"
                    "ПРАВИЛА:\n"
                    "1. Если информации о человеке, никнейме или месте нет — ЧЕСТНО скажи, что не знаешь.\n"
                    "2. СТРОГО ЗАПРЕЩЕНО выдумывать биографии людей, фильмы и вымышленные факты.\n"
                    "3. Никогда не говори 'я не могу гуглить' или 'у меня нет доступа в сеть'.\n"
                    "4. Пиши ТОЛЬКО обычным плоским текстом без звездочек и Markdown."
                )
            }
        ]

    # Сохраняем текстовое представление в истории
    history_entry = f"{user_name}: {user_text}" if user_text else f"{user_name}: [Отправил фото]"
    chat_histories[chat_id].append({"role": "user", "content": history_entry})
    
    if len(chat_histories[chat_id]) > 12:
        system_prompt = chat_histories[chat_id][0]
        recent_msgs = chat_histories[chat_id][-11:]
        chat_histories[chat_id] = [system_prompt] + recent_msgs

    is_reply_to_bot = (
        update.message.reply_to_message 
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    is_mentioned = "пантера" in lower_text or base64_image is not None
    
    should_random_speak = random.random() < 0.10
    if chat_type != "private" and not is_reply_to_bot and not is_mentioned and not should_random_speak:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # 4. ПОИСК В СЕТИ (ТОЛЬКО ЕСЛИ НЕТ КАРТИНКИ)
    search_context = ""
    if not base64_image:
        needs_search_keywords = ["кто", "что", "где", "когда", "почему", "знаешь", "найди", "инфа", "загугли", "гугл"]
        should_search = any(kw in lower_text for kw in needs_search_keywords) or len(user_text.split()) <= 3

        if should_search:
            search_result = search_web(user_text)
            if search_result:
                search_context = f"\n\n[ДАННЫЕ ИЗ ПОИСКА В ИНТЕРНЕТЕ]:\n{search_result}"
            else:
                search_context = "\n\n[ДАННЫЕ ИЗ ПОИСКА]: Информация не найдена. Если не знаешь ответа — честно скажи об этом."

    # Собираем контекст для запроса
    messages_to_send = list(chat_histories[chat_id])
    if search_context:
        messages_to_send[0] = {
            "role": "system",
            "content": chat_histories[chat_id][0]["content"] + search_context
        }

    # ВЫБОР МОДЕЛИ: Если есть фото — переключаемся на Vision модель
    active_model = TEXT_MODEL
    if base64_image:
        active_model = VISION_MODEL
        # Подготавливаем текущий запрос с картинкой
        image_content = []
        prompt_text = user_text if user_text else "Опиши, что изображено на этом фото."
        image_content.append({"type": "text", "text": f"{user_name}: {prompt_text}"})
        image_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
        })
        messages_to_send[-1] = {"role": "user", "content": image_content}

    # 5. ГЕНЕРАЦИЯ ОТВЕТА
    try:
        response = groq_client.chat.completions.create(
            model=active_model,
            messages=messages_to_send,
            max_tokens=400,
            temperature=0.2, # Низкая температура от галлюцинаций
        )
        reply_text = response.choices[0].message.content
        
        if reply_text:
            reply_text = reply_text.replace("*", "")
            chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        else:
            reply_text = "Не совсем понял, повтори еще раз."
    except Exception as e:
        logger.error(f"Ошибка Groq API ({active_model}): {e}")
        reply_text = "Сервер временно недоступен."

    try:
        await update.message.reply_text(reply_text)
    except Exception as e:
        logger.error(f"Ошибка отправки Telegram: {e}")


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Токены не заданы! Проверь переменные окружения.")
        return

    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    application.run_polling()


if __name__ == "__main__":
    main()
