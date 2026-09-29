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

Image.MAX_IMAGE_PIXELS = 50_000_000


# --- ОКРУЖЕНИЕ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
MASTER_USERNAME = "muctep_kpunep"

# Основной клиент — OpenRouter (для текста)
client = (
    OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
        timeout=45.0,
        max_retries=0,
    )
    if OPENROUTER_API_KEY
    else None
)

# Второй клиент — Groq (для зрения, если есть ключ). Он надёжнее шаред-пула OpenRouter.
groq_client = (
    OpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=GROQ_API_KEY,
        timeout=45.0,
        max_retries=0,
    )
    if GROQ_API_KEY
    else None
)

GROQ_VISION_MODEL = "llama-3.2-11b-vision-preview"


# --- ПАМЯТЬ ЧАТОВ ---
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


def _append_history(chat_id: int, role: str, content: str) -> None:
    """Склеивает соседние сообщения одной роли, чтобы не ловить 400 alternation error."""
    hist = get_history(chat_id)
    if hist and hist[-1].get("role") == role:
        hist[-1]["content"] = (hist[-1]["content"] + "\n" + content).strip()
    else:
        hist.append({"role": role, "content": content})


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
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    if text and text[0].isalpha() and text[0].isupper():
        text = text[0].lower() + text[1:]
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

# Отдельный явный whitelist реально зрячих бесплатных моделей OpenRouter.
# Проверено практикой; добавляй только те, что действительно принимают image.
EXPLICIT_VISION_WHITELIST = {
    "google/gemini-2.0-flash-exp:free",
    "google/gemini-2.0-flash-thinking-exp:free",
    "meta-llama/llama-3.2-11b-vision-instruct:free",
    "qwen/qwen-2-vl-7b-instruct:free",
    "qwen/qwen-2.5-vl-7b-instruct:free",
}

# Мусор по имени, который никогда не должен попасть ни в один пул.
UTILITY_NAME_MARKERS = [
    "embed", "tts", "audio", "whisper", "rerank", "reward",
    "guard", "moderation", "content-safety", "safety",
    "thinkingmachines/inkling",  # agentic-only, всегда 403
]

# Рантайм-блэклист: сюда попадают модели, провалившиеся навсегда
# (403 agentic, 404 no image support).
VISION_PERMABLACKLIST: set = set()
TEXT_PERMABLACKLIST: set = set()

# Cooldown для 429: не пробуем модель в течение N секунд после rate-limit.
RATE_LIMIT_COOLDOWN_SEC = 90
_rate_limited_until: dict = {}


def _in_cooldown(model_id: str) -> bool:
    until = _rate_limited_until.get(model_id, 0)
    return time.time() < until


def _mark_rate_limited(model_id: str) -> None:
    _rate_limited_until[model_id] = time.time() + RATE_LIMIT_COOLDOWN_SEC


def _is_utility_name(mid: str) -> bool:
    low = mid.lower()
    return any(w in low for w in UTILITY_NAME_MARKERS)


def _fetch_openrouter_models() -> list:
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Не смог получить список моделей OpenRouter: {e}")
        return []


def get_model_pools():
    data = _fetch_openrouter_models()

    if not data:
        logger.warning("OpenRouter не отдал модели, работаю на дефолтах")
        return DEFAULT_TEXT_MODELS, list(EXPLICIT_VISION_WHITELIST)

    text_models = []
    vision_models = []

    for m in data:
        mid = m.get("id")
        if not mid or ":free" not in mid.lower():
            continue
        if _is_utility_name(mid):
            continue
        if mid in VISION_PERMABLACKLIST or mid in TEXT_PERMABLACKLIST:
            continue

        # Зрячими считаем ТОЛЬКО те, что в явном whitelist.
        # Поле architecture.input_modalities на практике врёт: провайдеры
        # возвращают 404 "no image support" для моделей с флагом image.
        if mid in EXPLICIT_VISION_WHITELIST:
            vision_models.append(mid)
        else:
            text_models.append(mid)

    if not text_models:
        text_models = DEFAULT_TEXT_MODELS
    if not vision_models:
        vision_models = list(EXPLICIT_VISION_WHITELIST)

    logger.info(
        f"Модели: текстовых {len(text_models)}, зрячих {len(vision_models)} "
        f"(OpenRouter whitelist)"
    )
    logger.info(f"Зрячие: {vision_models}")

    return text_models[:20], vision_models[:20]


