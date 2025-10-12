#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Karaoke audio downloader microservice (FastAPI)
Features:
- POST /download { "url": "<YouTube URL>" } → downloads ONLY that video's audio
- GET  /songs → list downloaded files (served under /all_songs/*)
- GET  /health → { ok: true }
- Creates ./all_songs if not present
- Best-quality MP3 if FFmpeg available (auto-fetch static build); otherwise keeps best original (m4a/opus/webm)
- Sanitizes YouTube URLs to a single video (strips playlist/mix params)
- Token-protected routes (/download, /songs) via header X-Api-Token
- Safe CORS config

Env (Render → Environment):
  API_TOKEN = <long random string>
"""

import os
import re
import io
import json
import stat
import tarfile
import zipfile
import shutil
import platform
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

# ------------------- paths & config -------------------

APP_DIR = Path(__file__).parent.resolve()
SONGS_DIR = APP_DIR / "all_songs"
SONGS_DIR.mkdir(parents=True, exist_ok=True)

API_TOKEN = os.getenv("API_TOKEN", "")  # set this in Render

# ------------------- auth -------------------

def require_token(request: Request):
    """Require X-Api-Token for protected endpoints when API_TOKEN is set."""
    if not API_TOKEN:
        return
    tok = request.headers.get("x-api-token")
    if tok != API_TOKEN:
        raise HTTPException(401, "Unauthorized")

# ------------------- tiny utils -------------------

def have_cmd(name: str) -> bool:
    return shutil.which(name) is not None

def run(cmd):
    try:
        return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as e:
        return e

def pip_install(pkg: str):
    import sys
    run([sys.executable, "-m", "pip", "install", "-U", pkg])

def http_get(url: str, headers: dict = None, timeout: int = 60):
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout)

# ------------------- single-video sanitizer -------------------

_SINGLE_VIDEO_SAFE_PARAMS = {"v", "t", "time_continue"}

def sanitize_to_single_video(url: str) -> str:
    """
    If a YouTube URL includes playlist/mix/radio params, strip them so we get one video.
    Converts youtu.be short links to watch?v= form.
    """
    try:
        u = urlparse(url)
        host = u.netloc.lower()

        # short link → normal
        if host.endswith("youtu.be"):
            video_id = u.path.strip("/").split("/")[0]
            if video_id:
                new_q = dict(parse_qsl(u.query or ""))
                new_q["v"] = video_id
                return f"https://www.youtube.com/watch?{urlencode(new_q)}"

        # regular hosts
        if host in {"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com"}:
            q = dict(parse_qsl(u.query or ""))
            if "v" in q:
                clean_q = {k: v for k, v in q.items() if k in _SINGLE_VIDEO_SAFE_PARAMS}
                return urlunparse(u._replace(path="/watch", query=urlencode(clean_q)))

        return url
    except Exception:
        return url

# ------------------- yt-dlp & ffmpeg helpers -------------------

def ensure_yt_dlp():
    try:
        import yt_dlp  # noqa
        return
    except Exception:
        pip_install("yt-dlp")
    try:
        import yt_dlp  # noqa
    except Exception as e:
        raise RuntimeError(f"yt-dlp unavailable: {e}")

def _extract_ffmpeg_from_zip(zip_bytes: bytes, out_dir: Path, wanted_names):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        extracted = {}
        for name in z.namelist():
            base = Path(name).name.lower()
            if base in wanted_names:
                data = z.read(name)
                outp = out_dir / Path(base.replace(".exe", "")).name
                with open(outp, "wb") as f:
                    f.write(data)
                outp.chmod(outp.stat().st_mode | stat.S_IEXEC)
                extracted[base] = outp
        return extracted

def _extract_ffmpeg_from_tar_xz(tar_bytes: bytes, out_dir: Path, wanted_names):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:xz") as t:
        extracted = {}
        for m in t.getmembers():
            base = Path(m.name).name.lower()
            if base in wanted_names:
                f = t.extractfile(m)
                if f is None:
                    continue
                outp = out_dir / Path(base).name
                with open(outp, "wb") as out:
                    out.write(f.read())
                outp.chmod(outp.stat().st_mode | stat.S_IEXEC)
                extracted[base] = outp
        return extracted

def ensure_ffmpeg() -> Optional[Path]:
    """
    Try system ffmpeg; else fetch a portable static build (macOS: evermeet; Linux/Windows: BtbN).
    Returns Path or None.
    """
    if have_cmd("ffmpeg"):
        return Path(shutil.which("ffmpeg"))

    cache_dir = APP_DIR / ".portable_ffmpeg"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ff_bin = cache_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if ff_bin.exists():
        return ff_bin

    try:
        system = platform.system().lower()
        if system == "darwin":
            idx = "https://evermeet.cx/ffmpeg/"
            html = http_get(idx).read().decode("utf-8", "ignore")
            zips = re.findall(r'href="(ffmpeg-\d+(?:\.\d+)*\.zip)"', html)
            if not zips:
                raise RuntimeError("No macOS FFmpeg zip found")
            def vkey(s): return tuple(map(int, re.findall(r"(\d+)", s)))
            data = http_get(idx + sorted(zips, key=vkey)[-1]).read()
            ex = _extract_ffmpeg_from_zip(data, cache_dir, {"ffmpeg"})
            if "ffmpeg" in ex:
                return ex["ffmpeg"]
        else:
            api = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
            meta = json.loads(http_get(api, headers={"User-Agent": "curl"}).read().decode("utf-8"))
            assets = meta.get("assets", [])
            want = "linux64" if system == "linux" else "win64"
            cand = [a for a in assets if (want in a["name"].lower() and "gpl" in a["name"].lower())]
            if cand:
                a = cand[0]
                blob = http_get(a["browser_download_url"]).read()
                if a["name"].lower().endswith(".zip"):
                    ex = _extract_ffmpeg_from_zip(blob, cache_dir, {"ffmpeg.exe"})
                else:
                    ex = _extract_ffmpeg_from_tar_xz(blob, cache_dir, {"ffmpeg" if system == "linux" else "ffmpeg.exe"})
                return ex.get("ffmpeg") or ex.get("ffmpeg.exe")
    except Exception:
        pass

    # fallback: proceed without ffmpeg (keep original stream format)
    return None

def best_format(ffmpeg_ok: bool) -> str:
    return "bestaudio/best" if ffmpeg_ok else "bestaudio[ext=m4a]/bestaudio/bestaudio*"

# ------------------- FastAPI app -------------------

app = FastAPI(title="Karaoke Audio Downloader")

# IMPORTANT: explicit origin & no credentials keeps preflight simple
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.prophile.house"],  # adjust if you use a different domain too
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files: songs under /all_songs/*
app.mount("/all_songs", StaticFiles(directory=str(SONGS_DIR), html=False), name="all_songs")

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/songs", dependencies=[Depends(require_token)])
def list_songs(request: Request):
    base = str(request.base_url).rstrip("/")
    items = []
    for p in sorted(SONGS_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm"}:
            rel = f"/all_songs/{p.name}"
            items.append({
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "url": rel,
                "url_absolute": base + rel,
            })
    return {"count": len(items), "items": items}

@app.post("/download", dependencies=[Depends(require_token)])
async def download_song(payload: dict, request: Request):
    raw_url = (payload or {}).get("url", "").strip()
    if not raw_url:
        raise HTTPException(400, "Missing 'url'")

    try:
        ensure_yt_dlp()
    except Exception as e:
        raise HTTPException(500, f"yt-dlp error: {e}")
    import yt_dlp  # noqa

    target_url = sanitize_to_single_video(raw_url)
    ffmpeg_path = ensure_ffmpeg()
    ff_ok = ffmpeg_path is not None

    ydl_opts = {
        "noplaylist": True,
        "windowsfilenames": True,
        "nocheckcertificate": True,
        "quiet": False,
        "outtmpl": str(SONGS_DIR / "%(title)s.%(ext)s"),
        "format": best_format(ff_ok),
        "ignoreerrors": False,
    }
    if ff_ok:
        ydl_opts["ffmpeg_location"] = str(ffmpeg_path.parent)
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",  # best VBR
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([target_url])
    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")

    newest = None
    newest_mtime = -1
    for p in SONGS_DIR.iterdir():
        if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus"}:
            m = p.stat().st_mtime
            if m > newest_mtime:
                newest_mtime = m
                newest = p
    if not newest:
        raise HTTPException(500, "No output file created")

    rel = f"/all_songs/{newest.name}"
    return {
        "status": "ok",
        "filename": newest.name,
        "size_bytes": newest.stat().st_size,
        "url": rel,
        "url_absolute": str(request.base_url).rstrip("/") + rel,
    }

@app.get("/")
def root():
    return {"service": "Karaoke Audio Downloader",
            "routes": ["/download (POST)", "/songs (GET)", "/all_songs/* (static)", "/health (GET)"]}
