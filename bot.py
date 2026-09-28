import os
import random
import asyncio
import logging
import io
import urllib.parse
import httpx
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from telegram import Update
from telegram.constants import ParseMode
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

# Клиент Groq для текстов и зрения
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
        self.wfile.write(b"Ultimate Panther Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


async def generate_free_image(prompt: str) -> bytes | None:
    """Генерирует картинку бесплатно через Pollinations AI"""
    encoded_prompt = urllib.parse.quote(prompt)
    seed = random.randint(1, 1000000)
    image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&seed={seed}&nologo=true"
    
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        try:
            response = await client.get(image_url)
            if response.status_code == 200:
                return response.content
            else:
                logger.error(f"Ошибка генератора картинок: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"Исключение при запросе картинки: {e}")
            return None


def search_web(query: str) -> str:
    """Ищет информацию в интернете через DuckDuckGo"""
    try:
        with DDGS() as ddgs:
            results = [r.get('body', '') for r in ddgs.text(query, max_results=3)]
            if results:
                return "\n".join(results)
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
    return "Ничего свежего в сети не нашлось."


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name or "Друг"
    chat_type = update.message.chat.type

    user_text = update.message.text or update.message.caption or ""
    lower_text = user_text.lower()

    image_url = None
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        image_url = photo_file.file_path

    if not user_text and not image_url:
        return

    # 1. Проверяем, просит ли пользователь нарисовать что-то конкретное
    is_image_request = any(kw in lower_text for kw in ["нарисуй", "сделай картинку", "сгенерируй", "создай изображение"])

    if is_image_request:
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        
        prompt_for_image = user_text
        for kw in ["нарисуй", "сделай картинку", "сгенерируй", "создай изображение"]:
            prompt_for_image = prompt_for_image.replace(kw, "").strip()
        if not prompt_for_image:
            prompt_for_image = user_text

        image_bytes = await generate_free_image(prompt_for_image)
        
        if image_bytes:
            photo_stream = io.BytesIO(image_bytes)
            photo_stream.name = "generated_image.jpg"
            await update.message.reply_photo(photo=photo_stream, caption="Держи.")
            return
        else:
            await update.message.reply_text("Генератор картинок приболел, попробуй еще раз через секунду.")
            return

    # 2. Инициализация памяти чата
    if chat_id not in chat_histories:
        chat_histories[chat_id] = [
            {
                "role": "system", 
                "content": (
                    "Ты — Пантера, крутой, гибкий и смышленый ИИ-компаньон в Telegram. "
                    "Ты умеешь подстраиваться под стиль собеседника, общаешься непринужденно, с легким юмором или сарказмом, но без лишней злости. "
                    "Если просят убрать смайлики — не пиши их совсем. "
                    "У тебя есть доступ к интернету через поиск, поэтому если спрашивают свежие новости, треки или актуальную инфу — ты можешь её использовать. "
                    "Ты умеешь смотреть присланные картинки и оценивать их."
                )
            }
        ]

    if image_url:
        user_content = [
            {"type": "text", "text": f"{user_name} прислал(а) картинку с подписью: '{user_text}'" if user_text else f"{user_name} прислал(а) картинку. Оцени."},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]
    else:
        user_content = f"{user_name}: {user_text}"

    chat_histories[chat_id].append({"role": "user", "content": user_content})
    
    if len(chat_histories[chat_id]) > 12:
        chat_histories[chat_id] = [chat_histories[chat_id][0]] + chat_histories[chat_id][-11:]

    is_reply_to_bot = (
        update.message.reply_to_message 
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    is_mentioned = "пантера" in lower_text or image_url is not None
    
    # 3. Рандомный вброс: текст или рандомная картинка ради прикола
    should_random_speak = random.random() < 0.12
    should_random_image = random.random() < 0.03  # 3% шанс выдать картинку-сюрприз

    if chat_type != "private" and not is_reply_to_bot and not is_mentioned and not should_random_speak:
        return

    # Рандомная генерация картинки ради прикола в группе
    if should_random_image and chat_type != "private":
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        fun_prompts = [
            "cinematic dramatic scene, intense atmosphere, moody lighting",
            "cyberpunk panther sitting in a neon-lit alley, highly detailed",
            "epic movie still, cinematic tension, dramatic atmosphere"
        ]
        image_bytes = await generate_free_image(random.choice(fun_prompts))
        if image_bytes:
            photo_stream = io.BytesIO(image_bytes)
            photo_stream.name = "surprise.jpg"
            await context.bot.send_message(chat_id=chat_id, text="Вкину картинку просто ради прикола.")
            await context.bot.send_photo(chat_id=chat_id, photo=photo_stream)
            return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Если вопрос выглядит так, будто требует свежей инфы из интернета — делаем поиск
    search_keywords = ["кто такой", "что за", "трек", "песня", "новости", "последни", "свеж", "когда вышел", "почему", "что случилось"]
    if any(kw in lower_text for kw in search_keywords) and len(user_text) > 4:
        search_result = search_web(user_text)
        if search_result:
            chat_histories[chat_id].append({
                "role": "system", 
                "content": f"Результаты поиска в интернете по запросу пользователя:\n{search_result}"
            })

    is_direct_appeal = (chat_type == "private") or is_reply_to_bot or is_mentioned or (image_url is not None)

    prompt_mode_instruction = (
        " Инструкция: Напиши короткую реплику в чат, ни к кому не обращаясь."
        if not is_direct_appeal 
        else " Инструкция: Ответь емко автору сообщения или прокомментируй картинку/информацию."
    )

    temp_messages = chat_histories[chat_id].copy()
    temp_messages.append({"role": "system", "content": prompt_mode_instruction})

    reply_text = None
    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=temp_messages,
            max_tokens=600,
            temperature=0.75,
        )
        reply_text = response.choices[0].message.content
        
        if reply_text:
            chat_histories[chat_id].append({"role": "assistant", "content": reply_text})
        else:
            reply_text = "На связи."

    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}")
        reply_text = "Сеть моргнула, повтори."

    try:
        if is_direct_appeal:
            await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        if is_direct_appeal:
            await update.message.reply_text(reply_text)
        else:
            await context.bot.send_message(chat_id=chat_id, text=reply_text)


def main():
    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        logger.error("Не заданы TELEGRAM_TOKEN или GROQ_API_KEY!")
        return

    server_thread = Thread(target=run_web_server, daemon=True)
    server_thread.start()

    logger.info(f"Прокачанная Пантера запущена. Модель: {GROQ_MODEL}")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))

    application.run_polling()


if __name__ == "__main__":
    main()