TEXT_POOL, VISION_POOL = get_model_pools()
PRIMARY_TEXT_MODEL = TEXT_POOL[0] if TEXT_POOL else DEFAULT_TEXT_MODELS[0]


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


# --- ПРОМПТЫ ---
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


VISION_SYSTEM_PROMPT = (
    f"Ты — Валера, язвительный участник чата. Твой создатель — @{MASTER_USERNAME}.\n"
    "Тебе дали изображение. Посмотри на него внимательно и скажи по нему 1–2 коротких едких предложения.\n"
    "ПРАВИЛА:\n"
    "1. Обязательно опиши, что реально видно (объект, люди, текст, сцена) — своими словами, коротко.\n"
    "2. Стеби по существу увиденного, без выдумок и абстракций.\n"
    "3. Пиши строчными буквами, без markdown, без точки в конце.\n"
    "4. НИКОГДА не пиши слова: чож, чо, чот, валер, валера, валерон, дружище, братан, бро."
)


# --- ХЕЛПЕРЫ ДЛЯ РАБОТЫ С ОТВЕТАМИ МОДЕЛЕЙ ---
def _extract_reply(response) -> str:
    """Безопасно вытаскивает текст ответа. Возвращает '' при любой аномалии."""
    if response is None:
        return ""
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    first = choices[0]
    msg = getattr(first, "message", None)
    if msg is None:
        return ""
    content = getattr(msg, "content", None)
    if not isinstance(content, str):
        return ""
    return content


def _is_permanent_failure(e: Exception) -> str:
    """
    Возвращает 'agentic' | 'no_image' | '' — тип фатальной ошибки модели.
    """
    s = str(e).lower()
    if "agentic harnesses" in s or "gate free endpoints by agentic" in s:
        return "agentic"
    if "no endpoints found that support image" in s:
        return "no_image"
    return ""


def _is_rate_limit(e: Exception) -> bool:
    s = str(e)
    return " 429 " in s or "429 Too Many" in s or "'code': 429" in s


# --- ГЕНЕРАЦИЯ ТЕКСТА ---
async def generate_text_reply(chat_id: int, user_message_for_history: str, prompt_for_model: str) -> str:
    if not client:
        return ""

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

    for model_name in list(TEXT_POOL):
        if _in_cooldown(model_name):
            logger.info(f"[текст] {model_name} в cooldown, пропускаю")
            continue
        try:
            logger.info(f"[текст] пробуем {model_name}")
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                max_tokens=220,
                temperature=0.85,
                top_p=0.9,
            )
            reply = sanitize_reply(_extract_reply(response))
            if not reply:
                logger.info(f"[текст] {model_name} дал пустой ответ")
                continue

            with lock:
                _append_history(chat_id, "user", user_message_for_history)
                _append_history(chat_id, "assistant", reply)

            logger.info(f"[текст] ок через {model_name}")
            return reply

        except Exception as e:
            kind = _is_permanent_failure(e)
            if kind:
                TEXT_PERMABLACKLIST.add(model_name)
                try:
                    TEXT_POOL.remove(model_name)
                except ValueError:
                    pass
                logger.warning(f"[текст] {model_name} — постоянный фейл ({kind}), убрал из пула")
            elif _is_rate_limit(e):
                _mark_rate_limited(model_name)
                logger.warning(f"[текст] {model_name} 429, cooldown {RATE_LIMIT_COOLDOWN_SEC}s")
            else:
                logger.warning(f"[текст] {model_name} упала: {e}")
            continue

    logger.warning("[текст] все модели провалились или в cooldown")
    return ""


