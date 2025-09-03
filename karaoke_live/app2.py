from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import os, re, subprocess, time
from pathlib import Path
from typing import Optional
import requests
import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt
import yt_dlp

# ---------- Config ----------
API_TOKEN       = os.getenv("API_TOKEN")                 # optional
ALLOWED_ORIGIN  = os.getenv("ALLOWED_ORIGIN", "*")
DATA_DIR        = Path(os.getenv("DATA_DIR", "data")).resolve()
AUDIO_BR        = os.getenv("AUDIO_BR", "160k")
LOWPASS_HZ      = int(os.getenv("LOWPASS_HZ", "120"))
SAMPLE_RATE     = int(os.getenv("SAMPLE_RATE", "44100"))
PUBLIC_BASE_HOST= os.getenv("PUBLIC_BASE_HOST")          # e.g. https://yourdomain.com
PUSH_URL        = os.getenv("PUSH_URL")                  # e.g. https://yourdomain.com/karaoke-receiver.php
PUSH_TOKEN      = os.getenv("PUSH_TOKEN")                # shared secret with PHP

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Karaoke Builder (app2)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/files", StaticFiles(directory=str(DATA_DIR)), name="files")

# ---------- Utils ----------
def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"{' '.join(cmd)}\n{e.stderr.decode(errors='ignore')}") from e

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def butter_lowpass_sos(sr: int, cutoff_hz: int):
    from scipy.signal import butter
    return butter(4, cutoff_hz / (sr / 2), btype="low", output="sos")

def vocal_cancel_mid_side(src_wav: Path, out_wav: Path, keep_bass_hz=120):
    data, sr = sf.read(str(src_wav), always_2d=True, dtype="float32")
    if data.shape[1] == 1:
        sf.write(str(out_wav), data, sr); return
    L, R = data[:,0], data[:,1]
    M, S = 0.5*(L+R), 0.5*(L-R)
    sos = butter_lowpass_sos(sr, keep_bass_hz)
    M_low = sosfiltfilt(sos, M)
    outL, outR = S + M_low, (-S) + M_low
    out = np.stack([outL, outR], axis=1)
    peak = float(np.max(np.abs(out)) or 1.0)
    if peak > 1.0: out /= peak
    sf.write(str(out_wav), out, sr)

def to_wav(src: Path, out_wav: Path, sr=44100):
    _run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(src),"-ac","2","-ar",str(sr),str(out_wav)])

def encode_mp3(src_wav: Path, out_mp3: Path, br="160k"):
    _run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(src_wav),"-c:a","libmp3lame","-b:a",br,str(out_mp3)])

