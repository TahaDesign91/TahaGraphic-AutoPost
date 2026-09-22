from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError


# ============================================================
# TAHA GRAPHIC AUTO PUBLISHER
# ============================================================
#
# Architecture:
#
#   Editorial Engine
#          ↓
#   Topic Selection
#          ↓
#   Gemini Structured Generation
#          ↓
#   Semantic / Local Quality Gate
#          ↓
#   Duplicate Protection
#          ↓
#   Telegram Health Check
#          ↓
#   Telegram Publisher
#          ↓
#   Persistent State
#
# Target:
#   @Taha_Grafic
#
# Secrets:
#   GEMINI_API_KEY
#   TELEGRAM_BOT_TOKEN
#
# Optional:
#   GEMINI_MODEL
#   DRY_RUN
#   TELEGRAM_CHANNEL
#
# ============================================================


# ============================================================
# APPLICATION METADATA
# ============================================================

APP_NAME = "Taha Graphic Auto Publisher"
APP_VERSION = "3.0.0"


# ============================================================
# DEFAULT SETTINGS
# ============================================================

DEFAULT_CHANNEL = "@Taha_Grafic"

# The model can be changed without editing the source code.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"

TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# We deliberately stay below Telegram's hard limit
# because HTML entities are used during transmission.
MAX_RENDERED_POST_LENGTH = 3400

MIN_RENDERED_POST_LENGTH = 500

MAX_AI_ATTEMPTS = 4
MAX_REPAIR_ATTEMPTS = 2

TELEGRAM_RETRIES = 4

BASE_RETRY_DELAY = 3

REQUEST_TIMEOUT = 60

HISTORY_LIMIT = 50

TOPIC_COOLDOWN = 15

BRAND_LINE = "طاها گرافیک | آموزش و ترفند طراحی 🎨"


# ============================================================
# FILE SYSTEM
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"

STATE_FILE = DATA_DIR / "state.json"

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    )
)

logger = logging.getLogger(
    "TahaGraphicAutoPublisher"
)


# ============================================================
# CONFIGURATION
# ============================================================

@dataclass(frozen=True)
class Settings:
    """
    Application configuration loaded from environment variables.
    """

    telegram_token: str
    gemini_key: str

    channel: str = DEFAULT_CHANNEL

    gemini_model: str = DEFAULT_GEMINI_MODEL

    dry_run: bool = False

    max_post_length: int = MAX_RENDERED_POST_LENGTH

    min_post_length: int = MIN_RENDERED_POST_LENGTH


def env_bool(
    name: str,
    default: bool = False
) -> bool:

    value = os.getenv(name)

    if value is None:
        return default

    return value.lower() in {
        "1",
        "true",
        "yes",
        "on"
    }


def load_settings() -> Settings:
    """
    Read secrets and configuration from environment variables.
    """

    telegram_token = os.getenv(
        "TELEGRAM_BOT_TOKEN"
    )

    gemini_key = os.getenv(
        "GEMINI_API_KEY"
    )

    if not telegram_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured."
        )

    if not gemini_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured."
        )

    return Settings(
        telegram_token=telegram_token,
        gemini_key=gemini_key,
        channel=os.getenv(
            "TELEGRAM_CHANNEL",
            DEFAULT_CHANNEL
        ),
        gemini_model=os.getenv(
            "GEMINI_MODEL",
            DEFAULT_GEMINI_MODEL
        ),
        dry_run=env_bool(
            "DRY_RUN",
            False
        )
    )


# ============================================================
# STRUCTURED GEMINI OUTPUT
# ============================================================

ContentType = Literal[
    "tutorial",
    "quick_tip",
    "checklist",
    "mistake_fix",
    "workflow",
    "pro_tip",
    "comparison",
    "challenge"
]


class GeneratedPost(BaseModel):
    """
    Structured schema for the generated content.
    """

    content_type: ContentType

    title: str = Field(
        min_length=8,
        max_length=90
    )

    hook: str = Field(
        min_length=20,
        max_length=280
    )

    main_point: str = Field(
        min_length=80,
        max_length=900
    )

    steps: list[str] = Field(
        min_length=2,
        max_length=7
    )

    pro_tip: str = Field(
        min_length=20,
        max_length=500
    )

    hashtags: list[str] = Field(
        min_length=2,
        max_length=5
    )


# ============================================================
# EDITORIAL TOPICS
# ============================================================

@dataclass(frozen=True)
class Topic:
    category: str
    title: str
    difficulty: str


