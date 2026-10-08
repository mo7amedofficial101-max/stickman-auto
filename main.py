import asyncio
import json
import os
import random
import re
import sys

import arabic_reshaper
import edge_tts
import numpy as np
from bidi.algorithm import get_display
from google import genai
from google.genai import errors
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from moviepy import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    VideoClip,
    VideoFileClip,
)
from PIL import Image, ImageDraw, ImageFont


MODEL_NAME = os.environ.get(
    "GEMINI_MODEL", "gemini-3.8-flash"
).strip()
VOICE = "ar-SA-HamedNeural"


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"متغير البيئة {name} غير موجود أو فارغ.")
    return value


def reshape_ar(text):
    return get_display(arabic_reshaper.reshape(text))


def load_font(size):
    # يمكن تحديد مسار خط يدعم العربية عبر FONT_PATH.
    paths = [
        os.environ.get("FONT_PATH", "").strip(),
        "arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    ]
    for path in paths:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    raise RuntimeError(
        "لم أجد خطًا مناسبًا. وفّر خطًا يدعم العربية "
        "وحدّد مساره في FONT_PATH."
    )


def get_retry_seconds(error):
    payload = getattr(error, "response_json", None)

    if isinstance(payload, dict):
        body = payload.get("error", payload)
        if isinstance(body, dict):
            details = body.get("details", [])
            if isinstance(details, list):
                for detail in details:
                    if not isinstance(detail, dict):
                        continue

                    value = detail.get("retryDelay")
                    if isinstance(value, str):
                        match = re.fullmatch(
                            r"(\d+(?:\.\d+)?)s", value
                        )
                        if match:
                            return float(match.group(1))

    # مثال: Please retry in 10h11m26.984748055s.
    message = str(getattr(error, "message", "") or error)
    match = re.search(
        r"retry\s+in\s+"
        r"((?:\d+(?:\.\d+)?\s*[hms]\s*)+)",
        message,
        re.IGNORECASE,
    )
    if not match:
        return None

    factors = {"h": 3600, "m": 60, "s": 1}
    return sum(
        float(value) * factors[unit.lower()]
        for value, unit in re.findall(
            r"(\d+(?:\.\d+)?)\s*([hms])",
            match.group(1),
            re.IGNORECASE,
        )
    )


async def safe_generate(client, prompt):
    max_attempts = 4
    max_wait_seconds = 120

    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.to_thread(
                client.models.generate_content,
                model=MODEL_NAME,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
        except errors.APIError as exc:
            if exc.code not in (429, 503):
                raise

            reason = (
                "تجاوز حصة أو معدل طلبات Gemini"
                if exc.code == 429
                else "خدمة Gemini غير متاحة مؤقتًا"
            )
            retry_seconds = get_retry_seconds(exc)

            if (
                retry_seconds is not None
                and retry_seconds > max_wait_seconds
            ):
                minutes = int(retry_seconds // 60) + 1
                raise RuntimeError(
                    f"{reason}. يطلب API الانتظار حوالي "
                    f"{minutes} دقيقة. أعد التشغيل لاحقًا؛ "
                    "ولخطأ 429 راجع الحصة والفوترة."
                ) from exc

            if attempt == max_attempts:
                raise RuntimeError(
                    f"{reason}: فشلت {max_attempts} محاولات."
                ) from exc

            delay = (
                retry_seconds
                if retry_seconds is not None
                else min(10 * (2 ** (attempt - 1)), 60)
            )
            delay += random.uniform(0.5, 1.5)

            print(
                f"⏳ {reason} (HTTP {exc.code}). "
                f"إعادة المحاولة بعد {delay:.1f} ثانية "
                f"({attempt}/{max_attempts})."
            )
            await asyncio.sleep(delay)


async def generate_all(client):
    topic = random.choice([
        "قصة عن بر الوالدين",
        "قصة عن عاقبة الظلم",
        "قصة من حياة عمر بن الخطاب",
        "قصة عن التوبة",
        "قصة عن الأمانة",
        "قصة عن الصبر",
    ])

    prompt = f"""
    اكتب قصة إسلامية موثوقة عن: {topic}
    شروط هامة:
    1. لا تذكر أي حديث ضعيف أو موضوع إطلاقاً.
    2. لا تختلق أحداثاً وتنسبها لشخصيات تاريخية.
    3. رد بصيغة JSON فقط:
    {{
      "story": "القصة 300 كلمة بلغة عربية فصحى وبسرد مشوق",
      "title": "عنوان يوتيوب جذاب من 6 كلمات",
      "description": "وصف سطرين مناسب للـ SEO",
      "hashtags": "#قصص_اسلامية #ستيك_مان #عبرة #حكايات_اسلامية",
      "thumb_text": "كلمتين للصورة المصغرة"
    }}
    """

    response = await safe_generate(client, prompt)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini أعاد استجابة فارغة.")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # لا نحول ردًا معطوبًا إلى تعليق صوتي ونرفعه.
        raise RuntimeError(
            "استجابة Gemini ليست JSON صالحًا. توقف إنشاء الفيديو."
        ) from exc

    required_keys = (
        "story", "title", "description", "hashtags", "thumb_text"
    )
    if not isinstance(data, dict):
        raise RuntimeError("استجابة Gemini ليست كائن JSON.")

    for key in required_keys:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"الحقل {key} مفقود أو غير صالح في استجابة Gemini."
            )
        data[key] = value.strip()

    return data


async def text_to_speech(text):
    communicate = edge_tts.Communicate(
        text, VOICE, rate="-15%", volume="+5%"
    )
    await communicate.save("voice.mp3")
    return "voice.mp3"


def create_long_video(audio_path, story_text):
    width, height = 1920, 1080
    font = load_font(42)
    words = story_text.split()

    with AudioFileClip(audio_path) as audio:
        duration = audio.duration

        def make_frame(t):
            img = Image.new(
                "RGB", (width, height), color=(10, 10, 10)
            )
            draw = ImageDraw.Draw(img)
            x = width // 2
            y = int(5 * np.sin(t * 3))

            draw.ellipse(
                [x - 60, 250 + y, x + 60, 370 + y],
                outline="white",
                width=9,
            )
            for points in [
                [x, 370 + y, x, 600 + y],
                [x, 420 + y, x - 80, 480 + y],
                [x, 420 + y, x + 80, 480 + y],
                [x, 600 + y, x - 60, 750 + y],
                [x, 600 + y, x + 60, 750 + y],
            ]:
                draw.line(points, fill="white", width=9)

            draw.rectangle(
                [0, 850, width, height], fill=(255, 193, 7)
            )

            # عرض تقريبي حسب مدة الصوت، وليس مزامنة كلمات دقيقة.
            word_index = min(
                int(t / duration * len(words)), len(words) - 1
            )
            chunk_start = (word_index // 12) * 12
            chunk = " ".join(words[chunk_start:chunk_start + 12])

            draw.text(
                (x, 940),
                reshape_ar(chunk),
                fill="black",
                font=font,
                anchor="mm",
            )
            return np.array(img)

        video = VideoClip(
            frame_function=make_frame,
            duration=duration,
        ).with_audio(audio)

        try:
        video.write_videofile(
            "long_video.mp4",
            fps=24,
            codec="libx264",
            audio_codec="aac",
        )
    finally:
        video.close()
        audio.close()

    return "long_video.mp4"
