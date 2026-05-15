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


# ============================================================================
# CONFIGURATION
# ============================================================================

IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
MONGO_URI = os.environ.get("MONGO_URI")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DB_NAME = os.environ.get("DB_NAME", "competition_scraper")
COLLECTION = os.environ.get("COLLECTION", "competition")

IG_ACCOUNTS = [
    a.strip()
    for a in os.environ.get(
        "IG_ACCOUNTS",
        "infolomba,infolomba_gratis,infolomba.olimpiade",
    ).split(",")
    if a.strip()
]

MAX_WEB_ITEMS = int(os.environ.get("MAX_WEB_ITEMS", "15"))
MAX_IG_POSTS_PER_ACCOUNT = int(
    os.environ.get("MAX_IG_POSTS_PER_ACCOUNT", "6")
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}


# ============================================================================
# REGEX
# ============================================================================

URL_RE = re.compile(r"https?://[^\s<>'\"`)\]}]+", re.IGNORECASE)
INSTAGRAM_SHORTCODE_RE = re.compile(r"/(?:p|reel)/([^/?#]+)/?")
TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:judul|title|nama\s+lomba|competition|event)\s*[:-]\s*",
    re.IGNORECASE,
)
WHITESPACE_RE = re.compile(r"\s+")

DEADLINE_PATTERN = re.compile(
    r"(?:deadline|tutup|close|akhir|until|hingga|s\.d\.|sd)\s*[:-]?\s*"
    r"(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})?",
    re.IGNORECASE,
)


# ============================================================================
# KEYWORDS
# ============================================================================

REGISTRATION_KEYWORDS = {
    "daftar",
    "pendaftaran",
    "register",
    "registrasi",
    "registration",
    "apply",
    "submission",
}

NON_REGISTRATION_KEYWORDS = {
    "guidebook",
    "booklet",
    "juknis",
    "contact",
    "kontak",
    "whatsapp",
    "cp",
    "email",
    "instagram",
    "tiktok",
    "youtube",
}

TITLE_NOISE_KEYWORDS = {
    "link pendaftaran",
    "pendaftaran",
    "register",
    "registration",
    "deadline",
    "benefit",
    "prize",
    "hadiah",
    "save the date",
    "open registration",
}

MAHASISWA_KEYWORDS = {
    "mahasiswa",
    "mahasiswi",
    "universitas",
    "kampus",
    "s1",
    "d3",
    "d4",
    "student",
    "university",
    "umum",
}

BLOCKED_SOCIAL_HOSTS = {
    "instagram.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "wa.me",
}

FORM_HOSTS = {
    "forms.gle",
    "docs.google.com",
    "bit.ly",
    "s.id",
    "tinyurl.com",
    "lynk.id",
}

DEDUP_STOPWORDS = {
    "the",
    "of",
    "and",
    "in",
    "on",
    "at",
    "to",
    "a",
    "an",
    "di",
    "ke",
    "se",
    "dan",
    "atau",
    "untuk",
    "dengan",
    "dalam",
    "dari",
}

SOURCE_PRIORITY = {
    "infolomba.id": 0,
    "silomba.id": 1,
}

MONTH_MAP = {
    "januari": "Januari",
    "january": "Januari",
    "jan": "Januari",
    "februari": "Februari",
    "february": "Februari",
    "feb": "Februari",
    "maret": "Maret",
    "march": "Maret",
    "mar": "Maret",
    "april": "April",
    "apr": "April",
    "mei": "Mei",
    "may": "Mei",
    "juni": "Juni",
    "june": "Juni",
    "jun": "Juni",
    "juli": "Juli",
    "july": "Juli",
    "jul": "Juli",
    "agustus": "Agustus",
    "august": "Agustus",
    "aug": "Agustus",
    "september": "September",
    "sept": "September",
    "sep": "September",
    "oktober": "Oktober",
    "october": "Oktober",
    "oct": "Oktober",
    "november": "November",
    "nov": "November",
    "desember": "Desember",
    "december": "Desember",
    "dec": "Desember",
}

KATEGORI_KEYWORDS = {
    "IT": {
        "keywords": {
            "it",
            "programming",
            "coding",
            "developer",
            "web",
            "app",
            "software",
            "database",
            "ai",
            "machine learning",
            "data science",
            "backend",
            "frontend",
            "fullstack",
            "python",
            "javascript",
            "java",
            "c++",
            "php",
            "golang",
            "cybersecurity",
            "pemrograman",
            "programmer",
        },
        "priority": 1,
    },
    "Webdev": {
        "keywords": {
            "webdev",
            "web development",
            "frontend",
            "backend",
            "fullstack",
            "react",
            "vue",
            "html",
            "css",
            "javascript",
        },
        "priority": 1,
    },
    "Design": {
        "keywords": {
            "design",
            "graphic design",
            "ui",
            "ux",
            "illustration",
            "figma",
            "photoshop",
        },
        "priority": 2,
    },
}


