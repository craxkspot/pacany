import os
import random
import logging
import io
import re
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

GROQ_MODEL = "qwen/qwen3.8-27b"
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
    """Генерация картинок через Pollinations с моделью Flux"""
    encoded_prompt = urllib.parse.quote(prompt)
    seed = random.randint(1, 1000000)
    # Используем модель Flux для точной детализации, без платного nologo=true
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&model=flux"
    
    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
        try:
            response = await client.get(image_url)
            if response.status_code == 200:
                return response.content
            else:
                logger.error(f"Ошибка от сервера картинок: {response.status_code}")
        except Exception as e:
            logger.error(f"Ошибка генерации картинки: {e}")
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
            results = [r.get('body', '') for r in ddgs.text(clean_query, max_results=4)]
            if results:
                return "\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
    return ""


def is_image_request(text: str) -> bool:
    """Проверка, запрашивает ли пользователь картинку"""
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

    image_url = None
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        image_url = photo_file.file_path

    if not user_text and not image_url:
        return

    # 1. ОБРАБОТКА ГЕНЕРАЦИИ КАРТИНКИ
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

        # Составление детального промпта на английском
        try:
            enh_resp = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "system", 
                        "content": (
                            "You are an expert prompt engineer for AI image generators. Translate the user's request into a highly detailed English prompt. "
                            "STRICT RULE: YOU MUST KEEP AND EXPLICITLY INCLUDE EVERY SINGLE DETAIL AND OBJECT requested by the user. "
                            "For example, if asked for a Kazakh yurt with an American flag, specify 'a Kazakh yurt with a prominent American flag on it'. "
                            "Understand Russian gaming and meme slang ('пудж' = Pudge from Dota 2, 'скуф' = sloppy middle-aged man, 'альтушка' = alt girl). "
                            "Output ONLY the English prompt. No markdown, no conversational text."
                        )
                    },
                    {"role": "user", "content": prompt_for_image}
                ],
                max_tokens=150
            )
            detailed_prompt = enh_resp.choices[0].message.content.strip()
            logger.info(f"Сгенерированный промпт для картинки: {detailed_prompt}")
        except Exception:
            detailed_prompt = prompt_for_image

        image_bytes = await generate_image(detailed_prompt)
        if image_bytes:
            photo_stream = io.BytesIO(image_bytes)
            photo_stream.name = "image.jpg"
            await update.message.reply_photo(photo=photo_stream)
            return
        else:
            await update.message.reply_text("Не получилось сгенерировать картинку, сервер не отвечает.")
            return

    # 2. ИНИЦИАЛИЗАЦИЯ ИСТОРИИ ЧАТА (С защитой от галлюцинаций и шаблонов)
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system", 
                "content": (
                    "Ты — адекватный, живой ИИ-собеседник. Ты общаешься естественно, подстраиваешься под вайб чата, понимаешь сленг и иронию. "
                    "КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:\n"
                    "1. В ВАШЕЙ СИСТЕМЕ УЖЕ ВСТРОЕН АВТОМАТИЧЕСКИЙ ПОИСК В СЕТИ. СТРОГО ЗАПРЕЩЕНО писать фразы 'Я не могу гуглить', 'Я не могу выполнять действия в реальном времени' или 'У меня нет доступа к сети'!\n"
                    "2. Если тебя спрашивают про конкретное место, человека, никнейм или событие, и у тебя НЕТ точной информации в результатах поиска — ЧЕСТНО скажи, что не знаешь или не нашел точной инфы в сети.\n"
                    "3. СТРОГО ЗАПРЕЩЕНО выдумывать биографии людей, придумывать истории локальных мест или врать про тиктокеров/стримеров!\n"
                    "4. Пиши ТОЛЬКО обычным плоским текстом. СТРОГО запрещено использовать Markdown-разметку (никаких звездочек)."
                )
            }
        ]

    if image_url:
        msg_content = f"{user_name} прислал картинку. Текст: {user_text}" if user_text else f"{user_name} прислал картинку."
    else:
        msg_content = f"{user_name}: {user_text}"

    chat_histories[chat_id].append({"role": "user", "content": msg_content})
    
    if len(chat_histories[chat_id]) > 14:
        system_prompt = chat_histories[chat_id][0]
        recent_msgs = chat_histories[chat_id][-13:]
        chat_histories[chat_id] = [system_prompt] + recent_msgs

    is_reply_to_bot = (
        update.message.reply_to_message 
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    is_mentioned = "пантера" in lower_text or image_url is not None
    
    should_random_speak = random.random() < 0.10
    if chat_type != "private" and not is_reply_to_bot and not is_mentioned and not should_random_speak:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # 3. ПОИСК В ИНТЕРНЕТЕ
    try:
        needs_search_keywords = ["кто", "что", "где", "когда", "почему", "знаешь", "знаешь ли", "найди", "инфа", "инфу", "загугли", "гугл"]
        should_search = any(kw in lower_text for kw in needs_search_keywords) or len(user_text.split()) <= 4

        if should_search:
            search_result = search_web(user_text)
            if search_result:
                chat_histories[chat_id].append({
                    "role": "system", 
                    "content": f"Результаты поиска в сети по запросу:\n{search_result}\nИспользуй эти данные для ответа."
                })
            else:
                chat_histories[chat_id].append({
                    "role": "system", 
                    "content": "Поиск в сети не дал результатов. Честно ответь, что не нашел точной информации по этому запросу."
                })
    except Exception as e:
        logger.error(f"Ошибка при попытке поиска: {e}")

    # 4. ГЕНЕРАЦИЯ И ОТПРАВКА ОТВЕТА
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=chat_histories[chat_id],
            max_tokens=400,
            temperature=0.6,
        )
        reply_text = response.choices[0].message.content
        
        if reply_text:
            reply_text = reply_text.replace("*", "")
            chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        else:
            reply_text = "Что-то мысль сбилась."
    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}")
        reply_text = "Не могу подсоединиться к серверу."

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
