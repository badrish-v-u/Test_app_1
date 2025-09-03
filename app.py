from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
import os, requests
from bs4 import BeautifulSoup

API_TOKEN = os.getenv("API_TOKEN")            # set in the dashboard later
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],  # later: set to your site origin
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health():
    return {"ok": True}

@app.get("/scrape")
def scrape(url: str, x_token: str | None = Header(default=None)):
    if API_TOKEN and x_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MyDemoBot/1.0; +https://yourdomain.com)"
    }
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.title.string.strip() if soup.title else None
    return {"title": title, "length": len(r.text)}
