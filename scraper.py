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

URL_RE = re.compile(r"https?://[^\s<>'\"`)\]}]+", re.IGNORECASE)
INSTAGRAM_SHORTCODE_RE = re.compile(r"/(?:p|reel)/([^/?#]+)/?")
TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:judul|title|nama\s+lomba|competition|event)\s*[:\-]\s*", re.IGNORECASE
)
OPEN_REGISTRATION_RE = re.compile(
    r"open\s+registration\s*[:\-]\s*([^\]\n]+)", re.IGNORECASE
)
WHITESPACE_RE = re.compile(r"\s+")
# Timeline pattern: matches date ranges like "23-24 Januari" or "23-28 Jan"
TIMELINE_PATTERN = re.compile(
    r"(?:pendaftaran|registration|open|close|pengumpulan|submission|deadline|tutup)\s*[:\-]?\s*"
    r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([a-zA-Z]+|[0-9]{1,2})",
    re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Keyword sets
# ---------------------------------------------------------------------------

REGISTRATION_KEYWORDS = {"daftar", "pendaftaran", "register", "registrasi", "registration", "apply", "submission", "submit"}
NON_REGISTRATION_KEYWORDS = {"guidebook", "booklet", "juknis", "contact", "kontak", "whatsapp", "wa.me", "cp", "narahubung", "email", "instagram", "tiktok", "youtube"}
TITLE_NOISE_KEYWORDS = {"link pendaftaran", "pendaftaran", "register", "registration", "apply now", "guidebook", "contact us", "whatsapp", "deadline", "benefit", "prize", "hadiah", "timeline", "save the date", "open registration", "closed registration", "terbuka untuk", "untuk mahasiswa"}
MAHASISWA_KEYWORDS = {"mahasiswa", "mahasiswi", "universitas", "kampus", "s1", "d3", "d4", "umum", "undergraduate", "diploma", "student", "university"}
BLOCKED_SOCIAL_HOSTS = {"instagram.com", "facebook.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com", "wa.me", "api.whatsapp.com"}
FORM_HOSTS = {"forms.gle", "docs.google.com", "bit.ly", "s.id", "tinyurl.com", "lynk.id"}
DEDUP_STOPWORDS = {"the", "of", "and", "in", "on", "at", "to", "a", "an", "di", "ke", "se", "dan", "atau", "untuk", "dengan", "dalam", "dari", "oleh", "yang", "adalah", "ini", "itu"}
SOURCE_PRIORITY = {"infolomba.id": 0, "silomba.id": 1}

TITLE_COMPETITION_WORDS = {"lomba", "competition", "olimpiade", "challenge", "contest"}
TITLE_EVENT_WORDS = {"conference", "summit", "bootcamp", "program", "award"}

# Month mapping for normalization
MONTH_MAP = {
    "januari": "Januari", "january": "Januari", "jan": "Januari",
    "februari": "Februari", "february": "Februari", "feb": "Februari",
    "maret": "Maret", "march": "Maret", "mar": "Maret",
    "april": "April", "apr": "April",
    "mei": "Mei", "may": "Mei",
    "juni": "Juni", "june": "Juni", "jun": "Juni",
    "juli": "Juli", "july": "Juli", "jul": "Juli",
    "agustus": "Agustus", "august": "Agustus", "aug": "Agustus",
    "september": "September", "sept": "September", "sep": "September",
    "oktober": "Oktober", "october": "Oktober", "oct": "Oktober",
    "november": "November", "nov": "November",
    "desember": "Desember", "december": "Desember", "dec": "Desember",
}


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
    url = url.strip().strip(".,;:!?\"'`)]}")
    if url.startswith("//"):
        url = "https:" + url
    elif base_url and url.startswith("/"):
        url = urljoin(base_url, url)
    return url if url.startswith(("http://", "https://")) else ""


def strip_emoji_and_symbols(text: str) -> str:
    return "".join(
        " " if (unicodedata.category(ch).startswith("S") and ch not in {"&", "+", "#"}) else ch
        for ch in (text or "")
    )


def clean_title(text: str) -> str:
    text = strip_emoji_and_symbols(text)
    text = TITLE_PREFIX_RE.sub("", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r'[@#*_`>|"""\'\']+', " ", text)
    text = re.sub(r"\b(?:caption|repost|info lomba|infolomba)\b", " ", text, flags=re.I)
    text = re.sub(r"\s*[-–—|:]\s*(?:open registration|registration|pendaftaran).*$", "", text, flags=re.I)
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


def _keywords_in(text: str, keywords: set) -> bool:
    lower = (text or "").lower()
    return any(kw in lower for kw in keywords)


def is_registration_context(text: str) -> bool:
    return _keywords_in(text, REGISTRATION_KEYWORDS)


def is_non_registration_context(text: str) -> bool:
    return _keywords_in(text, NON_REGISTRATION_KEYWORDS)


# ---------------------------------------------------------------------------
# Timeline & Kategori extraction
# ---------------------------------------------------------------------------

def _normalize_month(month_str: str) -> str:
    """Normalize bulan ke format Bulan penuh"""
    return MONTH_MAP.get(month_str.lower().strip(), month_str)


def extract_timeline(text: str) -> str:
    """
    Extract timeline dari teks dalam format 'dd-dd BulanNama'.
    Cth: '23-28 Januari' dari 'Pendaftaran: 23-24 Januari, Pengumpulan: 27-28 Januari'
    """
    if not text:
        return ""
    
    matches = []
    for match in TIMELINE_PATTERN.finditer(text):
        day_start, day_end, month = match.groups()
        month_norm = _normalize_month(month)
        matches.append((int(day_start), int(day_end), month_norm))
    
    if not matches:
        return ""
    
    if len(matches) == 1:
        start_day, end_day, month = matches[0]
        return f"{start_day}-{end_day} {month}"
    
    # Jika ada 2+ matches (pendaftaran + pengumpulan), ambil range terluas
    all_days = [day for d1, d2, _ in matches for day in [d1, d2]]
    months = [month for _, _, month in matches]
    
    if len(set(months)) == 1:
        # Sama bulan, ambil min-max hari
        return f"{min(all_days)}-{max(all_days)} {months[0]}"
    else:
        # Beda bulan, ambil dari tanggal pertama sampai terakhir dengan bulan
        first_day, _, first_month = matches[0]
        _, last_day, last_month = matches[-1]
        return f"{first_day} {first_month} - {last_day} {last_month}"


def extract_kategori(text: str, title: str = "") -> str:
    """
    Extract kategori lomba dari teks dan judul.
    Return salah satu: Kompetisi, Konferensi, Bootcamp, Program, Award, Lainnya
    """
    combined = f"{title} {text}".lower()
    
    if any(w in combined for w in ["kompetisi", "lomba", "competition", "contest", "challenge", "olimpiade"]):
        return "Kompetisi"
    elif any(w in combined for w in ["konferensi", "conference", "seminar", "workshop"]):
        return "Konferensi"
    elif any(w in combined for w in ["bootcamp", "training", "pelatihan", "kursus"]):
        return "Bootcamp"
    elif any(w in combined for w in ["program", "beasiswa", "scholarship", "magang"]):
        return "Program"
    elif any(w in combined for w in ["award", "penghargaan", "apresiasi"]):
        return "Award"
    else:
        return "Lainnya"


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
# Title extraction
# ---------------------------------------------------------------------------

def _line_has_url(line: str) -> bool:
    return bool(URL_RE.search(line))


def _is_noise_title(line: str) -> bool:
    if _line_has_url(line) or len(normalize_space(line)) < 6:
        return True
    return _keywords_in(line, TITLE_NOISE_KEYWORDS)


def _score_title(line: str, position: int) -> int:
    lower = line.lower()
    score = 100 - position
    score += 25 * any(w in lower for w in TITLE_COMPETITION_WORDS)
    score += 10 * any(w in lower for w in TITLE_EVENT_WORDS)
    score += 8 * (line.isupper() and len(line) > 8)
    score += 5 * bool(re.search(r"\b20\d{2}\b", line))
    return score


def extract_title_from_caption(caption: str) -> str:
    lines = [normalize_space(l) for l in (caption or "").splitlines() if normalize_space(l)]
    candidates: list[tuple[int, str]] = []

    for idx, line in enumerate(lines[:25]):
        match = OPEN_REGISTRATION_RE.search(strip_emoji_and_symbols(line))
        if match:
            title = clean_title(match.group(1))
            if title != "Tanpa Judul":
                candidates.append((_score_title(title, idx) + 35, title))
                continue

        if _is_noise_title(line):
            continue
        title = clean_title(line)
        if title != "Tanpa Judul":
            candidates.append((_score_title(title, idx), title))

    if candidates:
        return max(candidates)[1]

    for line in lines:
        if not _is_noise_title(line):
            title = clean_title(line)
            if title != "Tanpa Judul":
                return title
    return "Tanpa Judul"


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------

def extract_urls_from_text(text: str) -> list[str]:
    seen, result = set(), []
    for m in URL_RE.finditer(text or ""):
        url = clean_url(m.group(0))
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def extract_registration_links(text: str = "", anchors: list[dict] | None = None) -> list[str]:
    anchors = anchors or []
    found: list[str] = []

    # Anchor-based detection
    for row in anchors:
        url = clean_url(row.get("url", ""))
        if url and not is_low_value_url(url):
            label = row.get("label", "")
            if is_registration_context(label) and not is_non_registration_context(label):
                found.append(url)

    # Text-based detection (context window around each URL line)
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for idx, line in enumerate(lines):
        urls = extract_urls_from_text(line)
        if not urls:
            continue
        context = " ".join(lines[max(0, idx - 1): idx + 2])
        if is_registration_context(context) and not is_non_registration_context(line):
            found.extend(u for u in urls if not is_low_value_url(u))

    if found:
        return list(dict.fromkeys(found))

    # Fallback: well-known form/shortener hosts only
    all_urls = [row["url"] for row in anchors if row.get("url")] + extract_urls_from_text(text)
    return list(dict.fromkeys(
        u for raw in all_urls
        if (u := clean_url(raw)) and not is_low_value_url(u)
        and any(h in urlparse(u).netloc.lower() for h in FORM_HOSTS)
    ))


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

_LLM_PROMPT_PREFIX = (
    "Rapikan data lomba untuk mahasiswa. Untuk setiap item, kembalikan JSON array "
    'dengan format {"i":"id","j":"judul resmi","l":["url pendaftaran"],"t":"timeline","k":"kategori"}. '
    "Judul harus berupa nama lomba/program/event saja, tanpa emoji, sapaan, label "
    "pendaftaran, tanggal, atau URL. Field l hanya berisi URL pendaftaran/register/apply, "
    "bukan guidebook, kontak, sosial media, atau WhatsApp. Field t adalah timeline gabungan "
    "(cth: '23-28 Januari') dari tanggal awal sampai akhir, kosongkan jika tidak ada. "
    "Field k adalah kategori lomba (Kompetisi/Konferensi/Bootcamp/Program/Award/Lainnya). "
    "Jika tidak yakin, pertahankan data yang sudah ada atau isi dengan string kosong.\n\nData: "
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
            time.sleep(10 * (attempt + 1))
    return []


def process_batch_with_gemini(batch: list) -> list:
    if not GEMINI_CLIENT or not batch:
        return batch

    # Pre-extract timeline dan kategori dari text untuk di-pass ke LLM
    for item in batch:
        caption = item.get("caption", "")
        if not item.get("timeline"):
            item["timeline"] = extract_timeline(caption)
        if not item.get("kategori"):
            item["kategori"] = extract_kategori(caption, item.get("judul", ""))

    payload = [
        {
            "i": item["id"],
            "sumber": item["sumber"],
            "judul": item.get("judul", ""),
            "caption": item.get("caption", "")[:1200],
            "link_pendaftaran": item.get("link_pendaftaran", []),
            "timeline": item.get("timeline", ""),
            "kategori": item.get("kategori", ""),
        }
        for item in batch
    ]

    llm_map = {
        row["i"]: row
        for row in _llm_call(_LLM_PROMPT_PREFIX + json.dumps(payload, ensure_ascii=False))
        if isinstance(row, dict) and row.get("i")
    }

    for item in batch:
        row = llm_map.get(item["id"], {})
        # Update judul jika LLM return yang lebih baik
        if row.get("j"):
            title = clean_title(row["j"])
            if title != "Tanpa Judul":
                item["judul"] = title
        # Update link jika LLM return yang lebih baik
        if row.get("l"):
            links = [u for raw in row["l"] if (u := clean_url(raw))]
            if links:
                item["link_pendaftaran"] = list(dict.fromkeys(links))
        # Update timeline dari LLM jika kosong
        if row.get("t") and not item.get("timeline"):
            item["timeline"] = row["t"]
        # Update kategori dari LLM jika kosong
        if row.get("k") and not item.get("kategori"):
            item["kategori"] = row["k"]

    time.sleep(2)
    return batch


# ---------------------------------------------------------------------------
# Scraper: infolomba.id
# ---------------------------------------------------------------------------

def _build_item(uid, source, title, poster, caption, links, direct_url) -> dict:
    return {
        "id": uid,
        "sumber": source,
        "judul": title,
        "poster": poster,
        "caption": caption,
        "link_pendaftaran": links,
        "link_direct": direct_url,
        "timeline": extract_timeline(caption),
        "kategori": extract_kategori(caption, title),
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
                slug_title = (
                    "-".join(link.rstrip("/").split("/")[-1].replace("info-", "", 1).split("-")[:-1])
                    .replace("-", " ").title()
                )
                title = clean_title(title_tag.get_text(" ") if title_tag else slug_title)
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
                    extract_registration_links(full_text, anchor_rows(dsoup, base_url)),
                    link,
                ))
                seen_ids.add(uid)

            except Exception as exc:
                print(f"[infolomba] Skip {link}: {exc}")

    except Exception as exc:
        print(f"[infolomba] Error: {exc}")

    print(f"[infolomba] Done: {len(results)} items")
    return results


# ---------------------------------------------------------------------------
# Scraper: silomba.id
# ---------------------------------------------------------------------------

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
                raw_title = (
                    card.get("aria-label", "").replace("Lihat detail kompetisi ", "").strip()
                    or (
                        (h := card.find(["h1", "h2", "h3", "h4"])) and h.get_text(" ").strip()
                    )
                    or "Tanpa Judul"
                )
                title = clean_title(raw_title)
                uid = make_id(title, "silomba.id")
                if uid in seen_ids:
                    continue

                link_detail = urljoin(base_url, card["href"])
                poster = caption = ""
                links = []

                try:
                    dp = await browser.new_page(user_agent=HEADERS["User-Agent"])
                    await dp.goto(link_detail, wait_until="networkidle", timeout=45000)
                    dsoup = BeautifulSoup(await dp.content(), "html.parser")
                    await dp.close()

                    full_text = dsoup.get_text("\n")
                    if not is_mahasiswa(full_text):
                        continue

                    poster = best_poster_from_soup(dsoup, base_url)
                    caption = (
                        full_text.split("Deskripsi Lomba")[-1].strip()
                        if "Deskripsi Lomba" in full_text
                        else "\n".join(l.strip() for l in full_text.splitlines() if l.strip())[:2500]
                    )
                    links = extract_registration_links(full_text, anchor_rows(dsoup, base_url))

                except Exception as exc:
                    print(f"[silomba] Detail failed {link_detail}: {exc}")

                results.append(_build_item(uid, "silomba.id", title, poster, caption, links, link_detail))
                seen_ids.add(uid)

        except Exception as exc:
            print(f"[silomba] Error: {exc}")
        finally:
            await browser.close()

    print(f"[silomba] Done: {len(results)} items")
    return results


# ---------------------------------------------------------------------------
# Scraper: Instagram
# ---------------------------------------------------------------------------

def _normalize_ig_caption(raw: str) -> str:
    caption = re.sub(r"^\s*[^:\n]{1,80}\s+on Instagram:\s*", "", (raw or "").strip(), flags=re.I)
    return re.sub(r'^\s*"|\"\s*$', "", caption).strip()


def _ig_shortcode(url: str) -> str:
    match = INSTAGRAM_SHORTCODE_RE.search(url or "")
    return match.group(1) if match else url


def _build_chrome_driver() -> webdriver.Chrome:
    opts = Options()
    for arg in ("--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
                 f"user-agent={HEADERS['User-Agent']}"):
        opts.add_argument(arg)
    if os.path.exists("/opt/chrome/chrome"):
        opts.binary_location = "/opt/chrome/chrome"
    service = Service("/usr/bin/chromedriver") if os.path.exists("/usr/bin/chromedriver") else Service()
    return webdriver.Chrome(service=service, options=opts)


_IG_POSTER_JS = """
const imgs = Array.from(document.querySelectorAll('article img'));
for (const img of imgs) {
  const src = img.currentSrc || img.src || '';
  const alt = (img.alt || '').toLowerCase();
  if (!src || alt.includes('profile') || src.includes('150x150')) continue;
  if (src.includes('scontent') || src.includes('cdninstagram')) return src;
}
return '';
"""


def _collect_post_urls(driver, account: str) -> list[str]:
    driver.get(f"https://www.instagram.com/{account}/")
    time.sleep(random.randint(4, 6))
    if "page not found" in driver.title.lower():
        return []

    seen, urls = set(), []
    last_height = driver.execute_script("return document.body.scrollHeight")
    for _ in range(3):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.randint(2, 4))
        for el in driver.find_elements(By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]'):
            href = el.get_attribute("href")
            if href and href not in seen:
                seen.add(href)
                urls.append(href)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
    return urls