TOPICS: list[Topic] = [

    Topic(
        "Photoshop",
        "ترفندهای حرفه‌ای Layer Mask",
        "متوسط"
    ),

    Topic(
        "Photoshop",
        "روش غیرمخرب برای اصلاح نور و رنگ",
        "متوسط"
    ),

    Topic(
        "Photoshop",
        "ترفندهای Smart Object",
        "پیشرفته"
    ),

    Topic(
        "Photoshop",
        "انتخاب دقیق سوژه در تصاویر پیچیده",
        "متوسط"
    ),

    Topic(
        "Photoshop",
        "ساخت سایه طبیعی برای سوژه",
        "متوسط"
    ),

    Topic(
        "Photoshop",
        "جلوه نورپردازی حرفه‌ای",
        "متوسط"
    ),

    Topic(
        "Photoshop",
        "مدیریت حرفه‌ای لایه‌ها",
        "مبتدی"
    ),

    Topic(
        "Photoshop",
        "ترفندهای کمتر شناخته‌شده Photoshop",
        "پیشرفته"
    ),

    Topic(
        "Photoshop",
        "افزایش سرعت کار با میانبرها",
        "مبتدی"
    ),

    Topic(
        "Photoshop",
        "اصلاح رنگ بدون خراب کردن تصویر اصلی",
        "متوسط"
    ),

    Topic(
        "Typography",
        "انتخاب فونت برای تیتر پوستر",
        "مبتدی"
    ),

    Topic(
        "Typography",
        "ترکیب حرفه‌ای دو فونت",
        "متوسط"
    ),

    Topic(
        "Typography",
        "کنترل فاصله حروف و کلمات",
        "متوسط"
    ),

    Topic(
        "Typography",
        "ساخت سلسله‌مراتب تایپوگرافی",
        "متوسط"
    ),

    Topic(
        "Graphic Design",
        "ساخت سلسله‌مراتب بصری",
        "متوسط"
    ),

    Topic(
        "Graphic Design",
        "استفاده حرفه‌ای از فضای خالی",
        "مبتدی"
    ),

    Topic(
        "Graphic Design",
        "چطور یک طرح شلوغ را ساده کنیم",
        "متوسط"
    ),

    Topic(
        "Graphic Design",
        "اشتباهات رایج طراحان تازه‌کار",
        "مبتدی"
    ),

    Topic(
        "Graphic Design",
        "تعادل بصری در طراحی",
        "متوسط"
    ),

    Topic(
        "Graphic Design",
        "استفاده درست از گرید",
        "متوسط"
    ),

    Topic(
        "Color",
        "انتخاب پالت رنگ حرفه‌ای",
        "مبتدی"
    ),

    Topic(
        "Color",
        "رنگ مکمل و رنگ تأکیدی",
        "متوسط"
    ),

    Topic(
        "Color",
        "کاهش شلوغی رنگی در پوستر",
        "متوسط"
    ),

    Topic(
        "Color",
        "کنترل کنتراست رنگ",
        "متوسط"
    ),

    Topic(
        "Poster",
        "جذب توجه در نگاه اول",
        "متوسط"
    ),

    Topic(
        "Poster",
        "اندازه و جایگذاری تیتر",
        "متوسط"
    ),

    Topic(
        "Poster",
        "ساخت پوستر مینیمال",
        "متوسط"
    ),

    Topic(
        "Poster",
        "چیدمان عناصر در پوستر",
        "متوسط"
    ),

    Topic(
        "Professional",
        "چک‌لیست کنترل نهایی قبل از تحویل",
        "پیشرفته"
    ),

    Topic(
        "Professional",
        "مرتب‌سازی حرفه‌ای فایل PSD",
        "متوسط"
    ),

    Topic(
        "Professional",
        "آماده‌سازی فایل برای چاپ",
        "متوسط"
    ),

    Topic(
        "Professional",
        "بررسی حرفه‌ای یک طرح قبل از انتشار",
        "پیشرفته"
    ),
]


# ============================================================
# CONTENT TYPE STRATEGY
# ============================================================

CONTENT_STYLES = {
    "tutorial": "آموزش گام‌به‌گام عمیق و قابل اجرا",

    "quick_tip": "یک ترفند سریع ولی واقعاً کاربردی",

    "checklist": "چک‌لیست حرفه‌ای و قابل استفاده",

    "mistake_fix": "یک اشتباه رایج + روش اصلاح",

    "workflow": "یک workflow حرفه‌ای و کاربردی",

    "pro_tip": "یک نکته کمتر شناخته‌شده برای طراحان",

    "comparison": "مقایسه دو روش و توضیح کاربرد هرکدام",

    "challenge": "یک چالش کوچک و آموزشی برای مخاطب"
}


# ============================================================
# FORBIDDEN CONTENT
# ============================================================

FORBIDDEN_PHRASES = [
    "به عنوان یک هوش مصنوعی",
    "من یک هوش مصنوعی هستم",
    "به عنوان مدل زبانی",
    "من نمی‌توانم",
    "من نمی توانم",
    "پرامپت",
    "API",
    "Gemini",
    "هوش مصنوعی تولید کرده",
    "امیدوارم مفید باشد",
    "در این پست می‌خواهیم",
    "امروز قصد داریم",
]


# ============================================================
# DEFAULT STATE
# ============================================================

