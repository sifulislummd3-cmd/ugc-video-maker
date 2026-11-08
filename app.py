import asyncio
import io
import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import edge_tts
import streamlit as st
from google import genai
from google.genai import types
from PIL import Image, ImageFilter

st.set_page_config(page_title="UGC Video Maker", page_icon="🎬", layout="centered")

API_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

W, H = 720, 1280  # output size (fast, looks fine on phones)


@st.cache_data(ttl=3600)
def pick_model(key):
    pref = st.secrets.get("GEMINI_MODEL", "")
    if pref:
        return pref
    try:
        client = genai.Client(api_key=key)
        best, best_score = None, None
        bad = ("lite", "image", "tts", "live", "audio", "embed",
               "robotics", "computer", "thinking")
        for m in client.models.list():
            name = m.name.replace("models/", "")
            acts = getattr(m, "supported_actions", None) or []
            if "generateContent" not in acts or "flash" not in name:
                continue
            if any(b in name for b in bad):
                continue
            v = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
            ver = (int(v.group(1)), int(v.group(2) or 0)) if v else (0, 0)
            stable = 0 if ("preview" in name or "exp" in name) else 1
            score = (ver, stable)
            if best_score is None or score > best_score:
                best, best_score = name, score
        if best:
            return best
    except Exception:
        pass
    return "gemini-2.5-flash"


MODEL = pick_model(API_KEY)

# label: (edge voice, language name, gTTS language code)
VOICES = {
    "বাংলা": ("bn-BD-NabanitaNeural", "Bengali", "bn"),
    "English": ("en-US-AriaNeural", "English", "en"),
}

PROMPT = """You are an expert short-form UGC video ad strategist (TikTok, Instagram Reels).
Look at the product image and create a {secs}-second vertical UGC-style ad in {lang}.

Structure (use exactly these roles, in order):
1. hook (first 1-3 seconds, must stop the scroll: problem, curiosity or bold claim)
2. problem (viewer recognises their own pain)
3. solution (show the product as the answer, how it is used)
4. proof (benefit, result or social proof, no fake statistics)
5. cta (clear call to action with a reason to act now)

Rules:
- Spoken like a real person talking to a friend, not an ad.
- Total spoken length must fit about {secs} seconds ({words} words in total across all scenes).
- Only claim what is visible or reasonable from the image. No invented numbers, no medical claims.
- Also give 5 alternative hooks.

Return ONLY JSON:
{{"product_name": "...", "target_audience": "...",
 "alt_hooks": ["...","...","...","...","..."],
 "scenes": [{{"role": "hook", "voiceover": "...", "caption": "short on-screen text, max 8 words"}}, ...]}}"""

RETRY_WORDS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
               "500", "INTERNAL", "overloaded", "high demand")


