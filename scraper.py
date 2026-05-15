from future import annotations
import asyncio
import hashlib
import json
import os
import random
import re
import time
import unicodedata
from urllib.parse import urljoin, urlparse

import cloudscraper
import pymongo
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from pymongo import UpdateOne
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

# --- Configuration ---
IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
MONGO_URI = os.environ.get("MONGO_URI")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DB_NAME = os.environ.get("DB_NAME", "competition_scraper")
COLLECTION = os.environ.get("COLLECTION", "competition")
IG_ACCOUNTS = [
    a.strip()
    for a in os.environ.get("IG_ACCOUNTS", "infolomba,infolomba_gratis,infolomba.olimpiade").split(",")
    if a.strip()
]
MAX_WEB_ITEMS = int(os.environ.get("MAX_WEB_ITEMS", "15"))
MAX_IG_POSTS_PER_ACCOUNT = int(os.environ.get("MAX_IG_POSTS_PER_ACCOUNT", "6"))

# --- Compiled Patterns & Constants ---
URL_RE = re.compile(r"https?://[^\s<>'\"`\)\|\}\]]+", re.IGNORECASE)
INSTAGRAM_SHORTCODE_RE = re.compile(r"/(?:p|reel)/([^/?#]+)/?")
WHITESPACE_RE = re.compile(r"\s+")
GUIDEBOOK_KEYWORDS = {"guidebook", "panduan", "juknis", "ketentuan", "booklet", "syarat", "rulebook", "bit.ly/panduan", "bit.ly/juknis"}
MAHASISWA_KEYWORDS = {"mahasiswa", "universitas", "kampus", "s1", "d3", "d4", "umum", "undergraduate", "diploma", "student"}
FORM_HOSTS = {"forms.gle", "docs.google.com", "bit.ly", "s.id", "tinyurl.com", "lynk.id"}
BLOCKED_SOCIAL_HOSTS = {"instagram.com", "facebook.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com", "wa.me", "api.whatsapp.com"}

# --- LLM Prompt Configuration ---
# Instructions to summarize, clean titles, and extract specific metadata
_LLM_PROMPT_PREFIX = (
    "Extract and clean competition data for students. Return a JSON array of objects. "
    "Required Format: "
    '{"i":"id","j":"enhanced_title","c":"summarized_caption","l":["reg_links_only"],"d":"deadline","p":"prizepool","t":"topic","o":"organizer"}. '
    "\nRules: "
    "\n- j: Professional title only. Remove 'Open Registration', emojis, and dates."
    "\n- c: Regenerate the caption to include only essential info (what, who, when) in 3-4 sentences."
    "\n- l: Strictly registration/apply links. Remove guidebook, rules, or WhatsApp links."
    "\n- d: Extract specific closing date (DD MMMM YYYY). Use '-' if unknown."
    "\n- p: Total prize pool amount (e.g., Rp 5.000.000). Use '-' if unknown."
    "\n- t: Category (IT/Bisnis/Webdev/Design/Poster/Data/Mobile/Game/Multimedia/IoT/Robotics/Lainnya)."
    "\n- o: The organizing institution or community name."
    "\n\nData: "
)

# --- Utility Helpers ---
def make_id(title: str, source: str) -> str:
    normalized = f"{source}_{title}".lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:12]

