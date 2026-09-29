import os
import re
import io
import time
import base64
import random
import logging
import threading
import urllib.parse
from collections import OrderedDict, deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)
from openai import OpenAI
import requests
from PIL import Image


# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    force=True,
)
logger = logging.getLogger(__name__)

# Защита от decompression bomb
Image.MAX_IMAGE_PIXELS = 50_000_000


# --- ОКРУЖЕНИЕ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
API_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
MASTER_USERNAME = "muctep_kpunep"

client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=API_KEY,
        timeout=45.0,
        max_retries=0,
    )
    if API_KEY
    else None
)


# --- ПАМЯТЬ ЧАТОВ (LRU + TTL + per-chat lock) ---
MAX_CHATS = 300
CHAT_HISTORY_SIZE = 40
CHAT_TTL_SECONDS = 6 * 60 * 60
MAX_IMAGE_SIDE = 1280

chat_histories: "OrderedDict[int, deque]" = OrderedDict()
chat_last_seen: dict = {}
chat_locks: dict = {}
_locks_guard = threading.Lock()


def _get_lock(chat_id: int) -> threading.Lock:
    with _locks_guard:
        lk = chat_locks.get(chat_id)
        if lk is None:
            lk = threading.Lock()
            chat_locks[chat_id] = lk
        return lk


def _evict_stale() -> None:
    now = time.time()
    dead = [cid for cid, ts in chat_last_seen.items() if now - ts > CHAT_TTL_SECONDS]
    for cid in dead:
        chat_histories.pop(cid, None)
        chat_last_seen.pop(cid, None)
        chat_locks.pop(cid, None)


def get_history(chat_id: int) -> deque:
    _evict_stale()
    if chat_id in chat_histories:
        chat_histories.move_to_end(chat_id)
    else:
        chat_histories[chat_id] = deque(maxlen=CHAT_HISTORY_SIZE)
        if len(chat_histories) > MAX_CHATS:
            old_id, _ = chat_histories.popitem(last=False)
            chat_last_seen.pop(old_id, None)
            chat_locks.pop(old_id, None)
    chat_last_seen[chat_id] = time.time()
    return chat_histories[chat_id]


def reset_history(chat_id: int) -> None:
    chat_histories.pop(chat_id, None)
    chat_last_seen.pop(chat_id, None)


# --- ФИЛЬТР МУСОРА В ОТВЕТЕ ---
BANNED_WORDS_RE = re.compile(
    r"\b(чож|чо\b|чот|валер\b|валера\b|валерон|дружище|братан|бро\b)",
    re.IGNORECASE,
)
MARKDOWN_JUNK_RE = re.compile(r"[*_`#]+")