DEFAULT_STATE = {
    "version": 1,
    "published_posts": [],
    "topics_used": [],
    "content_types_used": [],
    "last_success": None,
    "total_published": 0
}


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state() -> dict:
    """
    Read persistent state safely.
    """

    if not STATE_FILE.exists():
        return DEFAULT_STATE.copy()

    try:

        raw = STATE_FILE.read_text(
            encoding="utf-8"
        )

        state = json.loads(raw)

        if not isinstance(
            state,
            dict
        ):
            raise ValueError(
                "State must be an object."
            )

        return {
            **DEFAULT_STATE,
            **state
        }

    except Exception as exc:

        logger.warning(
            "State file could not be loaded: %s",
            exc
        )

        return DEFAULT_STATE.copy()


def atomic_write_json(
    path: Path,
    data: dict
) -> None:
    """
    Atomic JSON write.
    Prevents partial state files.
    """

    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp"
    )

    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2
            )

            file.write("\n")

        os.replace(
            temp_path,
            path
        )

    except Exception:

        try:
            os.remove(
                temp_path
            )
        except OSError:
            pass

        raise


def save_state(
    state: dict
) -> None:

    atomic_write_json(
        STATE_FILE,
        state
    )


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_text(
    value: str
) -> str:
    """
    Normalize Persian/Arabic text.
    """

    value = value.replace(
        "\r\n",
        "\n"
    )

    value = value.replace(
        "\r",
        "\n"
    )

    value = value.replace(
        "ي",
        "ی"
    )

    value = value.replace(
        "ك",
        "ک"
    )

    value = re.sub(
        r"[ \t]+",
        " ",
        value
    )

    value = re.sub(
        r"\n{3,}",
        "\n\n",
        value
    )

    return value.strip()


def normalize_hashtag(
    value: str
) -> str:

    value = value.strip()

    value = value.replace(
        "#",
        ""
    )

    value = re.sub(
        r"\s+",
        "",
        value
    )

    if not value:
        return ""

    return "#" + value


# ============================================================
# FINGERPRINT / DUPLICATE ENGINE
# ============================================================