# ============================================================================
# HELPERS
# ============================================================================


def make_id(title: str, source: str) -> str:
    normalized = f"{source}_{title}".lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()[:12]



def normalize_space(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text or "").strip()



def is_mahasiswa(text: str) -> bool:
    lower = (text or "").lower()
    return any(kw in lower for kw in MAHASISWA_KEYWORDS)



def clean_url(url: str, base_url: str = "") -> str:
    if not url:
        return ""

    url = url.strip().strip(".,;:!?'\")]}")

    if url.startswith("//"):
        url = "https:" + url
    elif base_url and url.startswith("/"):
        url = urljoin(base_url, url)

    return url if url.startswith(("http://", "https://")) else ""



def strip_emoji_and_symbols(text: str) -> str:
    return "".join(
        " "
        if (
            unicodedata.category(ch).startswith("S")
            and ch not in {"&", "+", "#"}
        )
        else ch
        for ch in (text or "")
    )



def clean_title(text: str) -> str:
    text = strip_emoji_and_symbols(text)
    text = TITLE_PREFIX_RE.sub("", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r'[@#*_>|"\'']+', " ", text)
    text = re.sub(
        r"\b(?:caption|repost|info lomba|infolomba)\b",
        " ",
        text,
        flags=re.I,
    )
    return normalize_space(text)[:140].strip(" -:|") or "Tanpa Judul"



def safe_json_loads(text: str) -> list:
    try:
        return json.loads(text)
    except Exception:
        return []



def extract_deadline(text: str) -> str:
    if not text:
        return ""

    match = DEADLINE_PATTERN.search(text)

    if not match:
        return ""

    day, month, year = match.groups()
    month_norm = MONTH_MAP.get(month.lower(), month)

    return f"{day} {month_norm}" + (f" {year}" if year else "")



def extract_kategori(text: str, title: str = "") -> str:
    combined = f"{title} {text}".lower()
    scores = {}

    for kategori, config in KATEGORI_KEYWORDS.items():
        matches = sum(1 for kw in config["keywords"] if kw in combined)

        if matches > 0:
            scores[kategori] = (matches, config["priority"])

    if not scores:
        return "Lainnya"

    best = sorted(scores.items(), key=lambda x: (-x[1][0], x[1][1]))[0]
    return best[0]


# ============================================================================
# LINK EXTRACTION
# ============================================================================


def extract_urls_from_text(text: str) -> list[str]:
    seen = set()
    result = []

    for match in URL_RE.finditer(text or ""):
        url = clean_url(match.group(0))

        if url and url not in seen:
            seen.add(url)
            result.append(url)

    return result



def extract_registration_links(text: str = "") -> list[str]:
    found = []
    seen = set()

    for url in extract_urls_from_text(text):
        netloc = urlparse(url).netloc.lower()

        if any(host in netloc for host in FORM_HOSTS):
            if url not in seen:
                found.append(url)
                seen.add(url)

    return found


# ============================================================================
# GEMINI
# ============================================================================


def create_gemini_client():
    if not GEMINI_API_KEY or not genai:
        return None

    try:
        return genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        return None


GEMINI_CLIENT = create_gemini_client()


LLM_PROMPT = (
    "Analisis data lomba mahasiswa. Return JSON array format: "
    '{"i":"id","j":"judul","o":"penyelenggara","d":"deadline"}. '
    "Jika field tidak ditemukan isi string kosong. Data:\n"
)



def llm_call(prompt: str) -> list:
    if not GEMINI_CLIENT or not genai_types:
        return []

    try:
        response = GEMINI_CLIENT.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )

        return safe_json_loads(response.text or "")

    except Exception as exc:
        print(f"[LLM] Error: {exc}")
        return []



