"""Competition scraper: infolomba.id, silomba.id, and Instagram."""

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
import cloudscraper
import pymongo
from pymongo import UpdateOne
from google import genai

# CONFIG
IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
MONGO_URI     = os.environ.get("MONGO_URI")
DB_NAME       = "competition_scraper"
COLLECTION    = "competition"
HEADERS       = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

try:
    gemini_model = genai.GenerativeModel(
        model_name="gemini-2.0-flash-lite",
        generation_config={"response_mime_type": "application/json"},
    )
except Exception:
    gemini_model = None


# HELPERS
def buat_id(judul: str, sumber: str) -> str:
    return hashlib.md5(f"{sumber}_{judul}".lower().strip().encode()).hexdigest()[:12]


def is_mahasiswa(teks: str) -> bool:
    keywords = [
        'mahasiswa', 'mahasiswi', 'universitas', 'kampus',
        's1', 'd3', 'd4', 'umum', 'undergraduate', 'diploma',
        'student', 'university',
    ]
    return any(kw in teks.lower() for kw in keywords)


# LLM
def _llm_call(prompt: str) -> list:
    if not GEMINI_CLIENT or not genai_types:
        return []
    for attempt in range(3):
        try:
            return json.loads(gemini_model.generate_content(prompt).text)
        except Exception as e:
            print(f"[LLM] Error attempt {attempt + 1}: {e}")
            time.sleep(15)
    return []


def proses_batch_dengan_gemini(data_batch: list) -> list:
    if not gemini_model or not data_batch:
        return data_batch

    grup_web = [x for x in data_batch if "IG" not in x["sumber"]]
    grup_ig  = [x for x in data_batch if "IG"     in x["sumber"]]
    llm_map  = {}

    if grup_web:
        payload  = [{"i": x["id"], "c": x["caption"][:400]} for x in grup_web]
        instruksi = (
            'Ekstrak nama resmi lomba dari field c tiap item. '
            'Hapus emoji, simbol (* _ # >), sapaan pembuka. '
            'Jika tidak ada tulis "Tanpa Judul". '
            'Output JSON Array. Format: {"i":"...","j":"..."}'
        )
        for row in _llm_call(instruksi + "\n\nData: " + json.dumps(payload, ensure_ascii=False)):
            llm_map[row.get("i", "")] = {"judul": row.get("j", ""), "link_pendaftaran": []}

    if grup_ig:
        payload  = [{"i": x["id"], "c": x["caption"][:1000]} for x in grup_ig]
        instruksi = (
            'Dari field c tiap item ekstrak: '
            '1) j: nama resmi lomba, hapus emoji/simbol/sapaan, jika tidak ada tulis "Tanpa Judul". '
            '2) l: semua URL di caption sebagai array string, jika tidak ada tulis []. '
            'Output JSON Array. Format: {"i":"...","j":"...","l":[]}'
        )
        for row in _llm_call(instruksi + "\n\nData: " + json.dumps(payload, ensure_ascii=False)):
            llm_map[row.get("i", "")] = {
                "judul": row.get("j", ""),
                "link_pendaftaran": list(dict.fromkeys(row.get("l", []))),
            }

    for item in data_batch:
        llm = llm_map.get(item["id"], {})
        item["judul"] = llm.get("judul") or item["judul"]
        if "IG" in item["sumber"]:
            item["link_pendaftaran"] = llm.get("link_pendaftaran", [])

    time.sleep(3)
    return data_batch


# SCRAPER: infolomba.id
def scrape_infolomba(id_sudah_ada: set) -> list:
    print("[infolomba] Mulai scraping...")
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

    print(f"[infolomba] Selesai: {len(hasil)} data")
    return hasil


# SCRAPER: silomba.id
async def scrape_silomba(id_sudah_ada: set) -> list:
    print("[silomba] Mulai scraping...")
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

    print(f"[silomba] Selesai: {len(hasil)} data")
    return hasil


# SCRAPER: Instagram
def scrape_instagram(id_sudah_ada: set) -> list:
    if not IG_SESSION_ID:
        print("[IG] IG_SESSION_ID tidak diset, skip.")
        return []

    print("[IG] Mulai scraping...")
    hasil = []

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

    return _build_item(
        uid, f"IG @{account}",
        extract_title_from_caption(caption),
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


# DEDUP
def _token_judul(judul: str) -> set:
    stopwords = {
        'the','of','and','in','on','at','to','a','an',
        'di','ke','se','dan','atau','untuk','dengan','dalam','dari','oleh','yang','adalah','ini','itu',
    }
    return {t for t in re.sub(r'[^\w\s]', ' ', judul.lower()).split()
            if t not in stopwords and len(t) > 1}


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


# MAIN
async def main():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not set.")

    print("[INFO] Connecting to MongoDB...")
    client = pymongo.MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION]

    data_di_db   = list(collection.find({}, {"id": 1, "link_direct": 1, "judul": 1, "_id": 0}))
    id_sudah_ada = {d["id"] for d in data_di_db if "id" in d}
    print(f"[INFO] {len(id_sudah_ada)} data sudah ada di DB.")

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