# --- КАРТИНКИ ---
def _prepare_image(image_bytes: bytes) -> str:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    if max(img.size) > MAX_IMAGE_SIDE:
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def _try_vision_openrouter(messages) -> str:
    if not client:
        return ""
    for model_name in list(VISION_POOL):
        if model_name in VISION_PERMABLACKLIST:
            continue
        if _in_cooldown(model_name):
            logger.info(f"[зрение] {model_name} в cooldown, пропускаю")
            continue
        try:
            logger.info(f"[зрение] пробуем {model_name}")
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                max_tokens=220,
                temperature=0.85,
            )
            reply = sanitize_reply(_extract_reply(response))
            if not reply:
                logger.info(f"[зрение] {model_name} пусто")
                continue
            logger.info(f"[зрение] ок через {model_name}")
            return reply
        except Exception as e:
            kind = _is_permanent_failure(e)
            if kind:
                VISION_PERMABLACKLIST.add(model_name)
                try:
                    VISION_POOL.remove(model_name)
                except ValueError:
                    pass
                logger.warning(f"[зрение] {model_name} постоянно недоступна ({kind}), убрал")
            elif _is_rate_limit(e):
                _mark_rate_limited(model_name)
                logger.warning(f"[зрение] {model_name} 429, cooldown {RATE_LIMIT_COOLDOWN_SEC}s")
            else:
                logger.warning(f"[зрение] {model_name} упала: {e}")
            continue
    return ""


async def _try_vision_groq(messages) -> str:
    if not groq_client:
        return ""
    try:
        logger.info(f"[зрение] пробуем Groq {GROQ_VISION_MODEL}")
        response = groq_client.chat.completions.create(
            model=GROQ_VISION_MODEL,
            messages=messages,
            max_tokens=220,
            temperature=0.85,
        )
        reply = sanitize_reply(_extract_reply(response))
        if reply:
            logger.info("[зрение] ок через Groq")
            return reply
    except Exception as e:
        logger.warning(f"[зрение] Groq упала: {e}")
    return ""


async def generate_image_reply(
    chat_id: int,
    user_message_for_history: str,
    caption: str,
    media_type: str,
    image_bytes: bytes,
) -> str:
    if not client and not groq_client:
        return ""

    try:
        b64 = _prepare_image(image_bytes)
    except Exception as e:
        logger.warning(f"Не смог подготовить картинку: {e}")
        return ""

    user_prompt_text = caption.strip() if caption and caption.strip() else f"что тут на этом {media_type}?"

    lock = _get_lock(chat_id)
    with lock:
        history = list(get_history(chat_id))

    messages = [{"role": "system", "content": VISION_SYSTEM_PROMPT}]
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

    # 1. Пробуем Groq — он самый стабильный для бесплатного зрения.
    reply = await _try_vision_groq(messages)

    # 2. Пробуем OpenRouter whitelist.
    if not reply:
        reply = await _try_vision_openrouter(messages)

    if not reply:
        logger.warning("[зрение] ни один провайдер не ответил. Молчу.")
        return ""

    with lock:
        _append_history(chat_id, "user", user_message_for_history)
        _append_history(chat_id, "assistant", reply)
    return reply


# --- КОМАНДЫ ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    status = (
        "🤖 Валера на связи\n"
        f"• папочка: {MASTER_USERNAME}\n"
        f"• текстовых моделей: {len(TEXT_POOL)}\n"
        f"• зрячих моделей (OR whitelist): {len(VISION_POOL)}\n"
        f"• Groq vision: {'✅ ' + GROQ_VISION_MODEL if groq_client else '❌ нет ключа'}\n"
        f"• permablacklist: text={len(TEXT_PERMABLACKLIST)} vision={len(VISION_PERMABLACKLIST)}\n"
        f"• в cooldown сейчас: {sum(1 for m in _rate_limited_until if _in_cooldown(m))}"
    )
    await update.message.reply_text(status)


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    reset_history(update.effective_chat.id)
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

    if not should_reply:
        with _get_lock(chat_id):
            if text:
                _append_history(chat_id, "user", f"[{username}]: {text}")
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
                _append_history(chat_id, "user", f"[{username} скинул медиа]")
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
        f"│ Groq vision        │ {(GROQ_VISION_MODEL if groq_client else '(нет ключа)'):<35} │\n"
        f"│ Текстовый пул      │ {len(TEXT_POOL):<35} │\n"
        f"│ Зрячий пул (OR)    │ {len(VISION_POOL):<35} │\n"
        "└────────────────────┴─────────────────────────────────────┘"
    )


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан")
    if not OPENROUTER_API_KEY:
        logger.warning("OPENROUTER_API_KEY не задан — текстовые ответы работать не будут")
    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY не задан — зрения через Groq не будет")

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
