from __future__ import annotations
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
# Support standard URLs and shortlinks even without http/https schema
URL_RE = re.compile(r"(?:https?://[^\s<>\"']+)|(?:(?:bit\.ly|heylink\.me|s\.id|lynk\.id|linktr\.ee|forms\.gle|taplink\.cc)/[^\s<>\"']+)", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")
GUIDEBOOK_KEYWORDS = {"guidebook", "panduan", "juknis", "ketentuan", "booklet", "syarat", "rulebook", "bit.ly/panduan", "bit.ly/juknis"}
MAHASISWA_KEYWORDS = {"mahasiswa", "universitas", "kampus", "s1", "d3", "d4", "umum", "undergraduate", "diploma", "student"}
BLOCKED_SOCIAL_HOSTS = {"instagram.com", "facebook.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com", "wa.me", "api.whatsapp.com"}

# --- LLM Prompt Configuration ---
_LLM_PROMPT_PREFIX = (
    "Ekstrak data lomba ke JSON array: "
    '[{"i":"id","j":"judul_bersih","c":"caption","l":["link_daftar"],"d":"deadline","p":"prizepool","t":"topik","o":"penyelenggara"}]'
    "\nAturan:"
    "\n- c: Ringkas info (event, target peserta, benefit) MAKS 3 kalimat padat."
    "\n- l: HANYA link form (bit.ly, gform, heylink). Abaikan link WA/Juknis."
    "\n- t: Kategori (IT/Bisnis/Design/Poster/Data/Mobile/Game/Multimedia/IoT/Robotics/Sastra/Seni/Lainnya)."
    "\n- d: Tgl penutupan (DD MMMM YYYY) atau '-'."
    "\n- p: Total nominal hadiah atau '-'."
    "\n- j: Nama lomba saja, buang emoji/sapaan/tanggal."
    "\nInput:\n"
)

# --- Utility Helpers ---
def make_id(title: str, source: str) -> str:
    normalized = f"{source}_{title}".lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:12]

def clean_url(url: str, base_url: str = "") -> str:
    if not url: return ""
    url = url.strip().strip(".,;:!?\"')]}")
    
    if not url.startswith("http"):
        if url.startswith("//"): 
            url = "https:" + url
        elif base_url and url.startswith("/"): 
            url = urljoin(base_url, url)
        else:
            url = "https://" + url 
            
    return url if url.startswith(("http://", "https://")) else ""

def normalize_space(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text or "").strip()

def strip_noise(text: str) -> str:
    text = "".join(ch if unicodedata.category(ch)[0] != "S" else " " for ch in (text or ""))
    return normalize_space(re.sub(r"[@#*_>|\"']+", " ", text))

def is_mahasiswa(text: str) -> bool:
    lower = (text or "").lower()
    return any(kw in lower for kw in MAHASISWA_KEYWORDS)

def compress_text_for_llm(text: str) -> str:
    # Token diet: Remove hashtags and standard social media call-to-actions
    text = re.sub(r'#\w+', '', text)
    noise_patterns = r'(?i)(silomba hanya media|jangan lupa follow|tag teman|klik link di bio|share postingan ini|bantu sebar|info lebih lanjut hubungi).*'
    text = re.sub(noise_patterns, '', text)
    text = strip_noise(text)
    # Core event info is usually in the first 800 characters
    return text[:800]

def safe_extract_json(text: str) -> list:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return []

# --- Link Extraction Logic ---
def extract_registration_links(text: str, anchors: list[dict] = None) -> list[str]:
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
    if not GEMINI_CLIENT or not batch: return batch
    
    payload = [
        {
            "i": item["id"], 
            "text": compress_text_for_llm(item["caption"]), 
            "title": item["judul"]
        } 
        for item in batch
    ]
    
    try:
        response = GEMINI_CLIENT.models.generate_content(
            model="gemini-2.0-flash-lite",
            # separators=(',', ':') removes whitespace to save tokens
            contents=_LLM_PROMPT_PREFIX + json.dumps(payload, separators=(',', ':')),
        )
        llm_results = safe_extract_json(response.text)
        llm_map = {str(r["i"]): r for r in llm_results if isinstance(r, dict)}
    except Exception as e:
        print(f"LLM Error: {e}")
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
            
            # Isolate article content to avoid menu and footer elements
            content_div = dsoup.find("div", class_="entry-content") or dsoup.find("article")
            text = content_div.get_text("\n") if content_div else dsoup.get_text("\n")

            if not is_mahasiswa(text): continue
            
            title = strip_noise(dsoup.find("h1").get_text() if dsoup.find("h1") else link)
            uid = make_id(title, "infolomba.id")
            if uid in seen_ids: continue

            results.append({
                "id": uid,
                "sumber": "infolomba.id",
                "judul": title,
                "poster": best_poster_from_soup(dsoup, base),
                "caption": text.strip()[:2000],
                "link_pendaftaran": extract_registration_links(text),
                "link_direct": link
            })
    except Exception as e: print(f"infolomba error: {e}")
    return results

