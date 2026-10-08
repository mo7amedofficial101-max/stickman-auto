import os, sys

# رقعة لضمان التوافق بين Pillow الحديثة ومكتبة MoviePy
import PIL.Image
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = getattr(PIL.Image, 'Resampling', PIL.Image).LANCZOS

import asyncio, edge_tts, random, textwrap, json, re
import arabic_reshaper
from bidi.algorithm import get_display

from google import genai
from google.genai import errors

# استيراد متوافق مع كافة إصدارات MoviePy
try:
    from moviepy.editor import VideoClip, AudioFileClip, VideoFileClip, ColorClip, CompositeVideoClip
except ImportError:
    from moviepy import VideoClip, AudioFileClip, VideoFileClip, ColorClip, CompositeVideoClip

from PIL import Image, ImageDraw, ImageFont
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import numpy as np

# جلب المفتاح مع تنظيفه من أي مسافات زائدة
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
client = genai.Client(api_key=GEMINI_KEY)

MODEL_NAME = 'gemini-3.8-flash'
VOICE = "ar-SA-HamedNeural"

def reshape_ar(text):
    return get_display(arabic_reshaper.reshape(text))

async def safe_generate(prompt):
    """دالة توليد تعطي تفاصيل الخطأ الدقيقة ولا تعتبر كل خطأ Rate Limit"""
    delays = [15, 30, 60]
    
    for attempt, delay in enumerate(delays, start=1):
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=MODEL_NAME,
                contents=prompt
            )
            return response
        except errors.APIError as e:
            print(f"❌ Gemini API Error Code: {e.code} - Message: {e.message}")
            if e.code == 429:
                print(f"⚠️ تجاوز حد الطلبات (Rate Limit). الانتظار {delay} ثانية... (المحاولة {attempt}/{len(delays)})")
                await asyncio.sleep(delay)
            else:
                # إذا كان الخطأ 400 أو 403 أو غيره، يتوقف فوراً مع إظهار السبب الحقيقي
                raise Exception(f"خطأ في API (الكود {e.code}): {e.message}")
        except Exception as e:
            print(f"❌ Unexpected Error: {str(e)}")
            raise e
            
    raise Exception("❌ استنفاد المحاولات بسبب الـ Rate Limit. انتظر بضع دقائق وأعد التشغيل.")

async def generate_all():
    topic = random.choice([
        "قصة عن بر الوالدين", "قصة عن عاقبة الظلم", 
        "قصة من حياة عمر بن الخطاب", "قصة عن التوبة", 
        "قصة عن الامانة", "قصة عن الصبر"
    ])
    
    prompt = f"""
    اكتب قصة إسلامية موثوقة عن: {topic}
    شروط هامة:
    1. لا تذكر أي حديث ضعيف أو موضوع إطلاقاً.
    2. رد بصيغة JSON فقط وبدون أي نصوص إضافية قبل أو بعد الـ JSON:
    {{
      "story": "القصة 300 كلمة بلغة عربية فصحى وبسرد مشوق",
      "title": "عنوان يوتيوب جذاب من 6 كلمات",
      "description": "وصف سطرين مناسب للـ SEO",
      "hashtags": "#قصص_اسلامية #ستيك_مان #عبرة #حكايات_اسلامية",
      "thumb_text": "كلمتين للصورة المصغرة"
    }}
    """

    response = await safe_generate(prompt)
    
    try:
        json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
        else:
            raise ValueError("JSON not found")
    except Exception:
        data = {
            "story": response.text, 
            "title": "قصة اسلامية تهز القلوب", 
            "description": "قصة اسلامية مؤثرة", 
            "hashtags": "#قصص_اسلامية #ستيك_مان", 
            "thumb_text": "عبرة عظيمة"
        }

    return data

async def text_to_speech(text):
    communicate = edge_tts.Communicate(text, VOICE, rate="-15%", volume="+5%")
    await communicate.save("voice.mp3")
    return "voice.mp3"

