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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
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
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------
URL_RE = re.compile(r"https?://[^\s<>\"']+|(?:bit\.ly|heylink\.me|s\.id|lynk\.id|forms\.gle)/[^\s<>\"']+", re.IGNORECASE)
INSTAGRAM_SHORTCODE_RE = re.compile(r"/(?:p|reel)/([^/?#]+)/?")
WHITESPACE_RE = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Keyword sets
# ---------------------------------------------------------------------------
REGISTRATION_KEYWORDS = {"daftar", "pendaftaran", "register", "registrasi", "registration", "apply", "submission", "submit"}
NON_REGISTRATION_KEYWORDS = {"guidebook", "booklet", "juknis", "contact", "kontak", "whatsapp", "wa.me", "cp", "narahubung", "email", "instagram", "tiktok", "youtube"}
MAHASISWA_KEYWORDS = {"mahasiswa", "mahasiswi", "universitas", "kampus", "s1", "d3", "d4", "umum", "undergraduate", "diploma", "student", "university"}
BLOCKED_SOCIAL_HOSTS = {"instagram.com", "facebook.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com", "wa.me", "api.whatsapp.com"}
FORM_HOSTS = {"forms.gle", "docs.google.com", "bit.ly", "s.id", "tinyurl.com", "lynk.id"}
DEDUP_STOPWORDS = {"the", "of", "and", "in", "on", "at", "to", "a", "an", "di", "ke", "se", "dan", "atau", "untuk", "dengan", "dalam", "dari", "oleh", "yang", "adalah", "ini", "itu"}
SOURCE_PRIORITY = {"infolomba.id": 0, "silomba.id": 1}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def make_id(title: str, source: str) -> str:
    normalized = f"{source}_{title}".lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:12]

def is_mahasiswa(text: str) -> bool:
    lower = (text or "").lower()
    return any(kw in lower for kw in MAHASISWA_KEYWORDS)

def normalize_space(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text or "").strip()

def clean_url(url: str, base_url: str = "") -> str:
    if not url:
        return ""
    url = url.strip().strip(".,;:!?\"')]}")
    
    if not url.startswith("http"):
        if url.startswith("//"):
            url = "https:" + url
        elif base_url and url.startswith("/"):
            url = urljoin(base_url, url)
        else:
            url = "https://" + url
            
    return url if url.startswith(("http://", "https://")) else ""

def strip_emoji_and_symbols(text: str) -> str:
    return "".join(
        " " if (unicodedata.category(ch).startswith("S") and ch not in {"&", "+", "#"}) else ch
        for ch in (text or "")
    )

def clean_title(text: str) -> str:
    text = strip_emoji_and_symbols(text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r'[@#*_>|\"\'\\]+', " ", text)
    text = re.sub(r"\b(?:caption|repost|info lomba|infolomba)\b", " ", text, flags=re.I)
    return normalize_space(text)[:140].strip(" -:|") or "Tanpa Judul"

def safe_json_loads(text: str) -> list:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\[.*\]", text or "", re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
        return []

def is_low_value_url(url: str) -> bool:
    return urlparse(url).netloc.lower() in BLOCKED_SOCIAL_HOSTS

def compress_text_for_llm(text: str) -> str:
    """Membuang hashtag dan kata sosmed untuk menghemat token secara drastis"""
    text = re.sub(r'#\w+', '', text)
    noise_patterns = r'(?i)(silomba hanya media|jangan lupa follow|tag teman|klik link di bio|share postingan ini|bantu sebar|info lebih lanjut hubungi).*'
    text = re.sub(noise_patterns, '', text)
    text = strip_emoji_and_symbols(text)
    return normalize_space(text)[:800] # Maksimal 800 karakter inti

# ---------------------------------------------------------------------------
# HTML / soup helpers
# ---------------------------------------------------------------------------
def anchor_rows(soup: BeautifulSoup, base_url: str = "") -> list[dict]:
    return [
        {"url": href, "label": normalize_space(a.get_text(" "))}
        for a in soup.find_all("a", href=True)
        if (href := clean_url(a["href"], base_url))
    ]