def _scrape_ig_post(driver, url: str, account: str, seen_ids: set) -> dict | None:
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "article")))
    except Exception:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "img")))
    time.sleep(random.randint(3, 5))

    caption = ""
    if h1s := driver.find_elements(By.XPATH, "//article//h1"):
        caption = h1s[0].text
    if not caption:
        if metas := driver.find_elements(By.XPATH, '//meta[@property="og:description"]'):
            raw = metas[0].get_attribute("content") or ""
            caption = raw.split(": ", 1)[1] if ": " in raw else raw

    caption = _normalize_ig_caption(caption or driver.title)
    if not caption or not is_mahasiswa(caption):
        return None

    uid = make_id(_ig_shortcode(url), f"IG @{account}")
    if uid in seen_ids:
        return None

    poster = driver.execute_script(_IG_POSTER_JS)
    if not poster:
        if og := driver.find_elements(By.XPATH, '//meta[@property="og:image"]'):
            poster = og[0].get_attribute("content") or ""

    title = extract_title_from_caption(caption)
    return _build_item(
        uid, f"IG @{account}",
        title,
        poster, caption,
        extract_registration_links(caption),
        url,
    )


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
        if "login" in driver.current_url.lower():
            print("[IG] Invalid session.")
            return []

        for account in IG_ACCOUNTS:
            post_urls = _collect_post_urls(driver, account)
            for url in post_urls[:MAX_IG_POSTS_PER_ACCOUNT]:
                try:
                    item = _scrape_ig_post(driver, url, account, seen_ids)
                    if item:
                        results.append(item)
                        seen_ids.add(item["id"])
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
        item["judul"] = clean_title(item.get("judul", ""))
        item["link_pendaftaran"] = list(dict.fromkeys(
            u for raw in item.get("link_pendaftaran", []) if (u := clean_url(raw))
        ))

        token = _tokenize(item.get("judul", ""))
        link = item.get("link_direct", "")

        if link and link in db_direct_urls:
            print(f"[DEDUP] Skip duplicate URL: {link}")
            continue
        if any(_jaccard(token, db_tok) >= threshold for db_tok in db_tokens):
            print(f"[DEDUP] Skip title similar to DB: {item['judul']!r}")
            continue

        dup_idx = next(
            (i for i, ex in enumerate(unique) if _jaccard(token, _tokenize(ex.get("judul", ""))) >= threshold),
            None,
        )

        if dup_idx is not None:
            existing = unique[dup_idx]
            merged = list(dict.fromkeys(existing.get("link_pendaftaran", []) + item.get("link_pendaftaran", [])))
            if SOURCE_PRIORITY.get(item["sumber"], 2) < SOURCE_PRIORITY.get(existing["sumber"], 2):
                item["link_pendaftaran"] = merged
                unique[dup_idx] = item
                print(f"[DEDUP] Replace {existing['sumber']} -> {item['sumber']}: {item['judul']!r}")
            else:
                unique[dup_idx]["link_pendaftaran"] = merged
                print(f"[DEDUP] Merge links {item['sumber']} -> {existing['sumber']}: {item['judul']!r}")
        else:
            unique.append(item)

    print(f"[DEDUP] {len(new_items)} -> {len(unique)} unique items.")
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
        print("[INFO] No new data.")
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
