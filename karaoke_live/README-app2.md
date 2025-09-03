# Karaoke Builder (app2)

POST `/process?url=...` → returns MP3 (instrumental & original) and optional lyrics.
If `PUSH_URL` + `PUSH_TOKEN` + `PUBLIC_BASE_HOST` set, files are uploaded to your hosting and response URLs point there.

Env:
- API_TOKEN (optional)
- ALLOWED_ORIGIN
- AUDIO_BR (default 160k)
- LOWPASS_HZ (default 120)
- SAMPLE_RATE (default 44100)
- PUSH_URL, PUSH_TOKEN, PUBLIC_BASE_HOST

Render:
- Build: `./render-build.sh`
- Start: `uvicorn app2:app --host 0.0.0.0 --port $PORT`