def best_poster_from_soup(soup: BeautifulSoup, base_url: str = "") -> str:
    for tag, attr in [("meta", "og:image"), ("meta", "twitter:image")]:
        node = soup.find(tag, attrs={"property": attr} if "og:" in attr else {"name": attr})
        if node and (url := clean_url(node.get("content", ""), base_url)):
            return url
    for img in soup.find_all("img"):
        src = clean_url(
            img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "", base_url
        )
        if src and not any(skip in src.lower() for skip in ("logo", "avatar", "profile")):
            return src
    return ""

# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------
def extract_urls_from_text(text: str) -> list[str]:
    seen, result = set(), []
    for m in URL_RE.finditer(text or ""):
        url = clean_url(m.group(0))
        if url and url not in seen and not is_low_value_url(url):
            seen.add(url)
            result.append(url)
    return result

def extract_registration_links(text: str = "", anchors: list[dict] | None = None) -> list[str]:
    anchors = anchors or []
    found: list[str] = []
    seen = set()
    
    # Kumpulkan semua URL
    all_urls = [row["url"] for row in anchors if row.get("url")] + extract_urls_from_text(text)
    
    for raw_url in all_urls:
        url = clean_url(raw_url)
        if not url or url in seen or is_low_value_url(url):
            continue
        # Abaikan URL yang jelas merupakan juknis/panduan
        if any(kw in url.lower() for kw in {"panduan", "juknis", "guidebook"}):
            continue
            
        found.append(url)
        seen.add(url)
        
    return list(dict.fromkeys(found))

# ---------------------------------------------------------------------------
# LLM (Gemini) helpers
# ---------------------------------------------------------------------------
def _create_gemini_client():
    if not GEMINI_API_KEY or not genai:
        return None
    try:
        return genai.Client(api_key=GEMINI_API_KEY)
    except Exception as exc:
        print(f"[LLM] Gemini inactive: {exc}")
        return None

GEMINI_CLIENT = _create_gemini_client()

# Prompt sangat padat dan jelas untuk hemat token
_LLM_PROMPT_PREFIX = (
    "Ekstrak data lomba ke JSON array: "
    '[{"i":"id","j":"judul_lomba","d":"deadline","o":"penyelenggara","l":["link_daftar"]}]'
    "\nAturan:"
    "\n- j: Nama lomba/event bersih tanpa emoji/kata promosi."
    "\n- d: Tgl penutupan pendaftaran (cth: 20 Mei 2026) atau '-'."
    "\n- o: Nama instansi/komunitas penyelenggara atau '-'."
    "\n- l: HANYA link form pendaftaran (bit.ly, gform) dari teks. Abaikan link WA/Juknis."
    "\nInput:\n"
)

def _llm_call(prompt: str) -> list:
    if not GEMINI_CLIENT or not genai_types:
        return []
    for attempt in range(3):
        try:
            response = GEMINI_CLIENT.models.generate_content(
                model="gemini-2.0-flash-lite",
                contents=prompt,
                config=genai_types.GenerateContentConfig(response_mime_type="application/json"),
            )
            data = safe_json_loads(response.text or "")
            return data if isinstance(data, list) else []
        except Exception as exc:
            print(f"[LLM] Error attempt {attempt + 1}: {exc}")
            time.sleep(5 * (attempt + 1))
    return []