def canonical_text(
    value: str
) -> str:

    value = normalize_text(
        value
    )

    value = re.sub(
        r"<[^>]+>",
        " ",
        value
    )

    value = html.unescape(
        value
    )

    value = re.sub(
        r"#[^\s]+",
        "",
        value
    )

    value = value.lower()

    value = re.sub(
        r"[^\w\u0600-\u06FF ]+",
        " ",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def sha256_text(
    value: str
) -> str:

    return hashlib.sha256(
        value.encode(
            "utf-8"
        )
    ).hexdigest()


def token_set(
    value: str
) -> set[str]:

    normalized = canonical_text(
        value
    )

    return {
        token
        for token in normalized.split()
        if len(token) >= 3
    }


def similarity_score(
    first: str,
    second: str
) -> float:

    a = token_set(first)
    b = token_set(second)

    if not a or not b:
        return 0.0

    intersection = len(
        a.intersection(b)
    )

    union = len(
        a.union(b)
    )

    if union == 0:
        return 0.0

    return intersection / union


def is_duplicate_like(
    post: str,
    state: dict
) -> tuple[bool, float]:

    history = state.get(
        "published_posts",
        []
    )

    current_hash = sha256_text(
        canonical_text(post)
    )

    highest = 0.0

    for item in history[-HISTORY_LIMIT:]:

        if not isinstance(
            item,
            dict
        ):
            continue

        old_hash = item.get(
            "hash"
        )

        if old_hash == current_hash:
            return True, 1.0

        old_text = item.get(
            "text",
            ""
        )

        score = similarity_score(
            post,
            old_text
        )

        highest = max(
            highest,
            score
        )

    # Conservative duplicate threshold.
    return (
        highest >= 0.78,
        highest
    )


# ============================================================
# TOPIC SELECTION
# ============================================================

def choose_topic(
    state: dict
) -> Topic:

    used = state.get(
        "topics_used",
        []
    )

    recent = set(
        used[-TOPIC_COOLDOWN:]
    )

    available = [
        topic
        for topic in TOPICS
        if topic.title not in recent
    ]

    if not available:
        available = TOPICS

    # Prefer a balanced category distribution.
    category_counts: dict[str, int] = {}

    for item in state.get(
        "published_posts",
        []
    )[-20:]:

        category = item.get(
            "category"
        )

        if category:
            category_counts[category] = (
                category_counts.get(
                    category,
                    0
                ) + 1
            )

    min_usage = min(
        (
            category_counts.get(
                topic.category,
                0
            )
            for topic in available
        ),
        default=0
    )

    balanced = [
        topic
        for topic in available
        if category_counts.get(
            topic.category,
            0
        ) <= min_usage + 1
    ]

    return random.choice(
        balanced or available
    )


def choose_content_type(
    state: dict
) -> ContentType:

    recent = state.get(
        "content_types_used",
        []
    )[-8:]

    candidates = [
        key
        for key in CONTENT_STYLES
        if key not in recent
    ]

    if not candidates:
        candidates = list(
            CONTENT_STYLES.keys()
        )

    return random.choice(
        candidates
    )  # type: ignore


# ============================================================
# AI CLIENT
# ============================================================

def create_ai_client(
    settings: Settings
) -> genai.Client:

    return genai.Client(
        api_key=settings.gemini_key
    )


# ============================================================
# SYSTEM INSTRUCTION
# ============================================================

SYSTEM_INSTRUCTION = """
تو سردبیر ارشد و مدرس حرفه‌ای محتوای فارسی برای برند
«طاها گرافیک | آموزش و ترفند طراحی 🎨» هستی.

ماموریت:
تولید محتوای ارزشمند، واقعی، کاربردی و حرفه‌ای برای طراحان گرافیک فارسی‌زبان.

استاندارد:
- طبیعی
- فارسی
- دقیق
- کاربردی
- غیرتکراری
- مختصر اما عمیق
- قابل اجرا

مخاطب:
طراحان گرافیک، کاربران Photoshop، دانشجویان طراحی،
طراحان مبتدی، متوسط و حرفه‌ای.

هر پست باید حداقل یک نکته عملی داشته باشد.

هرگز:
- درباره هوش مصنوعی حرف نزن.
- درباره مدل Gemini حرف نزن.
- درباره API حرف نزن.
- درباره نحوه تولید محتوا حرف نزن.
- منبع جعلی نساز.
- نقل‌قول جعلی نساز.
- لینک جعلی نساز.
- مقدمه‌های کلیشه‌ای تولید نکن.
- از جمله‌های بی‌ارزش و تبلیغاتی استفاده نکن.

نام برند باید در خروجی حفظ شود.

محتوا نباید مصنوعی، تکراری یا پر از ایموجی باشد.
"""


# ============================================================
# USER PROMPT
# ============================================================

def build_generation_prompt(
    topic: Topic,
    content_type: ContentType,
    state: dict
) -> str:

    recent_titles = []

    for item in state.get(
        "published_posts",
        []
    )[-12:]:

        title = item.get(
            "title"
        )

        if title:
            recent_titles.append(
                f"- {title}"
            )

    recent_text = (
        "\n".join(recent_titles)
        if recent_titles
        else
        "هنوز پستی ثبت نشده است."
    )

    style = CONTENT_STYLES[
        content_type
    ]

    return f"""
برای کانال تلگرام «طاها گرافیک» یک پست جدید بساز.

موضوع:
{topic.title}

دسته:
{topic.category}

سطح:
{topic.difficulty}

نوع محتوا:
{content_type}

سبک:
{style}

پست‌های اخیر برای جلوگیری از تکرار:
{recent_text}


قواعد اختصاصی:

1. متن نهایی کاملاً فارسی باشد.
2. عنوان کوتاه و حرفه‌ای باشد.
3. شروع متن باید توجه مخاطب را جلب کند.
4. نکته اصلی باید کاملاً مشخص باشد.
5. مراحل باید قابل اجرا باشند.
6. اگر Photoshop مطرح شد، نام فارسی و انگلیسی ابزار را بیاور.
7. در صورت امکان از روش غیرمخرب استفاده کن.
8. از توضیحات بسیار عمومی خودداری کن.
9. به جای توصیه مبهم، دستور مشخص بده.
10. از مثال کاربردی استفاده کن، ولی مثال را طولانی نکن.
11. حداکثر 5 هشتگ بده.
12. هشتگ‌ها مرتبط باشند.
13. ایموجی محدود استفاده کن.
14. هیچ لینک نساز.
15. هیچ منبع جعلی نساز.
16. عبارت «امیدوارم مفید باشد» ننویس.
17. جمله «امروز می‌خواهیم...» ننویس.
18. متن باید مستقیماً قابل انتشار باشد.
19. برای طراحان گرافیک ارزش واقعی ایجاد کن.
20. از موضوعات اخیر کپی نکن.
21. Brand:
   {BRAND_LINE}

حدود طول هدف:
بین 700 تا 2200 کاراکتر متن نهایی.


ملاک کیفیت:
مخاطب باید بعد از خواندن پست بتواند یک کار واقعی را
بهتر، سریع‌تر یا حرفه‌ای‌تر انجام دهد.
"""


# ============================================================
# AI GENERATION
# ============================================================

def generate_structured_post(
    client: genai.Client,
    settings: Settings,
    topic: Topic,
    content_type: ContentType,
    state: dict
) -> GeneratedPost:

    prompt = build_generation_prompt(
        topic=topic,
        content_type=content_type,
        state=state
    )

    last_error: Optional[Exception] = None

    for attempt in range(
        1,
        MAX_AI_ATTEMPTS + 1
    ):

        try:

            logger.info(
                "AI generation attempt %d/%d",
                attempt,
                MAX_AI_ATTEMPTS
            )

            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=GeneratedPost,
                    temperature=0.85,
                    max_output_tokens=2200,
                ),
            )

            if not response:
                raise RuntimeError(
                    "Gemini returned an empty response."
                )

            parsed = getattr(
                response,
                "parsed",
                None
            )

            if isinstance(
                parsed,
                GeneratedPost
            ):
                return parsed

            raw = getattr(
                response,
                "text",
                None
            )

            if not raw:
                raise RuntimeError(
                    "Gemini returned no usable text."
                )

            return GeneratedPost.model_validate_json(
                raw
            )

        except (
            ValidationError,
            Exception
        ) as exc:

            last_error = exc

            logger.warning(
                "AI generation failed: %s",
                exc
            )

            if attempt < MAX_AI_ATTEMPTS:

                delay = (
                    BASE_RETRY_DELAY
                    * (2 ** (attempt - 1))
                )

                time.sleep(
                    delay
                )

    raise RuntimeError(
        f"AI generation failed: {last_error}"
    )