def export_original_mp3(src_any: Path, out_mp3: Path, br="160k"):
    _run(["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(src_any),"-vn","-c:a","libmp3lame","-b:a",br,str(out_mp3)])

def pick_sub(info: dict) -> Optional[str]:
    """Return a caption URL (vtt preferred) if present."""
    def choose(subs: Optional[dict]):
        if not subs: return None
        # prefer 'en' if available
        langs = ["en"] + [k for k in subs.keys() if k!="en"]
        for lang in langs:
            arr = subs.get(lang) or []
            if arr:
                arr = sorted(arr, key=lambda x: 0 if x.get("ext")=="vtt" else 1)
                return arr[0].get("url")
        return None
    return choose(info.get("subtitles")) or choose(info.get("automatic_captions"))

def save_text(path: Path, text: str):
    path.write_text(text, encoding="utf-8")

def try_lrclib(title: str, artist: Optional[str]) -> Optional[str]:
    try:
        r = requests.get("https://lrclib.net/api/search",
                         params={"track_name": title, "artist_name": artist or ""},
                         timeout=8)
        if r.ok and isinstance(r.json(), list) and r.json():
            hit = r.json()[0]
            return (hit.get("syncedLyrics") or hit.get("plainLyrics") or "").strip() or None
    except Exception:
        pass
    return None

def push_to_host(video_id: str, title: str, uploader: str, inst_mp3: Path, orig_mp3: Path, lyrics: Optional[Path]):
    if not (PUSH_URL and PUSH_TOKEN): return None
    files = {
        "instrumental_mp3": (inst_mp3.name, open(inst_mp3,"rb"), "audio/mpeg"),
        "original_mp3":     (orig_mp3.name, open(orig_mp3,"rb"), "audio/mpeg"),
    }
    if lyrics and lyrics.exists():
        ext = lyrics.suffix.lower()
        mime = "text/vtt" if ext==".vtt" else "text/plain"
        files["lyrics"] = (lyrics.name, open(lyrics,"rb"), mime)
    try:
        r = requests.post(PUSH_URL,
                          data={"video_id": video_id, "title": title, "uploader": uploader},
                          files=files,
                          headers={"X-Upload-Token": PUSH_TOKEN},
                          timeout=180)
        r.raise_for_status()
        return r.json()
    finally:
        for k,v in files.items():
            try: v[1].close()
            except Exception: pass

def process(url: str) -> dict:
    url = url.strip()
    if not url: raise ValueError("Empty URL")
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(DATA_DIR / "%(id)s" / "source.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        vid = info.get("id") or ""
        if not vid: raise RuntimeError("No video id")
        title = _norm(info.get("title"))
        uploader = _norm(info.get("uploader") or info.get("channel"))
        duration = info.get("duration")
        base = DATA_DIR / vid
        base.mkdir(parents=True, exist_ok=True)
        src = next((p for p in base.iterdir() if p.name.startswith("source.")), None)
        if src is None: src = Path(ydl.prepare_filename(info))

    wav = base / "source.wav"
    to_wav(src, wav, SAMPLE_RATE)

    inst_wav = base / "instrumental.wav"
    vocal_cancel_mid_side(wav, inst_wav, keep_bass_hz=LOWPASS_HZ)

    inst_mp3 = base / "instrumental.mp3"
    orig_mp3 = base / "original.mp3"
    encode_mp3(inst_wav, inst_mp3, AUDIO_BR)
    export_original_mp3(src, orig_mp3, AUDIO_BR)

    # Captions/Lyrics
    lyrics_url = None
    vtt_path = base / "lyrics.vtt"
    lrc_path = base / "lyrics.lrc"
    sub_url = pick_sub(info)
    if sub_url:
        try:
            r = requests.get(sub_url, timeout=10)
            if r.ok and r.text.strip():
                save_text(vtt_path, r.text)
                lyrics_url = f"/files/{vid}/lyrics.vtt"
        except Exception:
            pass
    if not lyrics_url:
        # Try LRCLIB fallback
        m = re.match(r"(.+?)\s*-\s*(.+)", title or "")
        artist_name, song_title = (m.group(1), m.group(2)) if m else (uploader, title)
        lrc = try_lrclib(song_title, artist_name)
        if lrc:
            save_text(lrc_path, lrc)
            lyrics_url = f"/files/{vid}/lyrics.lrc"

    # Push to your hosting
    pushed = push_to_host(
        vid, title or "", uploader or "",
        inst_mp3=inst_mp3,
        orig_mp3=orig_mp3,
        lyrics=(vtt_path if vtt_path.exists() else (lrc_path if lrc_path.exists() else None))
    )

    # Build response (prefer your hosting URLs if push succeeded)
    def make_url(local_rel: str) -> str:
        if pushed and isinstance(pushed, dict) and PUBLIC_BASE_HOST:
            # PHP returns relative paths like /data/<vid>/file
            key = "instrumental_mp3" if local_rel.endswith("instrumental.mp3") else \
                  "original_mp3" if local_rel.endswith("original.mp3") else "lyrics"
            if pushed.get(key):
                return f"{PUBLIC_BASE_HOST}{pushed[key]}"
        # fallback: serve from Render
        return local_rel

    base_rel = f"/files/{vid}"
    return {
        "ok": True,
        "video_id": vid,
        "title": title,
        "uploader": uploader,
        "duration_sec": duration,
        "files": {
            "instrumental_mp3": make_url(f"{base_rel}/instrumental.mp3"),
            "original_mp3":     make_url(f"{base_rel}/original.mp3"),
            "lyrics":           make_url(lyrics_url) if lyrics_url else None
        }
    }

# ---------- Routes ----------
@app.get("/")
def health():
    return {"ok": True, "service": "karaoke-builder"}

@app.post("/process")
def process_endpoint(
    url: str = Query(..., description="YouTube URL"),
    x_token: Optional[str] = Header(default=None),
):
    if API_TOKEN and x_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")
    t0 = time.time()
    try:
        res = process(url)
        res["elapsed_sec"] = round(time.time() - t0, 3)
        return JSONResponse(res)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"yt-dlp error: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