def process_batch_with_gemini(batch: list) -> list:
    if not GEMINI_CLIENT or not batch:
        return batch

    # Payload super hemat: Kirim ID dan Caption yang sudah dikompresi saja
    payload = [{"i": item["id"], "c": compress_text_for_llm(item.get("caption", ""))} for item in batch]
    
    # Penggunaan separators=(',', ':') menghapus spasi putih pada JSON untuk hemat token
    raw_llm_data = _llm_call(_LLM_PROMPT_PREFIX + json.dumps(payload, separators=(',', ':')))
    llm_map = {str(row.get("i")): row for row in raw_llm_data if isinstance(row, dict) and row.get("i")}

    for item in batch:
        row = llm_map.get(item["id"], {})
        
        if row.get("j"): item["judul"] = clean_title(row["j"])
        item["deadline"] = row.get("d", "-")
        item["penyelenggara"] = row.get("o", "-")

        # Logika Link Pendaftaran
        if item["sumber"] == "silomba.id":
            if item.get("official_url"):
                # Prioritas 1: official_url (Website Resmi)
                item["link_pendaftaran"] = [item["official_url"]]
            elif row.get("l"):
                # Prioritas 2: Link dari hasil ekstraksi LLM
                item["link_pendaftaran"] = [u for u in row["l"] if str(u).startswith("http")]
        # PENTING: Jika bukan silomba.id (IG/Infolomba), link_pendaftaran dibiarkan 
        # utuh sesuai hasil ekstraksi regex Python bawaan (tidak ditimpa LLM).
        
        item.pop("official_url", None) # Hapus key temporary
        
    time.sleep(2)
    return batch

# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------
def _build_item(uid, source, title, poster, caption, links, direct_url, official_url="") -> dict:
    return {
        "id": uid,
        "sumber": source,
        "judul": title,
        "poster": poster,
        "caption": caption,
        "link_pendaftaran": links,
        "link_direct": direct_url,
        "official_url": official_url, # Key tambahan untuk LLM logic
        "deadline": "",
        "penyelenggara": ""
    }