def sanitize_reply(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    text = MARKDOWN_JUNK_RE.sub("", text)
    text = BANNED_WORDS_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    # убираем висящие запятые/точки с пробелами
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    # первая буква строчная, если это буква
    if text and text[0].isalpha() and text[0].isupper():
        text = text[0].lower() + text[1:]
    # финальная точка не нужна
    if text.endswith("."):
        text = text[:-1]
    return text.strip()


# --- ПУЛЫ МОДЕЛЕЙ ---
DEFAULT_TEXT_MODELS = [
    "meta-llama/llama-3.1-8b-instruct:free",
    "google/gemma-2-9b-it:free",
    "mistralai/mistral-7b-instruct:free",
    "deepseek/deepseek-chat:free",
]
DEFAULT_VISION_MODELS = [
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "qwen/qwen-2-vl-7b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
]


def get_model_pools():
    if not client:
        return DEFAULT_TEXT_MODELS, DEFAULT_VISION_MODELS

    try:
        logger.info("Сканируем бесплатные модели OpenRouter...")
        models_response = client.models.list()
        ids = []
        for m in models_response.data:
            mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
            if mid:
                ids.append(str(mid))

        free_models = [
            mid
            for mid in ids
            if ":free" in mid.lower()
            and not any(w in mid.lower() for w in ["embed", "tts", "audio", "guard", "moderation"])
        ]

        vision_kw = ["vision", "vl", "pixtral", "gemini", "flash", "multimodal"]
        vision_models = [m for m in free_models if any(k in m.lower() for k in vision_kw)]
        text_models = [m for m in free_models if m not in vision_models]

        if not vision_models:
            vision_models = DEFAULT_VISION_MODELS
        if not text_models:
            text_models = DEFAULT_TEXT_MODELS

        logger.info(
            f"Модели: текстовых {len(text_models)}, зрячих {len(vision_models)}"
        )
        return text_models[:15], vision_models[:15]

    except Exception as e:
        logger.error(f"Автоподбор моделей упал: {e}")
        return DEFAULT_TEXT_MODELS, DEFAULT_VISION_MODELS


TEXT_POOL, VISION_POOL = get_model_pools()
PRIMARY_TEXT_MODEL = TEXT_POOL[0] if TEXT_POOL else DEFAULT_TEXT_MODELS[0]
PRIMARY_VISION_MODEL = VISION_POOL[0] if VISION_POOL else DEFAULT_VISION_MODELS[0]


# --- ВЕБ-ПОИСК ---
SEARCH_TRIGGER_RE = re.compile(
    r"^\s*(найди|поищи|погугли|загугли|search|найти)\b",
    re.IGNORECASE,
)


def search_web(query: str) -> str:
    logger.info(f"Поиск: {query}")
    try:
        if TAVILY_API_KEY:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": TAVILY_API_KEY, "query": query, "max_results": 3},
                timeout=6,
            )
            data = resp.json()
            chunks = [r.get("content", "") for r in data.get("results", []) if r.get("content")]
            if chunks:
                return "\n".join(chunks)[:1500]

        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json"
        resp = requests.get(url, timeout=6)
        data = resp.json()
        abstract = data.get("AbstractText")
        if abstract:
            return abstract
        for topic in data.get("RelatedTopics", []):
            if isinstance(topic, dict) and "Text" in topic:
                return topic["Text"]
        return ""
    except Exception as e:
        logger.warning(f"Поиск не удался: {e}")
        return ""


# --- ВЕБ-СЕРВЕР ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Valera bot is live")

    def log_message(self, *args):
        return


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()


# --- СИСТЕМНЫЙ ПРОМПТ ---
def build_system_prompt(extra_facts: str = "") -> str:
    base = (
        f"Ты — Валера, язвительный и циничный участник чата. Твой создатель — @{MASTER_USERNAME}.\n\n"
        "ЖЁСТКИЕ ПРАВИЛА:\n"
        "1. Отвечай КОРОТКО — 1–2 предложения, максимум 3. Не растекайся.\n"
        "2. Реагируй на КОНКРЕТНОЕ сообщение собеседника. Не выдумывай то, чего не было.\n"
        "3. Не пиши общих рассуждений про жизнь, не философствуй, не задавай встречных вопросов.\n"
        "4. Опирайся на контекст выше, но не пересказывай его.\n"
        "5. Не используй markdown: никаких звёздочек, решёток, списков.\n"
        "6. Пиши строчными буквами, без точки в конце.\n"
        "7. НИКОГДА не пиши слова: чож, чо, чот, валер, валера, валерон, дружище, братан, бро.\n"
        "8. Если фактов нет — не выдумывай их, просто подколи по существу сказанного.\n"
        f"9. С @{MASTER_USERNAME} общайся с особой едкостью, но признавай его авторитет.\n"
        "10. Остальных стеби по фактам из их сообщения. Без перехода на личности, внешность, национальность."
    )
    if extra_facts:
        base += f"\n\nФАКТЫ ИЗ ИНТЕРНЕТА (используй, если релевантно):\n{extra_facts}"
    return base


# --- ГЕНЕРАЦИЯ ТЕКСТА ---
async def generate_text_reply(chat_id: int, user_message_for_history: str, prompt_for_model: str) -> str:
    if not client:
        return "нет api-ключа, я туплю"

    extra_facts = ""
    if SEARCH_TRIGGER_RE.match(prompt_for_model):
        extra_facts = search_web(prompt_for_model)

    system_prompt = build_system_prompt(extra_facts)

    lock = _get_lock(chat_id)
    with lock:
        history = list(get_history(chat_id))

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt_for_model})

    last_error = None
    for model_name in TEXT_POOL:
        try:
            logger.info(f"[текст] пробуем {model_name}")
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                max_tokens=220,
                temperature=0.85,
                top_p=0.9,
            )
            choice = response.choices[0]
            reply_raw = (choice.message.content or "") if choice.message else ""
            reply = sanitize_reply(reply_raw)
            if not reply:
                logger.info(f"[текст] {model_name} дал пустой ответ")
                continue

            with lock:
                hist = get_history(chat_id)
                hist.append({"role": "user", "content": user_message_for_history})
                hist.append({"role": "assistant", "content": reply})

            logger.info(f"[текст] ок через {model_name}")
            return reply

        except Exception as e:
            last_error = e
            logger.warning(f"[текст] {model_name} упала: {e}")
            continue

    logger.error(f"[текст] все модели упали. Последняя ошибка: {last_error}")
    return ""


