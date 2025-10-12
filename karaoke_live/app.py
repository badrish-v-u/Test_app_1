#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
from fastapi.responses import JSONResponse
from starlette.staticfiles import StaticFiles

APP_DIR = Path(__file__).parent.resolve()
SONGS_DIR = APP_DIR / "all_songs"
SONGS_DIR.mkdir(parents=True, exist_ok=True)

API_TOKEN = os.getenv("API_TOKEN", "8d8318e97ecc2a5465bdde27a967e25a8ec0af0fd3e3a3c7f6a805a2cad88331")  # set this in Render → Environment

# ----- security dependency -----
def require_token(request: Request):
    if not API_TOKEN:
        return  # token check disabled if env not set
    tok = request.headers.get("x-api-token") or request.query_params.get("token")
    if tok != API_TOKEN:
        raise HTTPException(401, "Unauthorized")

# ----- utils -----
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

_SINGLE_VIDEO_SAFE_PARAMS = {"v", "t", "time_continue"}

def sanitize_to_single_video(url: str) -> str:
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        if host not in {"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}:
            return url
        if host.endswith("youtu.be"):
            video_id = u.path.strip("/").split("/")[0]
            if video_id:
                new_q = dict(parse_qsl(u.query or ""))
                new_q["v"] = video_id
                return f"https://www.youtube.com/watch?{urlencode(new_q)}"
        q = dict(parse_qsl(u.query or ""))
        if "v" in q:
            clean_q = {k: v for k, v in q.items() if k in _SINGLE_VIDEO_SAFE_PARAMS}
            cleaned = u._replace(query=urlencode(clean_q), path="/watch")
            return urlunparse(cleaned)
        return url
    except Exception:
        return url

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
    if have_cmd("ffmpeg"):
        return Path(shutil.which("ffmpeg"))

    tmp_home = APP_DIR / ".portable_ffmpeg"
    tmp_home.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = tmp_home / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if ffmpeg_bin.exists():
        return ffmpeg_bin

    try:
        system = platform.system().lower()

        if system == "darwin":
            index_url = "https://evermeet.cx/ffmpeg/"
            html = http_get(index_url).read().decode("utf-8", errors="ignore")
            zips = re.findall(r'href="(ffmpeg-\d+(?:\.\d+)*\.zip)"', html)
            if not zips: raise RuntimeError("No macOS zip found")
            def vkey(s): return tuple(map(int, re.findall(r"(\d+)", s)))
            best_zip = sorted(zips, key=vkey)[-1]
            data = http_get(index_url + best_zip).read()
            extracted = _extract_ffmpeg_from_zip(data, tmp_home, {"ffmpeg"})
            if "ffmpeg" in extracted:
                return extracted["ffmpeg"]

        elif system == "windows":
            api = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
            meta = json.loads(http_get(api, headers={"User-Agent": "curl"}).read().decode("utf-8"))
            assets = meta.get("assets", [])
            cand = [a for a in assets if ("win64" in a["name"].lower() and "gpl" in a["name"].lower() and (a["name"].lower().endswith(".zip") or a["name"].lower().endswith(".tar.xz")))]
            if not cand: raise RuntimeError("No Windows asset found")
            asset = cand[0]
            bin_data = http_get(asset["browser_download_url"]).read()
            if asset["name"].lower().endswith(".zip"):
                extracted = _extract_ffmpeg_from_zip(bin_data, tmp_home, {"ffmpeg.exe"})
            else:
                extracted = _extract_ffmpeg_from_tar_xz(bin_data, tmp_home, {"ffmpeg.exe"})
            if "ffmpeg.exe" in extracted:
                return extracted["ffmpeg.exe"]

        else:
            api = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
            meta = json.loads(http_get(api, headers={"User-Agent": "curl"}).read().decode("utf-8"))
            assets = meta.get("assets", [])
            cand = [a for a in assets if ("linux64" in a["name"].lower() and "gpl" in a["name"].lower() and a["name"].lower().endswith(".tar.xz"))]
            if not cand: raise RuntimeError("No Linux asset found")
            asset = cand[0]
            bin_data = http_get(asset["browser_download_url"]).read()
            extracted = _extract_ffmpeg_from_tar_xz(bin_data, tmp_home, {"ffmpeg"})
            if "ffmpeg" in extracted:
                return extracted["ffmpeg"]
    except Exception:
        pass

    return None

def best_format(ffmpeg_ok: bool) -> str:
    return "bestaudio/best" if ffmpeg_ok else "bestaudio[ext=m4a]/bestaudio/bestaudio*"

# ----- app -----
app = FastAPI(title="Karaoke Audio Downloader")

# CORS: since you’ll call through proxy.php, this can remain permissive or tightened later.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/all_songs", StaticFiles(directory=str(SONGS_DIR), html=False), name="all_songs")

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/songs", dependencies=[Depends(require_token)])
def list_songs(request: Request):
    base = str(request.base_url).rstrip("/")
    files = []
    for p in sorted(SONGS_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".opus", ".webm"}:
            rel = f"/all_songs/{p.name}"
            files.append({
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "url": rel,
                "url_absolute": base + rel,
            })
    return {"count": len(files), "items": files}

@app.post("/download", dependencies=[Depends(require_token)])
async def download_song(payload: dict):
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
    ffmpeg_ok = ffmpeg_path is not None

    ydl_opts = {
        "noplaylist": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "nocheckcertificate": True,
        "quiet": False,
        "outtmpl": str(SONGS_DIR / "%(title)s.%(ext)s"),
        "format": best_format(ffmpeg_ok),
        "ignoreerrors": False,
    }

    if ffmpeg_ok:
        ydl_opts["ffmpeg_location"] = str(ffmpeg_path.parent)
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
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
    return {"service": "Karaoke Audio Downloader", "routes": ["/download (POST)", "/songs (GET)", "/all_songs/* (static)", "/health (GET)"]}