def scrape_infolomba(seen_ids: set) -> list:
    print("[infolomba] Starting...")
    base_url = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()
    results = []
    try:
        resp = scraper.get(base_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        unique_links = {
            urljoin(base_url, a["href"]): a
            for a in soup.find_all("a", href=lambda h: h and "info-" in h)
            if urljoin(base_url, a.get("href", "")).startswith(base_url + "/")
        }
        for link, anchor in list(unique_links.items())[:MAX_WEB_ITEMS]:
            try:
                res = scraper.get(link, headers=HEADERS, timeout=30)
                if res.status_code != 200:
                    continue
                dsoup = BeautifulSoup(res.text, "html.parser")
                full_text = dsoup.get_text("\n")
                if not is_mahasiswa(full_text):
                    continue
                
                title_tag = dsoup.find(["h1", "h2"])
                title = clean_title(title_tag.get_text(" ") if title_tag else link)
                uid = make_id(title, "infolomba.id")
                if uid in seen_ids:
                    continue
                
                if "Daftar Sekarang" in full_text and "Laporkan Lomba" in full_text:
                    body = full_text.split("Daftar Sekarang")[-1].split("Laporkan Lomba")[0]
                    caption = "\n".join(l.strip() for l in body.splitlines() if l.strip())
                else:
                    caption = "\n".join(l.strip() for l in full_text.splitlines() if l.strip())[:2500]
                
                poster = best_poster_from_soup(dsoup, base_url)
                if not poster:
                    img = anchor.find("img") or {}
                    poster = clean_url(img.get("src") or img.get("data-src") or "", base_url)
                
                results.append(_build_item(
                    uid, "infolomba.id", title, poster, caption,
                    extract_registration_links(full_text, anchor_rows(dsoup, base_url)), link
                ))
                seen_ids.add(uid)
            except Exception as exc:
                print(f"[infolomba] Skip {link}: {exc}")
    except Exception as exc:
        print(f"[infolomba] Error: {exc}")
    print(f"[infolomba] Done: {len(results)} items")
    return results

async def scrape_silomba(seen_ids: set) -> list:
    print("[silomba] Starting...")
    base_url = "https://silomba.id"
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=HEADERS["User-Agent"])
            await page.goto(base_url, wait_until="networkidle", timeout=45000)
            await page.wait_for_selector("#competition-section", timeout=15000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            soup = BeautifulSoup(await page.content(), "html.parser")
            await page.close()
            
            section = soup.find(id="competition-section")
            if not section:
                return results
                
            for card in section.find_all("a", href=lambda h: h and h.startswith("/lomba/"))[:MAX_WEB_ITEMS]:
                raw_title = card.get("aria-label", "").replace("Lihat detail kompetisi ", "").strip()
                title = clean_title(raw_title or "Lomba Silomba")
                uid = make_id(title, "silomba.id")
                if uid in seen_ids:
                    continue
                
                link_detail = urljoin(base_url, card["href"])
                try:
                    dp = await browser.new_page(user_agent=HEADERS["User-Agent"])
                    await dp.goto(link_detail, wait_until="networkidle", timeout=45000)
                    dsoup = BeautifulSoup(await dp.content(), "html.parser")
                    await dp.close()
                    
                    full_text = dsoup.get_text("\n")
                    if not is_mahasiswa(full_text):
                        continue
                        
                    poster = best_poster_from_soup(dsoup, base_url)
                    caption = full_text.split("Deskripsi Lomba")[-1].strip() if "Deskripsi Lomba" in full_text else full_text[:2500]
                    links = extract_registration_links(full_text, anchor_rows(dsoup, base_url))
                    
                    # Deteksi tombol "Website Resmi" untuk field official_url
                    official_url = ""
                    for a_tag in dsoup.find_all("a", href=True):
                        if "Website Resmi" in a_tag.get_text() or "Daftar Sekarang" in a_tag.get_text():
                            href = clean_url(a_tag["href"])
                            if href and not href.startswith(base_url):
                                official_url = href
                                break
                    
                    results.append(_build_item(
                        uid, "silomba.id", title, poster, caption, links, link_detail, official_url
                    ))
                    seen_ids.add(uid)
                except Exception as exc:
                    print(f"[silomba] Detail failed {link_detail}: {exc}")
        except Exception as exc:
            print(f"[silomba] Error: {exc}")
        finally:
            await browser.close()
    print(f"[silomba] Done: {len(results)} items")
    return results

def _normalize_ig_caption(raw: str) -> str:
    caption = re.sub(r"^\s*[^:\n]{1,80}\s+on Instagram:\s*", "", (raw or "").strip(), flags=re.I)
    return re.sub(r'^\s*"|"\s*$', "", caption).strip()

def _ig_shortcode(url: str) -> str:
    match = INSTAGRAM_SHORTCODE_RE.search(url or "")
    return match.group(1) if match else url

def _build_chrome_driver() -> webdriver.Chrome:
    opts = Options()
    for arg in ("--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", f"user-agent={HEADERS['User-Agent']}"):
        opts.add_argument(arg)
    if os.path.exists("/opt/chrome/chrome"):
        opts.binary_location = "/opt/chrome/chrome"
    service = Service("/usr/bin/chromedriver") if os.path.exists("/usr/bin/chromedriver") else Service()
    return webdriver.Chrome(service=service, options=opts)

def scrape_instagram(seen_ids: set) -> list:
    if not IG_SESSION_ID:
        print("[IG] IG_SESSION_ID not set, skipping.")
        return []
    print("[IG] Starting...")
    results = []
    driver = _build_chrome_driver()
    try:
        driver.get("https://www.instagram.com/")
        time.sleep(3)
        driver.add_cookie({"name": "sessionid", "value": IG_SESSION_ID, "domain": ".instagram.com"})
        driver.refresh()
        time.sleep(5)
        
        for account in IG_ACCOUNTS:
            driver.get(f"https://www.instagram.com/{account}/")
            time.sleep(random.randint(4, 6))
            
            post_urls = []
            for el in driver.find_elements(By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]'):
                if el.get_attribute("href") not in post_urls:
                    post_urls.append(el.get_attribute("href"))
                    
            for url in post_urls[:MAX_IG_POSTS_PER_ACCOUNT]:
                try:
                    driver.get(url)
                    time.sleep(random.randint(3, 5))
                    
                    caption = ""
                    if h1s := driver.find_elements(By.XPATH, "//article//h1"):
                        caption = h1s[0].text
                        
                    if not caption or not is_mahasiswa(caption):
                        continue
                        
                    uid = make_id(_ig_shortcode(url), f"IG @{account}")
                    if uid in seen_ids: continue
                    
                    poster = ""
                    if imgs := driver.find_elements(By.CSS_SELECTOR, "article img"):
                        poster = imgs[0].get_attribute("src")
                        
                    results.append(_build_item(
                        uid, f"IG @{account}", "Instagram Post", poster, caption,
                        extract_registration_links(caption), url
                    ))
                    seen_ids.add(uid)
                except Exception as exc:
                    print(f"[IG] Skip post {url}: {exc}")
    except Exception as exc:
        print(f"[IG] Error: {exc}")
    finally:
        driver.quit()
    print(f"[IG] Done: {len(results)} items")
    return results

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def _tokenize(title: str) -> set:
    return {
        t for t in re.sub(r"[^\w\s]", " ", (title or "").lower()).split()
        if t not in DEDUP_STOPWORDS and len(t) > 1
    }

def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0

def dedup_results(new_items: list, db_data: list, threshold: float = 0.6) -> list:
    db_direct_urls = {d["link_direct"] for d in db_data if d.get("link_direct")}
    db_tokens = [_tokenize(d["judul"]) for d in db_data if d.get("judul")]
    unique: list[dict] = []
    
    for item in new_items:
        token = _tokenize(item.get("judul", ""))
        link = item.get("link_direct", "")
        
        if link and link in db_direct_urls:
            continue
        if any(_jaccard(token, db_tok) >= threshold for db_tok in db_tokens):
            continue
            
        dup_idx = next((i for i, ex in enumerate(unique) if _jaccard(token, _tokenize(ex.get("judul", ""))) >= threshold), None)
        
        if dup_idx is not None:
            existing = unique[dup_idx]
            merged = list(dict.fromkeys(existing.get("link_pendaftaran", []) + item.get("link_pendaftaran", [])))
            if SOURCE_PRIORITY.get(item["sumber"], 2) < SOURCE_PRIORITY.get(existing["sumber"], 2):
                item["link_pendaftaran"] = merged
                unique[dup_idx] = item
            else:
                unique[dup_idx]["link_pendaftaran"] = merged
        else:
            unique.append(item)
            
    return unique

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not set.")
    print("[INFO] Connecting to MongoDB...")
    client = pymongo.MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION]
    
    db_data = list(collection.find({}, {"id": 1, "link_direct": 1, "judul": 1, "_id": 0}))
    seen_ids = {d["id"] for d in db_data if "id" in d}
    print(f"[INFO] {len(seen_ids)} existing records in DB.")
    
    batches = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, seen_ids),
        scrape_silomba(seen_ids),
        asyncio.to_thread(scrape_instagram, seen_ids),
    )
    
    raw = [item for batch in batches if isinstance(batch, list) for item in batch]
    print(f"[INFO] {len(raw)} new raw items found.")
    
    if not raw:
        client.close()
        return
        
    processed = []
    for i in range(0, len(raw), 15):
        batch = raw[i: i + 15]
        print(f"[LLM] Batch {i // 15 + 1} ({len(batch)} items)...")
        processed.extend(process_batch_with_gemini(batch))
        
    final = dedup_results(processed, db_data)
    if not final:
        print("[INFO] All items are duplicates.")
        client.close()
        return
        
    result = collection.bulk_write(
        [UpdateOne({"id": item["id"]}, {"$set": item}, upsert=True) for item in final]
    )
    print(f"[INFO] Saved: {result.upserted_count} new, {result.modified_count} updated.")
    client.close()

if __name__ == "__main__":
    asyncio.run(main())