def shrink_image(data):
    """Smaller upload = faster Gemini call."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((1024, 1024))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def analyze(image_bytes, lang, secs):
    words = int(secs * (2.0 if lang == "Bengali" else 2.5))
    client = genai.Client(api_key=API_KEY)
    models = []
    for m in [MODEL, "gemini-2.5-flash", "gemini-2.5-flash-lite"]:
        if m not in models:
            models.append(m)
    last = None
    for model in models:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                        PROMPT.format(secs=secs, lang=lang, words=words),
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"),
                )
                text = re.sub(r"^```json|```$", "", resp.text.strip()).strip()
                return json.loads(text)
            except Exception as e:
                last = e
                msg = str(e)
                if "404" in msg or "NOT_FOUND" in msg:
                    break  # this model does not exist, try the next one
                if isinstance(e, json.JSONDecodeError) or any(
                        k in msg for k in RETRY_WORDS):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
    raise last


def make_tts(text, voice, gtts_lang, path):
    """Edge-TTS first (2 tries), then free gTTS as a backup."""
    for attempt in range(2):
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate="+5%").save(path))
            if os.path.exists(path) and os.path.getsize(path) > 500:
                return
        except Exception:
            time.sleep(1)
    from gtts import gTTS  # backup voice
    gTTS(text=text, lang=gtts_lang).save(path)


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def compose_frame(src, dst):
    """Canvas: blurred background + product centered."""
    img = Image.open(src).convert("RGB")
    cw, ch = 1080, 1920
    bg = img.copy()
    scale = max(cw / bg.width, ch / bg.height)
    bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1))
    left = (bg.width - cw) // 2
    top = (bg.height - ch) // 2
    bg = bg.crop((left, top, left + cw, top + ch)).filter(ImageFilter.GaussianBlur(40))
    fg_scale = min(980 / img.width, 1200 / img.height)
    fg = img.resize((int(img.width * fg_scale), int(img.height * fg_scale)))
    bg.paste(fg, ((cw - fg.width) // 2, (ch - fg.height) // 2 - 80))
    bg.save(dst, quality=90)


def ass_file(text, dur, path):
    def t(s):
        return f"0:{int(s // 60):02d}:{s % 60:05.2f}"

    text = text.replace("\n", " ")
    content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Noto Sans Bengali,68,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,2,2,80,80,330,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,{t(0)},{t(dur)},Default,,0,0,0,,{text}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def render_scene(i, sc, voice, gtts_lang, workdir, frame):
    mp3 = os.path.join(workdir, f"a{i}.mp3")
    make_tts(sc["voiceover"], voice, gtts_lang, mp3)
    dur = duration(mp3) + 0.3
    ass = f"s{i}.ass"
    ass_file(sc.get("caption") or sc["voiceover"], dur, os.path.join(workdir, ass))
    frames = int(dur * 25) + 1
    if i % 2 == 0:
        z = "min(zoom+0.0008,1.15)"
    else:
        z = "if(eq(on,0),1.15,max(zoom-0.0008,1.0))"
    vf = (
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={W}x{H}:fps=25,"
        f"subtitles={ass},format=yuv420p"
    )
    out = os.path.join(workdir, f"c{i}.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", frame, "-i", mp3,
         "-vf", vf, "-t", f"{dur:.2f}", "-r", "25", "-threads", "2",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
         "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2",
         "-af", "apad", "-shortest", out],
        check=True, capture_output=True, cwd=workdir,
    )
    return i, out


def build_video(image_path, scenes, voice, gtts_lang, workdir, progress):
    frame = os.path.join(workdir, "frame.jpg")
    compose_frame(image_path, frame)
    n = len(scenes)
    clips = {}
    done = 0
    progress(0.05, "ভয়েস ও দৃশ্য একসাথে তৈরি হচ্ছে...")
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(render_scene, i, sc, voice, gtts_lang, workdir, frame)
                for i, sc in enumerate(scenes)]
        for f in as_completed(futs):
            i, path = f.result()
            clips[i] = path
            done += 1
            progress(0.05 + 0.85 * done / n, f"{done}/{n} দৃশ্য তৈরি হয়েছে...")
    lst = os.path.join(workdir, "list.txt")
    with open(lst, "w") as f:
        for i in range(n):
            f.write(f"file '{clips[i]}'\n")
    final = os.path.join(workdir, "final.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
         "-c", "copy", "-movflags", "+faststart", final],
        check=True, capture_output=True,
    )
    progress(1.0, "শেষ!")
    return final


# ---------------- UI ----------------
st.title("🎬 UGC ভিডিও মেকার")
st.caption("প্রোডাক্টের ছবি দিন, বাকিটা অটোমেটিক। শেষে ভিডিও ডাউনলোড করুন।")

if not API_KEY:
    st.error("GEMINI_API_KEY সেট করা নেই। Streamlit Secrets-এ যোগ করুন।")
    st.stop()

up = st.file_uploader("প্রোডাক্টের ছবি আপলোড করুন", type=["jpg", "jpeg", "png", "webp"])
c1, c2 = st.columns(2)
lang_label = c1.selectbox("ভাষা", list(VOICES.keys()))
secs = c2.selectbox("ভিডিওর দৈর্ঘ্য (সেকেন্ড)", [15, 30, 60], index=0)

if up and st.button("ভিডিও তৈরি করুন", type="primary", use_container_width=True):
    voice, lang, gl = VOICES[lang_label]
    t0 = time.time()
    try:
        with st.spinner("প্রোডাক্ট বিশ্লেষণ ও স্ক্রিপ্ট লেখা হচ্ছে..."):
            small = shrink_image(up.getvalue())
            data = analyze(small, lang, secs)
        st.subheader(data.get("product_name", "প্রোডাক্ট"))
        st.caption(f"টার্গেট: {data.get('target_audience', '')}")

        with st.expander("স্ক্রিপ্ট দেখুন", expanded=True):
            for sc in data["scenes"]:
                st.markdown(f"**{sc['role'].upper()}**: {sc['voiceover']}")
        with st.expander("বিকল্প ৫টা হুক"):
            for h in data.get("alt_hooks", []):
                st.write("•", h)

        with tempfile.TemporaryDirectory() as wd:
            img_path = os.path.join(wd, "product.jpg")
            with open(img_path, "wb") as f:
                f.write(small)
            bar = st.progress(0.0, "শুরু হচ্ছে...")
            final = build_video(img_path, data["scenes"], voice, gl, wd,
                                lambda p, m: bar.progress(min(p, 1.0), m))
            with open(final, "rb") as f:
                video_bytes = f.read()
        st.video(video_bytes)
        st.caption(f"⏱ মোট সময়: {time.time() - t0:.0f} সেকেন্ড")
        st.download_button("MP4 ডাউনলোড করুন", video_bytes, "ugc_video.mp4",
                           "video/mp4", use_container_width=True)
    except subprocess.CalledProcessError as e:
        st.error("ভিডিও রেন্ডারে সমস্যা হয়েছে।")
        st.code((e.stderr or b"").decode("utf-8", "ignore")[-1500:])
    except Exception as e:
        st.error(f"সমস্যা হয়েছে: {e}")