def create_long_video(audio_path, story_text):
    audio = AudioFileClip(audio_path)
    duration = audio.duration
    W, H = 1920, 1080
    def make_frame(t):
        img = Image.new('RGB', (W, H), color=(10,10,10))
        draw = ImageDraw.Draw(img)
        y = int(5 * np.sin(t*3))
        draw.ellipse([W//2-60, 250+y, W//2+60, 370+y], outline="white", width=9)
        draw.line([W//2, 370+y, W//2, 600+y], fill="white", width=9)
        draw.line([W//2, 420+y, W//2-80, 480+y], fill="white", width=9)
        draw.line([W//2, 420+y, W//2+80, 480+y], fill="white", width=9)
        draw.line([W//2, 600+y, W//2-60, 750+y], fill="white", width=9)
        draw.line([W//2, 600+y, W//2+60, 750+y], fill="white", width=9)
        draw.rectangle([0, 850, W, H], fill=(255,193,7))
        try: font = ImageFont.truetype("arial.ttf", 42)
        except: font = ImageFont.load_default()
        words = story_text.split()
        chunk = " ".join(words[int(t*2.2):int(t*2.2)+12])
        reshaped = reshape_ar(chunk)
        draw.text((W//2, 940), reshaped, fill="black", font=font, anchor="mm")
        return np.array(img)
    video = VideoClip(make_frame, duration=duration).set_audio(audio)
    video.write_videofile("long_video.mp4", fps=24, codec='libx264', audio_codec='aac')
    return "long_video.mp4"

def create_thumbnail(thumb_text):
    img = Image.new('RGB', (1280, 720), color=(255,193,7))
    draw = ImageDraw.Draw(img)
    draw.ellipse([440, 110, 840, 510], fill="white", outline="black", width=12)
    draw.ellipse([560, 230, 610, 300], fill="black")
    draw.ellipse([670, 230, 720, 300], fill="black")
    try: font = ImageFont.truetype("arial.ttf", 130)
    except: font = ImageFont.load_default()
    draw.text((640, 620), reshape_ar(thumb_text), fill="black", font=font, anchor="mm", stroke_width=3, stroke_fill="white")
    img.save("thumb.jpg")
    return "thumb.jpg"

def create_shorts(long_path):
    clip = VideoFileClip(long_path).subclip(0, min(35, VideoFileClip(long_path).duration))
    clip_resized = clip.resize(width=1080)
    background = ColorClip(size=(1080,1920), color=(0,0,0), duration=clip.duration)
    final = CompositeVideoClip([background, clip_resized.set_position("center")]).set_audio(clip.audio)
    final.write_videofile("shorts.mp4", fps=24, codec='libx264', audio_codec='aac')
    return "shorts.mp4"

def upload_youtube(video_path, thumb_path, title, description, tags, is_shorts=False):
    creds = Credentials.from_authorized_user_info({
        "client_id": os.environ["YT_CLIENT_ID"].strip(),
        "client_secret": os.environ["YT_CLIENT_SECRET"].strip(),
        "refresh_token": os.environ["YT_REFRESH_TOKEN"].strip(),
        "token_uri": "https://oauth2.googleapis.com/token"
    }, scopes=["https://www.googleapis.com/auth/youtube.upload"])
    
    if creds.expired or not creds.valid:
        creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)
    if is_shorts: 
        title = title + " #Shorts"

    res = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title[:95], 
                "description": description, 
                "tags": tags, 
                "categoryId": "22"
            }, 
            "status": {
                "privacyStatus": "public", 
                "selfDeclaredMadeForKids": False
            }
        },
        media_body=MediaFileUpload(video_path, resumable=True)
    ).execute()
    
    print(f"تم الرفع بنجاح: https://youtu.be/{res['id']}")
    
    if not is_shorts:
        youtube.thumbnails().set(
            videoId=res["id"], 
            media_body=MediaFileUpload(thumb_path)
        ).execute()
        
    return res["id"]

async def main():
    data = await generate_all()
    print("البيانات المولدة:", data)
    await text_to_speech(data["story"])
    create_long_video("voice.mp3", data["story"])
    create_thumbnail(data["thumb_text"])
    create_shorts("long_video.mp4")
    tags = data["hashtags"].replace("#","").split()
    desc_long = f"{data['description']}\n\n{data['story'][:400]}\n\n{data['hashtags']}"
    upload_youtube("long_video.mp4", "thumb.jpg", data["title"], desc_long, tags, False)
    upload_youtube("shorts.mp4", "thumb.jpg", data["thumb_text"], data["hashtags"] + " #Shorts", tags, True)

if __name__ == "__main__":
    asyncio.run(main())