def clean_url(url: str, base_url: str = "") -> str:
    if not url: return ""
    url = url.strip().strip(".,;:!?"')]}")
    if url.startswith("//"): url = "https:" + url
    elif base_url and url.startswith("/"): url = urljoin(base_url, url)
    return url if url.startswith(("http://", "https://")) else ""

def normalize_space(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text or "").strip()

def strip_noise(text: str) -> str:
    text = "".join(ch if unicodedata.category(ch)[0] != "S" else " " for ch in (text or ""))
    return normalize_space(re.sub(r'[@#*_>|"""'']+', " ", text))

def is_mahasiswa(text: str) -> bool:
    lower = (text or "").lower()
    return any(kw in lower for kw in MAHASISWA_KEYWORDS)

# --- Link Extraction Logic ---
def extract_registration_links(text: str, anchors: list[dict] = None) -> list[str]:
    # Filters out guidebook and social links to find actual registration forms
    anchors = anchors or []
    found, seen = [], set()
    
    all_urls = []
    for a in anchors:
        url = clean_url(a.get("url", ""))
        label = a.get("label", "").lower()
        if not any(kw in label for kw in GUIDEBOOK_KEYWORDS):
            all_urls.append(url)
    
    all_urls.extend([m.group(0) for m in URL_RE.finditer(text or "")])

    for raw in all_urls:
        url = clean_url(raw)
        if not url or url in seen or any(h in url.lower() for h in BLOCKED_SOCIAL_HOSTS):
            continue
        # Exclude links that likely point to documentation
        if any(kw in url.lower() for kw in GUIDEBOOK_KEYWORDS):
            continue
        
        found.append(url)
        seen.add(url)
    return found

# --- HTML Parsing Helpers ---
def best_poster_from_soup(soup: BeautifulSoup, base_url: str) -> str:
    for tag, attr in [("meta", "og:image"), ("meta", "twitter:image")]:
        node = soup.find(tag, attrs={"property": attr} if "og:" in attr else {"name": attr})
        if node and (url := clean_url(node.get("content", ""), base_url)): return url
    for img in soup.find_all("img"):
        src = clean_url(img.get("src") or img.get("data-src") or "", base_url)
        if src and not any(s in src.lower() for s in ("logo", "avatar", "profile", "icon")):
            return src
    return ""

# --- LLM Processing ---
def _create_gemini_client():
    if not GEMINI_API_KEY or not genai: return None
    try: return genai.Client(api_key=GEMINI_API_KEY)
    except Exception: return None

GEMINI_CLIENT = _create_gemini_client()

def process_batch_with_gemini(batch: list) -> list:
    # Uses AI to summarize captions and enhance metadata accuracy
    if not GEMINI_CLIENT or not batch: return batch
    
    payload = [{"i": item["id"], "text": item["caption"][:1500], "title": item["judul"]} for item in batch]
    
    try:
        response = GEMINI_CLIENT.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=_LLM_PROMPT_PREFIX + json.dumps(payload),
            config=genai_types.GenerateContentConfig(response_mime_type="application/json")
        )
        llm_results = json.loads(response.text)
        llm_map = {str(r["i"]): r for r in llm_results if isinstance(r, dict)}
    except Exception:
        return batch

    for item in batch:
        data = llm_map.get(item["id"])
        if data:
            item["judul"] = strip_noise(data.get("j", item["judul"]))
            item["caption"] = data.get("c", item["caption"])
            item["link_pendaftaran"] = data.get("l", item["link_pendaftaran"])
            item["deadline_pendaftaran"] = data.get("d", "-")
            item["prizepool"] = data.get("p", "-")
            item["topik"] = data.get("t", "Lainnya")
            item["penyelenggara"] = data.get("o", "Unknown")
    return batch