# --- ГЕНЕРАЦИЯ ПО КАРТИНКАМ ---
def _prepare_image(image_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if max(img.size) > MAX_IMAGE_SIDE:
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def generate_image_reply(
    chat_id: int,
    user_message_for_history: str,
    caption: str,
    media_type: str,
    image_bytes: bytes,
) -> str:
    if not client:
        return "нет api-ключа, я туплю"

    try:
        b64 = _prepare_image(image_bytes)
    except Exception as e:
        logger.warning(f"Не смог подготовить картинку: {e}")
        return "картинка битая, даже смотреть не буду"

    user_prompt_text = caption.strip() if caption and caption.strip() else f"что тут на этом {media_type}?"

    system_prompt = (
        f"Ты — Валера, язвительный участник чата. Твой создатель — @{MASTER_USERNAME}.\n"
        "Тебе дали изображение. Посмотри на него внимательно и скажи по нему 1–2 коротких едких предложения.\n"
        "ПРАВИЛА:\n"
        "1. Обязательно опиши, что реально видно (объект, люди, текст, сцена) — своими словами, коротко.\n"
        "2. Стеби по существу увиденного, без выдумок и абстракций.\n"
        "3. Пиши строчными буквами, без markdown, без точки в конце.\n"
        "4. НИКОГДА не пиши слова: чож, чо, чот, валер, валера, валерон, дружище, братан, бро."
    )

    lock = _get_lock(chat_id)
    with lock:
        history = list(get_history(chat_id))

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        }
    )

    for model_name in VISION_POOL:
        try:
            logger.info(f"[зрение] пробуем {model_name}")
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                max_tokens=220,
                temperature=0.85,
            )
            choice = response.choices[0]
            reply_raw = (choice.message.content or "") if choice.message else ""
            reply = sanitize_reply(reply_raw)
            if not reply:
                continue

            with lock:
                hist = get_history(chat_id)
                hist.append({"role": "user", "content": user_message_for_history})
                hist.append({"role": "assistant", "content": reply})

            logger.info(f"[зрение] ок через {model_name}")
            return reply

        except Exception as e:
            logger.warning(f"[зрение] {model_name} упала: {e}")
            continue

    logger.warning("[зрение] все зрячие модели упали, откат на текст")
    return await generate_text_reply(
        chat_id,
        user_message_for_history,
        f"[пользователь скинул {media_type}, зрение не работает. Скажи что-нибудь едкое по факту того, что он скинул]",
    )


# --- КОМАНДЫ ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    status = (
        "🤖 Валера на связи\n"
        f"• папочка: {MASTER_USERNAME}\n"
        f"• текстовых моделей: {len(TEXT_POOL)}\n"
        f"• зрячих моделей: {len(VISION_POOL)}\n"
        f"• активное зрение: {PRIMARY_VISION_MODEL}"
    )
    await update.message.reply_text(status)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    reset_history(chat_id)
    await update.message.reply_text("память по этому чату очищена")