# ============================================================
# REPAIR ENGINE
# ============================================================

def repair_post(
    client: genai.Client,
    settings: Settings,
    post: GeneratedPost,
    problems: list[str]
) -> GeneratedPost:

    repair_prompt = f"""
این محتوای تولیدشده باید قبل از انتشار اصلاح شود.

محتوای فعلی:

عنوان:
{post.title}

Hook:
{post.hook}

نکته اصلی:
{post.main_point}

مراحل:
{json.dumps(post.steps, ensure_ascii=False)}

نکته حرفه‌ای:
{post.pro_tip}

هشتگ‌ها:
{json.dumps(post.hashtags, ensure_ascii=False)}

مشکلات شناسایی‌شده:
{json.dumps(problems, ensure_ascii=False)}

نسخه اصلاح‌شده کامل را تولید کن.

قوانین:
- همان موضوع اصلی حفظ شود.
- اطلاعات درست بماند.
- فارسی طبیعی باشد.
- تکرار کم شود.
- هیچ توضیحی درباره اصلاح یا هوش مصنوعی ننویس.
- خروجی دقیقاً مطابق schema باشد.
"""

    for attempt in range(
        1,
        MAX_REPAIR_ATTEMPTS + 1
    ):

        try:

            logger.info(
                "Repair attempt %d/%d",
                attempt,
                MAX_REPAIR_ATTEMPTS
            )

            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=repair_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=GeneratedPost,
                    temperature=0.6,
                    max_output_tokens=2200,
                ),
            )

            parsed = getattr(
                response,
                "parsed",
                None
            )

            if isinstance(
                parsed,
                GeneratedPost
            ):
                return parsed

            raw = getattr(
                response,
                "text",
                None
            )

            if raw:
                return GeneratedPost.model_validate_json(
                    raw
                )

        except Exception as exc:

            logger.warning(
                "Repair failed: %s",
                exc
            )

            time.sleep(
                BASE_RETRY_DELAY
            )

    raise RuntimeError(
        "Post repair failed."
    )


# ============================================================
# RENDERER
# ============================================================

def escape(
    value: str
) -> str:

    return html.escape(
        normalize_text(value),
        quote=False
    )


def render_post(
    post: GeneratedPost
) -> str:

    hashtags = []

    for tag in post.hashtags:

        normalized = normalize_hashtag(
            tag
        )

        if normalized and normalized not in hashtags:

            hashtags.append(
                normalized
            )

    hashtags = hashtags[:5]

    lines = []

    lines.append(
        f"<b>{escape(post.title)}</b>"
    )

    lines.append("")

    lines.append(
        escape(post.hook)
    )

    lines.append("")

    lines.append(
        "🔹 <b>نکته اصلی</b>"
    )

    lines.append(
        escape(post.main_point)
    )

    lines.append("")

    lines.append(
        "🔹 <b>مراحل اجرا</b>"
    )

    for index, step in enumerate(
        post.steps,
        start=1
    ):

        lines.append(
            f"<b>{index}.</b> "
            f"{escape(step)}"
        )

    lines.append("")

    lines.append(
        "💡 <b>نکته حرفه‌ای</b>"
    )

    lines.append(
        escape(post.pro_tip)
    )

    lines.append("")

    lines.append(
        f"<b>{escape(BRAND_LINE)}</b>"
    )

    if hashtags:

        lines.append("")

        lines.append(
            " ".join(
                escape(tag)
                for tag in hashtags
            )
        )

    return "\n".join(
        lines
    ).strip()


# ============================================================
# LOCAL QUALITY ENGINE
# ============================================================

def plain_text(
    rendered_html: str
) -> str:

    value = re.sub(
        r"<[^>]+>",
        "",
        rendered_html
    )

    return html.unescape(
        value
    )


def count_hashtags(
    text: str
) -> int:

    return len(
        re.findall(
            r"(?<!\w)#[^\s]+",
            text
        )
    )


def persian_ratio(
    text: str
) -> float:

    all_letters = re.findall(
        r"[A-Za-z\u0600-\u06FF]",
        text
    )

    if not all_letters:
        return 0.0

    persian_letters = re.findall(
        r"[\u0600-\u06FF]",
        text
    )

    return (
        len(persian_letters)
        / len(all_letters)
    )