# --- Scrapers ---
def scrape_infolomba(seen_ids: set) -> list:
    # Scrapes infolomba.id website using cloudscraper
    base = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()
    results = []
    try:
        resp = scraper.get(base, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        links = {urljoin(base, a["href"]) for a in soup.find_all("a", href=lambda h: h and "info-" in h)}
        
        for link in list(links)[:MAX_WEB_ITEMS]:
            res = scraper.get(link, timeout=30)
            dsoup = BeautifulSoup(res.text, "html.parser")
            text = dsoup.get_text("\n")
            if not is_mahasiswa(text): continue
            
            title = strip_noise(dsoup.find("h1").get_text() if dsoup.find("h1") else link)
            uid = make_id(title, "infolomba.id")
            if uid in seen_ids: continue

            results.append({
                "id": uid,
                "sumber": "infolomba.id",
                "judul": title,
                "poster": best_poster_from_soup(dsoup, base),
                "caption": text[:2000],
                "link_pendaftaran": extract_registration_links(text),
                "link_direct": link
            })
    except Exception as e: print(f"infolomba error: {e}")
    return results

async def scrape_silomba(seen_ids: set) -> list:
    # Scrapes silomba.id using Playwright for dynamic content
    base = "https://silomba.id"
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(base, wait_until="networkidle")
            soup = BeautifulSoup(await page.content(), "html.parser")
            
            cards = soup.select("#competition-section a[href^='/lomba/']")
            for card in cards[:MAX_WEB_ITEMS]:
                href = urljoin(base, card["href"])
                await page.goto(href, wait_until="networkidle")
                dsoup = BeautifulSoup(await page.content(), "html.parser")
                text = dsoup.get_text("\n")
                
                title = strip_noise(dsoup.find("h1").get_text() if dsoup.find("h1") else "Lomba")
                uid = make_id(title, "silomba.id")
                if uid in seen_ids: continue

                results.append({
                    "id": uid,
                    "sumber": "silomba.id",
                    "judul": title,
                    "poster": best_poster_from_soup(dsoup, base),
                    "caption": text[:2000],
                    "link_pendaftaran": extract_registration_links(text),
                    "link_direct": href
                })
        except Exception as e: print(f"silomba error: {e}")
        finally: await browser.close()
    return results

def scrape_instagram(seen_ids: set) -> list:
    # Scrapes Instagram accounts using Selenium and session cookie
    if not IG_SESSION_ID: return []
    results = []
    opts = Options()
    opts.add_argument("--headless=new")
    driver = webdriver.Chrome(options=opts)
    
    try:
        driver.get("https://www.instagram.com/")
        driver.add_cookie({"name": "sessionid", "value": IG_SESSION_ID, "domain": ".instagram.com"})
        driver.refresh()
        
        for acc in IG_ACCOUNTS:
            driver.get(f"https://www.instagram.com/{acc}/")
            time.sleep(5)
            links = [e.get_attribute("href") for e in driver.find_elements(By.CSS_SELECTOR, "a[href*='/p/'], a[href*='/reel/']")]
            
            for url in links[:MAX_IG_POSTS_PER_ACCOUNT]:
                driver.get(url)
                time.sleep(4)
                caption = ""
                if h1s := driver.find_elements(By.TAG_NAME, "h1"): caption = h1s[0].text
                
                if not is_mahasiswa(caption): continue
                uid = make_id(url.split("/")[-2], f"IG_{acc}")
                if uid in seen_ids: continue

                poster = ""
                if imgs := driver.find_elements(By.CSS_SELECTOR, "article img"):
                    poster = imgs[0].get_attribute("src")

                results.append({
                    "id": uid,
                    "sumber": f"IG @{acc}",
                    "judul": "Instagram Post",
                    "poster": poster,
                    "caption": caption,
                    "link_pendaftaran": extract_registration_links(caption),
                    "link_direct": url
                })
    except Exception as e: print(f"IG error: {e}")
    finally: driver.quit()
    return results

# --- Main Logic ---
async def main():
    if not MONGO_URI: return
    client = pymongo.MongoClient(MONGO_URI)
    col = client[DB_NAME][COLLECTION]
    
    # Load existing IDs to avoid duplicates
    db_data = list(col.find({}, {"id": 1}))
    seen_ids = {d["id"] for d in db_data}
    
    # Run scrapers
    web_data, silomba_data, ig_data = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, seen_ids),
        scrape_silomba(seen_ids),
        asyncio.to_thread(scrape_instagram, seen_ids)
    )
    
    raw_items = web_data + silomba_data + ig_data
    if not raw_items:
        client.close()
        return

    # Process in batches for LLM optimization
    final_data = []
    for i in range(0, len(raw_items), 10):
        batch = raw_items[i:i+10]
        final_data.extend(process_batch_with_gemini(batch))

    # Save to Database
    if final_data:
        ops = [UpdateOne({"id": item["id"]}, {"$set": item}, upsert=True) for item in final_data]
        col.bulk_write(ops)
    
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
