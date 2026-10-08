import asyncio
import json
import math
import os
import random
import re
from pathlib import Path

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


# يمكن تغيير الموديل من متغير البيئة GEMINI_MODEL.
# القيمة الافتراضية هي نفس الموجودة في كودك السابق.
MODEL_NAME = os.environ.get(
    "GEMINI_MODEL", "gemini-3.8-flash"
).strip()
VOICE = "ar-SA-HamedNeural"


def reshape_ar(text):
    return get_display(arabic_reshaper.reshape(text))


def load_font(size):
    candidates = [
        os.environ.get("ARABIC_FONT_PATH", ""),
        "arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf",
    ]

    for path in candidates:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    raise RuntimeError(
        "لم يتم العثور على خط يدعم العربية. "
        "أضف ملف خط واضبط ARABIC_FONT_PATH على مساره."
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
                minutes = math.ceil(retry_seconds / 60)
                raise RuntimeError(
                    f"{reason}. يطلب API الانتظار حوالي "
                    f"{minutes} دقيقة. أعد التشغيل لاحقًا. "
                    "في حالة 429 راجع الحصة والفوترة."
                ) from exc

            if attempt == max_attempts:
                raise RuntimeError(
                    f"{reason}. فشلت {max_attempts} محاولات."
                ) from exc

            delay = (
                retry_seconds
                if retry_seconds is not None
                else min(10 * (2 ** (attempt - 1)), 60)
            )
            delay += random.uniform(0.5, 1.5)

            print(
                f"⏳ {reason} (HTTP {exc.code}). "
                f"المحاولة التالية بعد {delay:.1f} ثانية "
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

الشروط:
1. لا تذكر أي حديث ضعيف أو موضوع.
2. لا تختلق رواية وتنسبها إلى شخصية تاريخية.
3. رد بكائن JSON فقط، بقيم نصية غير فارغة:
{{
  "story": "القصة نحو 300 كلمة بلغة عربية فصحى وبسرد مشوق",
  "title": "عنوان يوتيوب جذاب من 6 كلمات",
  "description": "وصف سطرين مناسب للبحث",
  "hashtags": "#قصص_اسلامية #ستيك_مان #عبرة #حكايات_اسلامية",
  "thumb_text": "كلمتان للصورة المصغرة"
}}
"""

    response = await safe_generate(client, prompt)
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini أعاد استجابة نصية فارغة.")

    # إزالة سياج Markdown إن أعاده الموديل رغم طلب JSON.
    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE
        )
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "استجابة Gemini ليست JSON صالحًا؛ "
            "تم إيقاف التنفيذ بدل تحويل الاستجابة الخاطئة لفيديو."
        ) from exc

    fields = (
        "story",
        "title",
        "description",
        "hashtags",
        "thumb_text",
    )
    if not isinstance(data, dict):
        raise RuntimeError("الاستجابة يجب أن تكون كائن JSON.")

    for field in fields:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"الحقل {field} مفقود أو ليس نصًا صالحًا."
            )
        data[field] = value.strip()

    return data


async def text_to_speech(text):
    path = "voice.mp3"
    communicate = edge_tts.Communicate(
        text,
        VOICE,
        rate="-15%",
        volume="+5%",
    )
    await communicate.save(path)
    return path


def create_long_video(audio_path, story_text):
    width, height = 1920, 1080
    font = load_font(42)
    words = story_text.split()
    chunks = [
        " ".join(words[index:index + 12])
        for index in range(0, len(words), 12)
    ]
    if not chunks:
        raise ValueError("نص القصة فارغ.")

    with AudioFileClip(audio_path) as audio:
        duration = audio.duration
        if not duration or duration <= 0:
            raise RuntimeError("ملف الصوت لا يحتوي على مدة صالحة.")

        def make_frame(t):
            image = Image.new(
                "RGB", (width, height), color=(10, 10, 10)
            )
            draw = ImageDraw.Draw(image)
            center = width // 2
            y = int(5 * np.sin(t * 3))

            draw.ellipse(
                [center - 60, 250 + y, center + 60, 370 + y],
                outline="white",
                width=9,
            )
            segments = [
                [center, 370 + y, center, 600 + y],
                [center, 420 + y, center - 80, 480 + y],
                [center, 420 + y, center + 80, 480 + y],
                [center, 600 + y, center - 60, 750 + y],
                [center, 600 + y, center + 60, 750 + y],
            ]
            for segment in segments:
                draw.line(segment, fill="white", width=9)

            draw.rectangle(
                [0, 850, width, height], fill=(255, 193, 7)
            )

            # توزيع تقريبي للنص على مدة الصوت، وليس محاذاة كلمات.
            index = min(
                int((t / duration) * len(chunks)),
                len(chunks) - 1,
            )
            draw.text(
                (center, 940),
                reshape_ar(chunks[index]),
                fill="black",
                font=font,
                anchor="mm",
            )
            return np.array(image)

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

    return "long_video.mp4"


def create_thumbnail(thumb_text):
    image = Image.new(
        "RGB", (1280, 720), color=(255, 193, 7)
    )
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        [440, 110, 840, 510],
        fill="white",
        outline="black",
        width=12,
    )
    draw.ellipse([560, 230, 610, 300], fill="black")
    draw.ellipse([670, 230, 720, 300], fill="black")

    text = reshape_ar(thumb_text)
    size = 130
    font = load_font(size)
    while draw.textlength(text, font=font) > 1180 and size > 20:
        size -= 5
        font = load_font(size)

    draw.text(
        (640, 620),
        text,
        fill="black",
        font=font,
        anchor="mm",
        stroke_width=3,
        stroke_fill="white",
    )
    image.save("thumb.jpg")
    return "thumb.jpg"


def create_shorts(long_path):
    with VideoFileClip(long_path) as source:
        clip = source.subclipped(0, min(35, source.duration))
        resized = clip.resized(width=1080).with_position("center")
        background = ColorClip(
            size=(1080, 1920),
            color=(0, 0, 0),
            duration=clip.duration,
        )
        final = CompositeVideoClip(
            [background, resized]
        ).with_audio(clip.audio)

        try:
            final.write_videofile(
                "shorts.mp4",
                fps=24,
                codec="libx264",
                audio_codec="aac",
            )
        finally:
            final.close()
            background.close()

    return "shorts.mp4"


def upload_youtube(
    video_path,
    thumb_path,
    title,
    description,
    tags,
    is_shorts=False,
):
    creds = Credentials.from_authorized_user_info(
        {
            "client_id": os.environ["YT_CLIENT_ID"].strip(),
            "client_secret": os.environ["YT_CLIENT_SECRET"].strip(),
            "refresh_token": os.environ["YT_REFRESH_TOKEN"].strip(),
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    if not creds.valid:
        creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)
    if is_shorts:
        title = title[:87] + " #Shorts"

    try:
        result = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title[:95],
                    "description": description,
                    "tags": tags,
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                },
            },
            media_body=MediaFileUpload(
                video_path, resumable=True
            ),
        ).execute()

        video_id = result["id"]
        print(f"✅ تم الرفع: https://youtu.be/{video_id}")

        if not is_shorts:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumb_path),
            ).execute()

        return video_id
    finally:
        youtube.close()


async def main():
    required = (
        "GEMINI_API_KEY",
        "YT_CLIENT_ID",
        "YT_CLIENT_SECRET",
        "YT_REFRESH_TOKEN",
    )
    missing = [
        name
        for name in required
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError(
            "متغيرات البيئة المطلوبة مفقودة: "
            + ", ".join(missing)
        )

    # التحقق من الخط قبل استهلاك طلب Gemini.
    load_font(42)

    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"].strip()
    )
    try:
        data = await generate_all(client)
    finally:
        client.close()

    # حفظ النص لتسهيل المراجعة عند الحاجة.
    print("البيانات المولدة:", json.dumps(data, ensure_ascii=False))

    audio_path = await text_to_speech(data["story"])
    long_path = create_long_video(audio_path, data["story"])
    thumb_path = create_thumbnail(data["thumb_text"])
    shorts_path = create_shorts(long_path)

    tags = data["hashtags"].replace("#", "").split()
    description = (
        f"{data['description']}\n\n"
        f"{data['story'][:400]}\n\n"
        f"{data['hashtags']}"
    )

    upload_youtube(
        long_path,
        thumb_path,
        data["title"],
        description,
        tags,
    )
    upload_youtube(
        shorts_path,
        thumb_path,
        data["thumb_text"],
        data["hashtags"] + " #Shorts",
        tags,
        is_shorts=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"❌ توقف التشغيل: {exc}", flush=True)
        raise