def quality_check(
    post: str,
    structured: GeneratedPost,
    settings: Settings,
    topic: Topic
) -> tuple[bool, list[str]]:

    problems: list[str] = []

    text = plain_text(
        post
    )

    # --------------------------------------------------------
    # Length
    # --------------------------------------------------------

    if len(text) < settings.min_post_length:

        problems.append(
            "متن بیش از حد کوتاه است."
        )

    if len(post) > settings.max_post_length:

        problems.append(
            "پیام نهایی بیش از حد طولانی است."
        )

    # --------------------------------------------------------
    # Brand
    # --------------------------------------------------------

    if BRAND_LINE not in text:

        problems.append(
            "نام برند وجود ندارد."
        )

    # --------------------------------------------------------
    # Hashtags
    # --------------------------------------------------------

    hashtag_count = count_hashtags(
        text
    )

    if hashtag_count < 2:

        problems.append(
            "هشتگ کافی وجود ندارد."
        )

    if hashtag_count > 5:

        problems.append(
            "تعداد هشتگ بیش از حد مجاز است."
        )

    # --------------------------------------------------------
    # Steps
    # --------------------------------------------------------

    if len(structured.steps) < 2:

        problems.append(
            "تعداد مراحل کم است."
        )

    if len(structured.steps) > 7:

        problems.append(
            "تعداد مراحل بیش از حد است."
        )

    # --------------------------------------------------------
    # Persian language
    # --------------------------------------------------------

    if persian_ratio(text) < 0.42:

        problems.append(
            "نسبت متن فارسی پایین است."
        )

    # --------------------------------------------------------
    # Forbidden phrases
    # --------------------------------------------------------

    lowered = text.lower()

    for phrase in FORBIDDEN_PHRASES:

        if phrase.lower() in lowered:

            problems.append(
                f"عبارت ممنوع: {phrase}"
            )

    # --------------------------------------------------------
    # Excessive punctuation
    # --------------------------------------------------------

    if re.search(
        r"[!?؟!]{4,}",
        text
    ):

        problems.append(
            "علائم نگارشی بیش از حد."
        )

    # --------------------------------------------------------
    # Excessive emoji
    # --------------------------------------------------------

    emoji_count = len(
        re.findall(
            r"[\U0001F300-\U0001FAFF]",
            text
        )
    )

    if emoji_count > 12:

        problems.append(
            "تعداد ایموجی زیاد است."
        )

    # --------------------------------------------------------
    # Duplicate title / words
    # --------------------------------------------------------

    title_words = (
        token_set(
            structured.title
        )
    )

    if len(title_words) < 2:

        problems.append(
            "عنوان بیش از حد کوتاه یا مبهم است."
        )

    # --------------------------------------------------------
    # Category relevance
    # --------------------------------------------------------

    category_keywords = {

        "Photoshop": [
            "فتوشاپ",
            "photoshop",
            "لایه",
            "ماسک",
            "ابزار",
            "فیلتر",
        ],

        "Typography": [
            "فونت",
            "تایپوگرافی",
            "حروف",
            "متن",
        ],

        "Color": [
            "رنگ",
            "کنتراست",
            "پالت",
            "اشباع",
        ],

        "Graphic Design": [
            "طراحی",
            "گرافیک",
            "ترکیب",
            "چیدمان",
        ],

        "Poster": [
            "پوستر",
            "تیتر",
            "چیدمان",
        ],

        "Professional": [
            "طراحی",
            "فایل",
            "خروجی",
            "حرفه‌ای",
        ]
    }

    keywords = category_keywords.get(
        topic.category,
        []
    )

    if keywords:

        relevant = any(
            keyword.lower()
            in text.lower()
            for keyword in keywords
        )

        if not relevant:

            problems.append(
                "ارتباط کافی با موضوع دیده نشد."
            )

    return (
        len(problems) == 0,
        problems
    )


# ============================================================
# TELEGRAM CLIENT
# ============================================================

class TelegramClient:

    def __init__(
        self,
        token: str,
        channel: str
    ) -> None:

        self.token = token

        self.channel = channel

        self.base_url = (
            f"https://api.telegram.org/bot{token}"
        )

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent":
                f"{APP_NAME}/{APP_VERSION}"
        })

    # --------------------------------------------------------
    # Generic request
    # --------------------------------------------------------

    def request(
        self,
        method: str,
        payload: Optional[dict] = None
    ) -> dict:

        url = (
            f"{self.base_url}/{method}"
        )

        last_error: Optional[Exception] = None

        for attempt in range(
            1,
            TELEGRAM_RETRIES + 1
        ):

            try:

                response = self.session.post(
                    url,
                    json=payload or {},
                    timeout=REQUEST_TIMEOUT
                )

                response.raise_for_status()

                data = response.json()

                if not data.get(
                    "ok",
                    False
                ):

                    raise RuntimeError(
                        f"Telegram API rejected request: {data}"
                    )

                return data

            except Exception as exc:

                last_error = exc

                logger.warning(
                    "Telegram API attempt %d/%d failed: %s",
                    attempt,
                    TELEGRAM_RETRIES,
                    exc
                )

                if attempt < TELEGRAM_RETRIES:

                    delay = (
                        BASE_RETRY_DELAY
                        * (2 ** (attempt - 1))
                    )

                    time.sleep(
                        delay
                    )

        raise RuntimeError(
            f"Telegram API failed: {last_error}"
        )

    # --------------------------------------------------------
    # Bot identity
    # --------------------------------------------------------

    def get_me(self) -> dict:

        return self.request(
            "getMe"
        )

    # --------------------------------------------------------
    # Channel
    # --------------------------------------------------------

    def get_chat(self) -> dict:

        return self.request(
            "getChat",
            {
                "chat_id":
                    self.channel
            }
        )

    # --------------------------------------------------------
    # Member / administrator status
    # --------------------------------------------------------

    def get_chat_member(
        self,
        user_id: int
    ) -> dict:

        return self.request(
            "getChatMember",
            {
                "chat_id":
                    self.channel,

                "user_id":
                    user_id
            }
        )

    # --------------------------------------------------------
    # Send message
    # --------------------------------------------------------

    def send_message(
        self,
        text: str
    ) -> dict:

        return self.request(
            "sendMessage",
            {
                "chat_id":
                    self.channel,

                "text":
                    text,

                "parse_mode":
                    "HTML",

                "disable_web_page_preview":
                    True
            }
        )