# --- ГЛАВНЫЙ ОБРАБОТЧИК ---
MENTION_RE = re.compile(r"\b(валер\w*|валерон\w*)", re.IGNORECASE)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return
    if update.message.from_user.is_bot:
        return

    chat = update.effective_chat
    chat_id = chat.id
    user = update.message.from_user
    username = user.username or user.first_name or "anon"
    text = (update.message.text or update.message.caption or "").strip()

    is_private = chat.type == "private"
    is_reply_to_bot = bool(
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    is_addressed = bool(MENTION_RE.search(text))
    is_random = (not is_private) and random.random() < 0.07

    should_reply = is_private or is_addressed or is_reply_to_bot or is_random

    logger.info(
        f"msg chat={chat_id} user={username} private={is_private} "
        f"addressed={is_addressed} reply_to_bot={is_reply_to_bot} random={is_random} "
        f"-> reply={should_reply} text='{text[:40]}'"
    )

    # Если не отвечаем — просто записываем в память для контекста
    if not should_reply:
        with _get_lock(chat_id):
            hist = get_history(chat_id)
            if text:
                hist.append({"role": "user", "content": f"[{username}]: {text}"})
            elif any(
                [
                    update.message.photo,
                    update.message.sticker,
                    update.message.video,
                    update.message.voice,
                    update.message.animation,
                    update.message.video_note,
                    update.message.audio,
                ]
            ):
                hist.append({"role": "user", "content": f"[{username} скинул медиа]"})
        return

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:
        pass

    reply_text = ""

    try:
        if update.message.photo:
            f = await update.message.photo[-1].get_file()
            b = await f.download_as_bytearray()
            reply_text = await generate_image_reply(
                chat_id,
                user_message_for_history=f"[{username} скинул фото]: {text}" if text else f"[{username} скинул фото]",
                caption=text,
                media_type="фото",
                image_bytes=bytes(b),
            )

        elif update.message.sticker:
            if update.message.sticker.is_animated or update.message.sticker.is_video:
                reply_text = await generate_text_reply(
                    chat_id,
                    user_message_for_history=f"[{username} скинул анимированный стикер]",
                    prompt_for_model=f"[{username} скинул анимированный стикер, посмейся над этим фактом коротко]",
                )
            else:
                f = await update.message.sticker.get_file()
                b = await f.download_as_bytearray()
                reply_text = await generate_image_reply(
                    chat_id,
                    user_message_for_history=f"[{username} скинул стикер]",
                    caption=text or "",
                    media_type="стикер",
                    image_bytes=bytes(b),
                )

        elif update.message.animation or update.message.video:
            kind = "гифку" if update.message.animation else "видео"
            reply_text = await generate_text_reply(
                chat_id,
                user_message_for_history=f"[{username} скинул {kind}]: {text}" if text else f"[{username} скинул {kind}]",
                prompt_for_model=f"[{username} скинул {kind}. Скажи по этому поводу одну короткую едкую фразу]",
            )

        elif update.message.voice or update.message.video_note or update.message.audio:
            kind = "голосовуху" if update.message.voice else ("кружок" if update.message.video_note else "аудио")
            reply_text = await generate_text_reply(
                chat_id,
                user_message_for_history=f"[{username} скинул {kind}]",
                prompt_for_model=f"[{username} скинул {kind}. Одна короткая едкая фраза по этому поводу]",
            )

        else:
            reply_text = await generate_text_reply(
                chat_id,
                user_message_for_history=f"[{username}]: {text}",
                prompt_for_model=f"[{username}]: {text}",
            )

    except Exception as e:
        logger.error(f"Глобальная ошибка обработки сообщения: {e}", exc_info=True)
        reply_text = ""

    if reply_text:
        try:
            await update.message.reply_text(reply_text)
        except Exception as e:
            logger.error(f"Не смог отправить ответ: {e}")


# --- СТАРТ ---
def print_startup_status() -> None:
    logger.info(
        "\n"
        "┌──────────────────────────────────────────────────────────┐\n"
        "│                   ЗАПУСК ВАЛЕРЫ                          │\n"
        "├────────────────────┬─────────────────────────────────────┤\n"
        f"│ Основной текст     │ {PRIMARY_TEXT_MODEL:<35} │\n"
        f"│ Основное зрение    │ {PRIMARY_VISION_MODEL:<35} │\n"
        f"│ Текстовый пул      │ {len(TEXT_POOL):<35} │\n"
        f"│ Зрячий пул         │ {len(VISION_POOL):<35} │\n"
        "└────────────────────┴─────────────────────────────────────┘"
    )


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан")
    if not API_KEY:
        logger.warning("API_KEY не задан — бот не сможет генерировать ответы")

    Thread(target=run_web_server, daemon=True).start()
    print_startup_status()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_message))

    logger.info("Валера запущен")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