def process_batch_with_gemini(batch: list) -> list:
    if not GEMINI_CLIENT or not batch:
        return batch

    payload = [
        {
            "i": item["id"],
            "judul": item.get("judul", "")[:100],
            "caption": item.get("caption", "")[:800],
        }
        for item in batch
    ]

    llm_map = {
        row["i"]: row
        for row in llm_call(
            LLM_PROMPT + json.dumps(payload, ensure_ascii=False)
        )
        if isinstance(row, dict) and row.get("i")
    }

    for item in batch:
        row = llm_map.get(item["id"], {})

        if row.get("j"):
            item["judul"] = clean_title(row["j"])

        if row.get("o"):
            item["penyelenggara"] = row["o"].strip()[:100]

        if row.get("d"):
            item["deadline"] = row["d"].strip()[:50]

    return batch


# ============================================================================
# BUILD ITEM
# ============================================================================


def build_item(
    uid,
    source,
    title,
    poster,
    caption,
    links,
    direct_url,
):
    return {
        "id": uid,
        "sumber": source,
        "judul": title,
        "poster": poster,
        "caption": caption,
        "link_pendaftaran": links,
        "link_direct": direct_url,
        "deadline": extract_deadline(caption),
        "kategori": extract_kategori(caption, title),
        "penyelenggara": "",
    }


# ============================================================================
# INFOLMBA SCRAPER
# ============================================================================


def scrape_infolomba(seen_ids: set) -> list:
    print("[infolomba] Starting...")

    base_url = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()
    results = []

    try:
        response = scraper.get(base_url, headers=HEADERS, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        links = {
            urljoin(base_url, a["href"])
            for a in soup.find_all("a", href=lambda h: h and "info-" in h)
        }

        for link in list(links)[:MAX_WEB_ITEMS]:
            try:
                detail = scraper.get(link, headers=HEADERS, timeout=30)

                if detail.status_code != 200:
                    continue

                dsoup = BeautifulSoup(detail.text, "html.parser")
                full_text = dsoup.get_text("\n")

                if not is_mahasiswa(full_text):
                    continue

                title_tag = dsoup.find(["h1", "h2"])
                title = clean_title(
                    title_tag.get_text(" ") if title_tag else "Tanpa Judul"
                )

                uid = make_id(title, "infolomba.id")

                if uid in seen_ids:
                    continue

                caption = "\n".join(
                    line.strip()
                    for line in full_text.splitlines()
                    if line.strip()
                )[:2500]

                poster = ""

                meta = dsoup.find("meta", attrs={"property": "og:image"})

                if meta:
                    poster = clean_url(meta.get("content", ""), base_url)

                item = build_item(
                    uid,
                    "infolomba.id",
                    title,
                    poster,
                    caption,
                    extract_registration_links(caption),
                    link,
                )

                results.append(item)
                seen_ids.add(uid)

            except Exception as exc:
                print(f"[infolomba] Skip: {exc}")

    except Exception as exc:
        print(f"[infolomba] Error: {exc}")

    print(f"[infolomba] Done: {len(results)} items")
    return results


# ============================================================================
# DEDUP
# ============================================================================


def tokenize(title: str) -> set:
    return {
        t
        for t in re.sub(r"[^\w\s]", " ", (title or "").lower()).split()
        if t not in DEDUP_STOPWORDS and len(t) > 1
    }



def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0



def dedup_results(new_items: list, db_data: list) -> list:
    db_tokens = [tokenize(d.get("judul", "")) for d in db_data]

    unique = []

    for item in new_items:
        token = tokenize(item.get("judul", ""))

        if any(jaccard(token, db_tok) >= 0.6 for db_tok in db_tokens):
            continue

        unique.append(item)

    return unique


# ============================================================================
# MAIN
# ============================================================================


async def main():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not set")

    print("[INFO] Connecting MongoDB...")

    client = pymongo.MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION]

    db_data = list(
        collection.find({}, {"id": 1, "judul": 1, "_id": 0})
    )

    seen_ids = {
        d["id"]
        for d in db_data
        if "id" in d
    }

    batches = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, seen_ids),
    )

    raw = [
        item
        for batch in batches
        if isinstance(batch, list)
        for item in batch
    ]

    print(f"[INFO] Raw items: {len(raw)}")

    processed = []

    for i in range(0, len(raw), 15):
        batch = raw[i:i + 15]
        processed.extend(process_batch_with_gemini(batch))

    final = dedup_results(processed, db_data)

    if not final:
        print("[INFO] No new data")
        client.close()
        return

    result = collection.bulk_write(
        [
            UpdateOne(
                {"id": item["id"]},
                {"$set": item},
                upsert=True,
            )
            for item in final
        ]
    )

    print(
        f"[INFO] Saved: {result.upserted_count} new, "
        f"{result.modified_count} updated"
    )

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