async def scrape_silomba(seen_ids: set) -> list:
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
                
                # Isolate the exact description text
                full_text = dsoup.get_text("\n")
                clean_text = full_text
                if "Deskripsi Lomba" in full_text:
                    clean_text = full_text.split("Deskripsi Lomba")[-1]
                    if "Silomba hanya media" in clean_text:
                        clean_text = clean_text.split("Silomba hanya media")[0]
                
                if not is_mahasiswa(clean_text): continue
                
                title = strip_noise(dsoup.find("h1").get_text() if dsoup.find("h1") else "Lomba")
                uid = make_id(title, "silomba.id")
                if uid in seen_ids: continue

                results.append({
                    "id": uid,
                    "sumber": "silomba.id",
                    "judul": title,
                    "poster": best_poster_from_soup(dsoup, base),
                    "caption": clean_text.strip()[:2000],
                    "link_pendaftaran": extract_registration_links(clean_text),
                    "link_direct": href
                })
        except Exception as e: print(f"silomba error: {e}")
        finally: await browser.close()
    return results

def scrape_instagram(seen_ids: set) -> list:
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
            
            elems = driver.find_elements(By.CSS_SELECTOR, "a[href*='/p/'], a[href*='/reel/']")
            links = [e.get_attribute("href") for e in elems if e.get_attribute("href")]
            
            for url in links[:MAX_IG_POSTS_PER_ACCOUNT]:
                driver.get(url)
                time.sleep(4)
                
                caption = ""
                try:
                    h1s = driver.find_elements(By.TAG_NAME, "h1")
                    if h1s: caption = h1s[0].text
                except Exception:
                    pass
                
                if not is_mahasiswa(caption): continue
                
                try:
                    shortcode = url.rstrip('/').split('/')[-1]
                    uid = make_id(shortcode, f"IG_{acc}")
                except Exception:
                    continue
                    
                if uid in seen_ids: continue

                poster = ""
                try:
                    if imgs := driver.find_elements(By.CSS_SELECTOR, "article img"):
                        poster = imgs[0].get_attribute("src")
                except Exception:
                    pass

                results.append({
                    "id": uid,
                    "sumber": f"IG @{acc}",
                    "judul": "Instagram Post",
                    "poster": poster,
                    "caption": caption,
                    "link_pendaftaran": extract_registration_links(caption),
                    "link_direct": url
                })
    except Exception as e: 
        print(f"IG error: {e}")
    finally: 
        driver.quit()
    return results

# --- Main Logic ---
async def main():
    if not MONGO_URI: return
    client = pymongo.MongoClient(MONGO_URI)
    col = client[DB_NAME][COLLECTION]
    
    db_data = list(col.find({}, {"id": 1}))
    seen_ids = {d["id"] for d in db_data}
    
    web_data, silomba_data, ig_data = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, seen_ids),
        scrape_silomba(seen_ids),
        asyncio.to_thread(scrape_instagram, seen_ids)
    )
    
    raw_items = web_data + silomba_data + ig_data
    if not raw_items:
        client.close()
        return

    final_data = []
    # Batch items to fit comfortably in LLM context limits
    for i in range(0, len(raw_items), 10):
        batch = raw_items[i:i+10]
        final_data.extend(process_batch_with_gemini(batch))

    if final_data:
        ops = [UpdateOne({"id": item["id"]}, {"$set": item}, upsert=True) for item in final_data]
        col.bulk_write(ops)
    
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