# ============================================================
# TELEGRAM HEALTH CHECK
# ============================================================

def verify_telegram(
    telegram: TelegramClient
) -> dict:

    logger.info(
        "Checking Telegram bot identity..."
    )

    bot_response = telegram.get_me()

    bot = bot_response.get(
        "result",
        {}
    )

    bot_id = bot.get(
        "id"
    )

    bot_username = bot.get(
        "username",
        "unknown"
    )

    if not bot_id:

        raise RuntimeError(
            "Telegram bot ID could not be determined."
        )

    logger.info(
        "Telegram bot verified: @%s",
        bot_username
    )

    logger.info(
        "Checking target channel..."
    )

    channel_response = telegram.get_chat()

    channel_data = channel_response.get(
        "result",
        {}
    )

    chat_type = channel_data.get(
        "type"
    )

    if chat_type != "channel":

        raise RuntimeError(
            f"Target is not a Telegram channel. "
            f"Detected type: {chat_type}"
        )

    logger.info(
        "Target channel verified: %s",
        channel_data.get(
            "title",
            telegram.channel
        )
    )

    logger.info(
        "Checking bot administrator status..."
    )

    member_response = telegram.get_chat_member(
        bot_id
    )

    member = member_response.get(
        "result",
        {}
    )

    status = member.get(
        "status"
    )

    if status not in {
        "administrator",
        "creator"
    }:

        raise RuntimeError(
            "The bot is not an administrator of the channel."
        )

    if status == "administrator":

        can_post = member.get(
            "can_post_messages"
        )

        if can_post is False:

            raise RuntimeError(
                "The bot is administrator "
                "but does not have post permission."
            )

    logger.info(
        "Telegram permissions verified."
    )

    return {
        "bot_id": bot_id,
        "bot_username": bot_username,
        "channel_title":
            channel_data.get("title")
    }


# ============================================================
# STATE UPDATE
# ============================================================

def record_success(
    state: dict,
    post: str,
    structured: GeneratedPost,
    topic: Topic
) -> None:

    now = datetime.now(
        timezone.utc
    ).isoformat()

    signature = sha256_text(
        canonical_text(post)
    )

    history_item = {
        "timestamp": now,
        "hash": signature,
        "title": structured.title,
        "category": topic.category,
        "topic": topic.title,
        "content_type":
            structured.content_type,
        "text": plain_text(post)
    }

    published = state.setdefault(
        "published_posts",
        []
    )

    published.append(
        history_item
    )

    state["published_posts"] = (
        published[-HISTORY_LIMIT:]
    )

    state.setdefault(
        "topics_used",
        []
    ).append(
        topic.title
    )

    state["topics_used"] = (
        state["topics_used"]
        [-HISTORY_LIMIT:]
    )

    state.setdefault(
        "content_types_used",
        []
    ).append(
        structured.content_type
    )

    state["content_types_used"] = (
        state["content_types_used"]
        [-20:]
    )

    state["last_success"] = now

    state["total_published"] = (
        int(
            state.get(
                "total_published",
                0
            )
        )
        + 1
    )

    save_state(
        state
    )


# ============================================================
# FULL CONTENT PIPELINE
# ============================================================

