from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
import os
import re
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone

API_TOKEN = os.getenv("API_TOKEN")  # optional
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")
BASE_URL = os.getenv("BASE_URL", "https://www.similarsites.com/site/")  # prefix url

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 "
    "(compatible; SimilarSitesScraper/1.0; +https://yourdomain.com)"
)

def normalize_domain(s: str) -> str:
    """
    Accepts a bare domain like 'example.com' or a full URL.
    Returns just 'example.com'. Very light validation.
    """
    s = s.strip()
    if not s:
        raise ValueError("Empty domain")
    if "://" in s:
        parsed = urlparse(s)
        host = parsed.netloc
    else:
        # If user accidentally includes paths, strip them
        host = s.split("/")[0]
    # Drop leading 'www.'
    host = re.sub(r"^www\.", "", host, flags=re.IGNORECASE)
    # basic sanity check for a domain-like string
    if "." not in host or any(ch.isspace() for ch in host):
        raise ValueError(f"Invalid domain: {s}")
    return host

def fetch_similar_sites(domain: str):
    """
    Scrape SimilarSites for a single domain.
    Returns dict with similar sites, category rank, total visits, and category stats.
    """
    url = f"{BASE_URL.rstrip('/')}/{domain}"
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # --- Similar sites list (exclude .uk like the reference code) ---
    similar_sites_divs = soup.find_all(
        "div", class_="SimilarSitesCard__Domain-zq2ozc-4 kuvZIX"
    )
    raw_sites = [div.get_text(strip=True) for div in similar_sites_divs]
    # filter: no .uk and dedupe while keeping order
    seen = set()
    similar_sites = []
    for s in raw_sites:
        if s and not s.lower().endswith(".uk") and s not in seen:
            seen.add(s)
            similar_sites.append(s)

    # --- Metrics: Category rank & total visits (per provided classes) ---
    metric_elems = soup.find_all(
        "div", class_="SiteHeader__MetricValue-sc-1ybnx66-14 cLauOv"
    )
    # In the reference: first is category_rank, second is total_visits (if present)
    category_rank = metric_elems[0].get_text(strip=True) if len(metric_elems) >= 1 else None
    total_visits = metric_elems[1].get_text(strip=True) if len(metric_elems) >= 2 else None

    # --- Category distribution values (list) ---
    stats_values_elems = soup.find_all(
        "div", class_="StatisticsCategoriesDistribution__CategoryTitleValueWrapper-fnuckk-5 dvxqnd"
    )
    categories = [elem.get_text(strip=True) for elem in stats_values_elems if elem.get_text(strip=True)]

    return {
        "domain": domain,
        "source": url,
        "similar_sites": similar_sites,
        "counts": {
            "similar_sites": len(similar_sites),
        },
        "metrics": {
            "category_rank": category_rank,
            "total_visits": total_visits,
        },
        "categories": categories,              # list form
        "categories_joined": ", ".join(categories) if categories else None,  # if you want the joined text too
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/")
def health():
    return {"ok": True}

@app.get("/similar-sites")
def similar_sites(
    domain: str = Query(..., description="Domain like example.com (or full URL)"),
    x_token: str | None = Header(default=None),
    prefix_url: str | None = Query(None, description="Override the base prefix if needed"),
):
    # auth check
    if API_TOKEN and x_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="bad token")

    # allow per-request override of the prefix/base url if supplied
    global BASE_URL
    if prefix_url:
        BASE_URL = prefix_url

    try:
        normalized = normalize_domain(domain)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        data = fetch_similar_sites(normalized)
        return data
    except requests.HTTPError as e:
        # surface upstream HTTP status
        raise HTTPException(status_code=e.response.status_code if e.response else 502,
                            detail=f"Upstream error: {e}") from e
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Network error: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}") from e
