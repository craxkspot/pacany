import os
import random
import logging
import io
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
    """Генерация картинок через Pollinations"""
    encoded_prompt = urllib.parse.quote(prompt)
    seed = random.randint(1, 1000000)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&nologo=true"
    
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        try:
            response = await client.get(image_url)
            if response.status_code == 200:
                return response.content
        except Exception as e:
            logger.error(f"Ошибка генерации картинки: {e}")
    return None


def search_web(query: str) -> str:
    """Поиск в интернете через DuckDuckGo"""
    try:
        with DDGS() as ddgs:
            results = [r.get('body', '') for r in ddgs.text(query, max_results=3)]
            if results:
                return "\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
    return ""


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name or "Чел"
    chat_type = update.message.chat.type

    user_text = update.message.text or update.message.caption or ""
    lower_text = user_text.lower()

    image_url = None
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        image_url = photo_file.file_path

    if not user_text and not image_url:
        return

    # Обработка команд генерации картинок
    image_triggers = ["нарисуй", "сделай картинку", "сгенерируй", "создай изображение"]
    if any(kw in lower_text for kw in image_triggers):
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        
        prompt_for_image = user_text
        for kw in image_triggers:
            prompt_for_image = prompt_for_image.replace(kw, "").strip()
        if not prompt_for_image:
            prompt_for_image = user_text

        try:
            enh_resp = groq_client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": "Translate and expand this image prompt into a detailed English prompt for an image generator. Output ONLY the English prompt."},
                    {"role": "user", "content": prompt_for_image}
                ],
                max_tokens=100
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
            await update.message.reply_text("Не получилось сгенерировать картинку.")
            return

    # ЖИВОЙ СИСТЕМНЫЙ ПРОМПТ (возвращаем свободу мысли и стиль общения)
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system", 
                "content": (
                    "Ты — крутой, расслабленный собеседник и товарищ по чату. Общайся естественно, живо, поддерживай беседу с интересом, "
                    "улавливай вайб, подмечай детали и рассуждай так, как общался бы умный живой человек. "
                    "Не скатывайся в роботоподобные отчеты и сухие ответы. Разрешено ирония, размышления вслух, ассоциации и свой стиль. "
                    "НИКОГДА не используй символы разметки вроде звездочек (** или *) для выделения текста — пиши обычным плоским текстом."
                )
            }
        ]

    # Формируем контент для истории
    if image_url:
        msg_content = f"{user_name} прислал картинку. Текст: {user_text}" if user_text else f"{user_name} прислал картинку."
    else:
        msg_content = f"{user_name}: {user_text}"

    chat_histories[chat_id].append({"role": "user", "content": msg_content})
    
    # Нормальная длина памяти, чтобы диалог не обрывался на полуслове
    if len(chat_histories[chat_id]) > 14:
        chat_histories[chat_id] = [chat_histories[chat_id][0]] + chat_histories[chat_id][-13:]

    is_reply_to_bot = (
        update.message.reply_to_message 
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    is_mentioned = "пантера" in lower_text or image_url is not None
    
    should_random_speak = random.random() < 0.10
    if chat_type != "private" and not is_reply_to_bot and not is_mentioned and not should_random_speak:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        check_resp = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Does this message require looking up real-time info, news, facts, or specific people/entities on the web? Answer ONLY 'YES' or 'NO'."},
                {"role": "user", "content": user_text}
            ],
            max_tokens=5
        ]
        decision = check_resp.choices[0].message.content.strip().upper()
        if "YES" in decision:
            search_result = search_web(user_text)
            if search_result:
                chat_histories[chat_id].append({
                    "role": "system", 
                    "content": f"Информация из интернета по запросу:\n{search_result}"
                })
    except Exception:
        pass

    # Возвращаем адекватную температуру (0.75), чтобы у текста появился характер, глубина и стиль
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=chat_histories[chat_id],
            max_tokens=400,
            temperature=0.75, 
        )
        reply_text = response.choices[0].message.content
        
        if reply_text:
            chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        else:
            reply_text = "тут"
    except Exception as e:
        logger.error(f"Groq error: {e}")
        reply_text = "ошибка сети"

    try:
        await update.message.reply_text(reply_text)
    except Exception as e:
        logger.error(f"Telegram send error: {e}")


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Токены не заданы!")
        return

    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))
    application.run_polling()


if __name__ == "__main__":
    main()