def create_quality_controlled_post(
    client: genai.Client,
    settings: Settings,
    state: dict
) -> tuple[str, GeneratedPost, Topic]:

    topic = choose_topic(
        state
    )

    content_type = choose_content_type(
        state
    )

    logger.info(
        "Selected topic: %s",
        topic.title
    )

    logger.info(
        "Selected category: %s",
        topic.category
    )

    logger.info(
        "Selected content type: %s",
        content_type
    )

    structured = generate_structured_post(
        client=client,
        settings=settings,
        topic=topic,
        content_type=content_type,
        state=state
    )

    post = render_post(
        structured
    )

    # --------------------------------------------------------
    # First local quality check
    # --------------------------------------------------------

    valid, problems = quality_check(
        post=post,
        structured=structured,
        settings=settings,
        topic=topic
    )

    # --------------------------------------------------------
    # Repair loop
    # --------------------------------------------------------

    repair_count = 0

    while (
        not valid
        and repair_count < MAX_REPAIR_ATTEMPTS
    ):

        repair_count += 1

        logger.warning(
            "Quality gate failed. Repairing..."
        )

        logger.warning(
            "Problems: %s",
            problems
        )

        structured = repair_post(
            client=client,
            settings=settings,
            post=structured,
            problems=problems
        )

        post = render_post(
            structured
        )

        valid, problems = quality_check(
            post=post,
            structured=structured,
            settings=settings,
            topic=topic
        )

    if not valid:

        raise RuntimeError(
            "Content failed quality control: "
            + " | ".join(problems)
        )

    # --------------------------------------------------------
    # Duplicate protection
    # --------------------------------------------------------

    duplicate, score = is_duplicate_like(
        post,
        state
    )

    logger.info(
        "Duplicate similarity score: %.3f",
        score
    )

    if duplicate:

        raise RuntimeError(
            "Generated post is too similar "
            "to a previously published post."
        )

    return (
        post,
        structured,
        topic
    )


# ============================================================
# PREVIEW
# ============================================================

def print_preview(
    post: str,
    structured: GeneratedPost,
    topic: Topic,
    settings: Settings
) -> None:

    print()
    print("=" * 72)
    print(APP_NAME)
    print("=" * 72)

    print(
        f"Version: {APP_VERSION}"
    )

    print(
        f"Model: {settings.gemini_model}"
    )

    print(
        f"Channel: {settings.channel}"
    )

    print(
        f"Category: {topic.category}"
    )

    print(
        f"Topic: {topic.title}"
    )

    print(
        f"Type: {structured.content_type}"
    )

    print(
        f"Length: {len(post)}"
    )

    print("-" * 72)

    print(
        plain_text(post)
    )

    print("=" * 72)
    print()


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    start_time = time.time()

    logger.info(
        "=" * 72
    )

    logger.info(
        "%s v%s",
        APP_NAME,
        APP_VERSION
    )

    logger.info(
        "Starting..."
    )

    logger.info(
        "=" * 72
    )

    # --------------------------------------------------------
    # Load settings
    # --------------------------------------------------------

    settings = load_settings()

    logger.info(
        "Configuration loaded."
    )

    logger.info(
        "Channel: %s",
        settings.channel
    )

    logger.info(
        "Model: %s",
        settings.gemini_model
    )

    logger.info(
        "Dry run: %s",
        settings.dry_run
    )

    # --------------------------------------------------------
    # State
    # --------------------------------------------------------

    state = load_state()

    logger.info(
        "Published history entries: %d",
        len(
            state.get(
                "published_posts",
                []
            )
        )
    )

    logger.info(
        "Total successful publications: %s",
        state.get(
            "total_published",
            0
        )
    )

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    telegram = TelegramClient(
        token=settings.telegram_token,
        channel=settings.channel
    )

    telegram_info = verify_telegram(
        telegram
    )

    logger.info(
        "Telegram identity: @%s",
        telegram_info["bot_username"]
    )

    # --------------------------------------------------------
    # Gemini
    # --------------------------------------------------------

    client = create_ai_client(
        settings
    )

    logger.info(
        "Gemini client initialized."
    )

    # --------------------------------------------------------
    # Generate
    # --------------------------------------------------------

    post, structured, topic = (
        create_quality_controlled_post(
            client=client,
            settings=settings,
            state=state
        )
    )

    # --------------------------------------------------------
    # Preview
    # --------------------------------------------------------

    print_preview(
        post=post,
        structured=structured,
        topic=topic,
        settings=settings
    )

    # --------------------------------------------------------
    # Dry run
    # --------------------------------------------------------

    if settings.dry_run:

        logger.info(
            "DRY_RUN enabled."
        )

        logger.info(
            "No message was published."
        )

        return

    # --------------------------------------------------------
    # Publish
    # --------------------------------------------------------

    logger.info(
        "Publishing..."
    )

    result = telegram.send_message(
        post
    )

    message_id = (
        result
        .get("result", {})
        .get("message_id")
    )

    logger.info(
        "Published successfully."
    )

    logger.info(
        "Telegram message ID: %s",
        message_id
    )

    # --------------------------------------------------------
    # Persist history
    # --------------------------------------------------------

    record_success(
        state=state,
        post=post,
        structured=structured,
        topic=topic
    )

    elapsed = (
        time.time()
        - start_time
    )

    logger.info(
        "State saved."
    )

    logger.info(
        "Execution completed in %.2f seconds.",
        elapsed
    )

    logger.info(
        "✅ TAHA GRAPHIC AUTO PUBLISHER SUCCESS"
    )


# ============================================================
# GLOBAL ERROR HANDLING
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        logger.warning(
            "Execution interrupted."
        )

        sys.exit(130)

    except Exception as exc:

        logger.exception(
            "FATAL ERROR: %s",
            exc
        )

        sys.exit(1)
