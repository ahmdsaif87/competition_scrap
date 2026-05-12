import os
import time
import random
import asyncio
import re
import hashlib
import json
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import cloudscraper
import pymongo
from pymongo import UpdateOne
from google import genai

# =============================================================================
# CONFIG
# =============================================================================
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


# =============================================================================
# HELPERS
# =============================================================================
def buat_id(judul: str, sumber: str) -> str:
    return hashlib.md5(f"{sumber}_{judul}".lower().strip().encode()).hexdigest()[:12]


def is_mahasiswa(teks: str) -> bool:
    keywords = [
        'mahasiswa', 'mahasiswi', 'universitas', 'kampus',
        's1', 'd3', 'd4', 'umum', 'undergraduate', 'diploma',
        'student', 'university',
    ]
    return any(kw in teks.lower() for kw in keywords)


# =============================================================================
# LLM
# Website : ekstrak judul dari caption (caption[:400])
# IG      : ekstrak judul + link_pendaftaran dari caption (caption[:1000])
# Key payload disingkat (i/c/j/l) untuk hemat token.
# =============================================================================
def _llm_call(prompt: str) -> list:
    for attempt in range(3):
        try:
            raw = gemini_model.generate_content(prompt).text
            print(f"[LLM] Raw response: {raw[:300]}")
            return json.loads(raw)
        except Exception as e:
            print(f"[LLM] Error attempt {attempt + 1}: {e}")
            time.sleep(15)
    return []


def _strip_emoji(teks: str) -> str:
    """Hapus semua emoji dan simbol non-ASCII dari string."""
    return re.sub(r'[^\u0000-\u024F\u1E00-\u1EFF]', '', teks).strip()


def proses_batch_dengan_gemini(data_batch: list) -> list:
    if not gemini_model or not data_batch:
        return data_batch

    grup_web = [x for x in data_batch if "IG" not in x["sumber"]]
    grup_ig  = [x for x in data_batch if "IG"     in x["sumber"]]

    # llm_map: key = id (string), value = {judul, link_pendaftaran}
    llm_map: dict = {}

    if grup_web:
        payload = [{"i": x["id"], "c": x["caption"][:400]} for x in grup_web]
        instruksi = (
            'Dari field c tiap item, temukan nama resmi lomba/kompetisi. '
            'Nama lomba biasanya mengandung kata: Kompetisi, Lomba, Competition, Olympiad, Hackathon, Festival, Award, Call for. '
            'WAJIB hapus semua emoji dan simbol dari hasil. Contoh: "🚀 Open Registration BMC 2026⭐" → "Open Registration BMC 2026". '
            'Jika tidak ditemukan tulis "Tanpa Judul". '
            'Output JSON Array. Format tiap item: {"i":"...","j":"..."}'
        )
        hasil = _llm_call(instruksi + "\n\nData: " + json.dumps(payload, ensure_ascii=False))
        for row in hasil:
            uid = str(row.get("i", ""))
            llm_map[uid] = {
                "judul": _strip_emoji(row.get("j", "")),
                "link_pendaftaran": [],
            }

    if grup_ig:
        payload = [{"i": x["id"], "c": x["caption"][:1000]} for x in grup_ig]
        instruksi = (
            'Dari field c tiap item ekstrak dua hal: '
            '1) j: nama resmi lomba/kompetisi. WAJIB hapus semua emoji dan simbol. Contoh: "🚀 Open Registration BMC 2026⭐" → "Open Registration BMC 2026". Jika tidak ada tulis "Tanpa Judul". '
            '2) l: array semua URL yang ada di caption (http/https/bit.ly/s.id/linktr.ee/forms.gle). Jika tidak ada URL tulis []. '
            'Output JSON Array. Format tiap item: {"i":"...","j":"...","l":[]}'
        )
        hasil = _llm_call(instruksi + "\n\nData: " + json.dumps(payload, ensure_ascii=False))
        for row in hasil:
            uid = str(row.get("i", ""))
            llm_map[uid] = {
                "judul": _strip_emoji(row.get("j", "")),
                "link_pendaftaran": list(dict.fromkeys(row.get("l", []))),
            }

    for item in data_batch:
        llm = llm_map.get(str(item["id"]), {})  # pastikan key selalu string
        if llm.get("judul"):
            item["judul"] = llm["judul"]
        if "IG" in item["sumber"]:
            item["link_pendaftaran"] = llm.get("link_pendaftaran", [])

    time.sleep(3)
    return data_batch


# =============================================================================
# SCRAPER: infolomba.id
# Scrape : poster, caption, link_pendaftaran, link_direct
# LLM    : judul
# =============================================================================
def scrape_infolomba(id_sudah_ada: set) -> list:
    print("[infolomba] Mulai scraping...")
    base_url = "https://infolomba.id"
    scraper  = cloudscraper.create_scraper()
    hasil    = []

    try:
        soup = BeautifulSoup(scraper.get(base_url, headers=HEADERS).text, 'html.parser')
        links_unik = list({
            urljoin(base_url, a.get('href', '')): a
            for a in soup.find_all('a', href=lambda h: h and 'info-' in h)
            if urljoin(base_url, a.get('href', '')).startswith(base_url + '/')
        }.items())

        for link, anchor in links_unik[:15]:
            try:
                res = scraper.get(link, headers=HEADERS)
                if res.status_code != 200:
                    continue

                dsoup     = BeautifulSoup(res.text, 'html.parser')
                full_text = dsoup.get_text(separator=' ')
                if not is_mahasiswa(full_text):
                    continue

                judul_kasar = (
                    '-'.join(link.rstrip('/').split('/')[-1].replace('info-', '', 1).split('-')[:-1])
                    .replace('-', ' ').title()
                )
                uid = buat_id(judul_kasar, "infolomba.id")
                if uid in id_sudah_ada:
                    continue

                # Caption
                if "Daftar Sekarang" in full_text and "Laporkan Lomba" in full_text:
                    caption = '\n'.join(
                        l.strip() for l in
                        full_text.split("Daftar Sekarang")[-1].split("Laporkan Lomba")[0].split('\n')
                        if l.strip()
                    )
                else:
                    caption = full_text[:2000]

                # Poster
                poster = (anchor.find('img') or {}).get('src') or (anchor.find('img') or {}).get('data-src')
                if not poster:
                    wadah  = anchor.find_parent('div')
                    img    = (wadah.find_parent('div') or wadah).find('img') if wadah else None
                    poster = (img or {}).get('src') or (img or {}).get('data-src', '')
                if poster and not poster.startswith('http'):
                    poster = urljoin(base_url, poster)
                if not poster:
                    poster = next(
                        (urljoin(base_url, img.get('src') or img.get('data-src', ''))
                         for img in dsoup.find_all('img')
                         if '/poster/' in (img.get('src') or img.get('data-src', ''))),
                        ''
                    )

                # Link pendaftaran
                btn = dsoup.find(lambda t: t.name == 'a' and t.text and 'Daftar Sekarang' in t.text)
                link_pendaftaran = (
                    [btn['href']] if btn and btn.get('href')
                    and btn['href'] not in ['#', '']
                    and not btn['href'].startswith('javascript')
                    else []
                )

                hasil.append({
                    "id": uid, "sumber": "infolomba.id", "judul": judul_kasar,
                    "poster": poster, "caption": caption,
                    "link_pendaftaran": link_pendaftaran, "link_direct": link,
                })
                id_sudah_ada.add(uid)

            except Exception:
                pass

    except Exception as e:
        print(f"[infolomba] Error: {e}")

    print(f"[infolomba] Selesai: {len(hasil)} data")
    return hasil


# =============================================================================
# SCRAPER: silomba.id
# Scrape : poster, caption, link_pendaftaran, link_direct
# LLM    : judul
# =============================================================================
async def scrape_silomba(id_sudah_ada: set) -> list:
    print("[silomba] Mulai scraping...")
    base_url = "https://silomba.id"
    hasil    = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(base_url, wait_until="networkidle")
            await page.wait_for_selector("#competition-section", timeout=15000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            soup = BeautifulSoup(await page.content(), 'html.parser')
            await page.close()

            section = soup.find(id='competition-section')
            if not section:
                return hasil

            for card in section.find_all('a', href=lambda h: h and h.startswith('/lomba/')):
                judul_kasar = (
                    card.get('aria-label', '').replace('Lihat detail kompetisi ', '').strip()
                    or (card.find(['h2', 'h3', 'h4']).text.strip()
                        if card.find(['h2', 'h3', 'h4']) else "Tanpa judul")
                )
                uid = buat_id(judul_kasar, "silomba.id")
                if uid in id_sudah_ada:
                    continue

                link_detail      = urljoin(base_url, card['href'])
                poster, caption, link_pendaftaran = '', '', []

                try:
                    dp = await browser.new_page()
                    await dp.goto(link_detail, wait_until="networkidle")
                    dsoup     = BeautifulSoup(await dp.content(), 'html.parser')
                    full_text = dsoup.get_text(separator=' ')
                    await dp.close()

                    if not is_mahasiswa(full_text):
                        continue

                    poster = (
                        dsoup.find('img', src=lambda s: s and 'original-poster' in s)
                        or dsoup.find('img', src=lambda s: s and 'storage2.silomba.id' in s)
                        or {}
                    ).get('src', '')

                    caption = (
                        full_text.split("Deskripsi Lomba")[-1].strip()
                        if "Deskripsi Lomba" in full_text else full_text[:2000]
                    )

                    link_pendaftaran = list(set(
                        btn.get('href') for btn in dsoup.find_all(
                            lambda t: t.name == 'a' and t.text
                            and any(x in t.text for x in ['Daftar', 'Website Resmi', 'Register'])
                        )
                        if btn.get('href') and btn.get('href') != '#'
                        and not btn.get('href').startswith('javascript')
                    ))

                except Exception:
                    pass

                hasil.append({
                    "id": uid, "sumber": "silomba.id", "judul": judul_kasar,
                    "poster": poster, "caption": caption,
                    "link_pendaftaran": link_pendaftaran, "link_direct": link_detail,
                })
                id_sudah_ada.add(uid)

        except Exception as e:
            print(f"[silomba] Error: {e}")
        finally:
            await browser.close()

    print(f"[silomba] Selesai: {len(hasil)} data")
    return hasil


# =============================================================================
# SCRAPER: Instagram
# Scrape : poster, caption, link_direct
# LLM    : judul + link_pendaftaran (dari caption)
# =============================================================================
def scrape_instagram(id_sudah_ada: set) -> list:
    if not IG_SESSION_ID:
        print("[IG] IG_SESSION_ID tidak diset, skip.")
        return []

    print("[IG] Mulai scraping...")
    hasil = []

    opts = Options()
    for arg in ["--headless=new", "--disable-gpu", "--no-sandbox",
                "--disable-dev-shm-usage", f"user-agent={HEADERS['User-Agent']}"]:
        opts.add_argument(arg)
    opts.binary_location = "/opt/chrome/chrome"
    driver = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=opts)

    try:
        driver.get("https://www.instagram.com/")
        time.sleep(3)
        driver.add_cookie({'name': 'sessionid', 'value': IG_SESSION_ID, 'domain': '.instagram.com'})
        driver.refresh()
        time.sleep(5)
        if "login" in driver.current_url:
            print("[IG] Session tidak valid.")
            return []

        for akun in ['infolomba', 'infolomba_gratis', 'infolomba.olimpiade']:
            driver.get(f"https://www.instagram.com/{akun}/")
            time.sleep(random.randint(4, 6))
            if "Page Not Found" in driver.title:
                continue

            url_posts = []
            last_h    = driver.execute_script("return document.body.scrollHeight")
            for _ in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(random.randint(2, 4))
                url_posts.extend(
                    el.get_attribute('href')
                    for el in driver.find_elements(
                        By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]'
                    ) if el.get_attribute('href')
                )
                new_h = driver.execute_script("return document.body.scrollHeight")
                if new_h == last_h:
                    break
                last_h = new_h

            for url in list(dict.fromkeys(url_posts))[:6]:
                try:
                    driver.get(url)
                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.TAG_NAME, "img"))
                        )
                    except Exception:
                        pass
                    time.sleep(random.randint(3, 5))

                    caption = ""
                    try:
                        caption = driver.find_element(By.XPATH, '//h1').text
                    except Exception:
                        try:
                            m = driver.find_element(
                                By.XPATH, '//meta[@property="og:description"]'
                            ).get_attribute('content')
                            caption = m.split(": ", 1)[1].strip('"') if ": " in m else m
                        except Exception:
                            caption = driver.title

                    if not caption or not is_mahasiswa(caption):
                        continue

                    judul_kasar = caption.split('\n')[0][:100].strip() or str(time.time())
                    uid = buat_id(judul_kasar, f"IG @{akun}")
                    if uid in id_sudah_ada:
                        continue

                    poster = driver.execute_script(
                        "var i=document.querySelectorAll('img');"
                        "for(var j=0;j<i.length;j++){"
                        "  var s=i[j].src||'';"
                        "  if((i[j].alt||'').toLowerCase().includes('profile')||s.includes('150x150'))continue;"
                        "  if(s.includes('scontent')||s.includes('cdninstagram')){"
                        "    var sr=i[j].srcset;return sr?sr.split(',').pop().trim().split(' ')[0]:s;"
                        "  }"
                        "}return '';"
                    ) or (
                        driver.find_element(By.XPATH, '//meta[@property="og:image"]').get_attribute('content')
                        if driver.find_elements(By.XPATH, '//meta[@property="og:image"]') else ""
                    )

                    hasil.append({
                        "id": uid, "sumber": f"IG @{akun}", "judul": judul_kasar,
                        "poster": poster, "caption": caption,
                        "link_pendaftaran": [], "link_direct": url,
                    })
                    id_sudah_ada.add(uid)

                except Exception:
                    pass

    except Exception as e:
        print(f"[IG] Error: {e}")
    finally:
        driver.quit()

    print(f"[IG] Selesai: {len(hasil)} data")
    return hasil


# =============================================================================
# DEDUP
# Lapis 1 — id exact match          : dicegah di scraper (id_sudah_ada)
# Lapis 2 — link_direct exact match : vs DB
# Lapis 3 — Jaccard similarity judul: vs DB (threshold 0.6)
# Lapis 4 — Jaccard similarity judul: antar item baru, gabung link, menang prioritas
# Prioritas sumber: infolomba.id (0) > silomba.id (1) > IG (2)
# =============================================================================
def _token_judul(judul: str) -> set:
    stopwords = {
        'the','of','and','in','on','at','to','a','an',
        'di','ke','se','dan','atau','untuk','dengan','dalam','dari','oleh','yang','adalah','ini','itu',
    }
    return {t for t in re.sub(r'[^\w\s]', ' ', judul.lower()).split()
            if t not in stopwords and len(t) > 1}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


PRIORITAS_SUMBER = {"infolomba.id": 0, "silomba.id": 1}


def dedup_hasil(hasil_baru: list, data_di_db: list, threshold: float = 0.6) -> list:
    link_direct_db = {d["link_direct"] for d in data_di_db if d.get("link_direct")}
    token_judul_db = [_token_judul(d["judul"]) for d in data_di_db if d.get("judul")]
    unik = []

    for item in hasil_baru:
        token = _token_judul(item.get("judul", ""))
        link  = item.get("link_direct", "")

        if link and link in link_direct_db:
            print(f"[DEDUP] Skip URL duplikat: {link}")
            continue

        if any(_jaccard(token, t) >= threshold for t in token_judul_db):
            print(f"[DEDUP] Skip judul mirip di DB: {item['judul']!r}")
            continue

        dup_idx = next(
            (i for i, ex in enumerate(unik)
             if _jaccard(token, _token_judul(ex.get("judul", ""))) >= threshold),
            None
        )

        if dup_idx is not None:
            ex     = unik[dup_idx]
            merged = list(dict.fromkeys(ex["link_pendaftaran"] + item["link_pendaftaran"]))
            p_item = PRIORITAS_SUMBER.get(item["sumber"], 2)
            p_ex   = PRIORITAS_SUMBER.get(ex["sumber"], 2)
            if p_item < p_ex:
                item["link_pendaftaran"] = merged
                unik[dup_idx] = item
                print(f"[DEDUP] Ganti {ex['sumber']} → {item['sumber']}: {item['judul']!r}")
            else:
                unik[dup_idx]["link_pendaftaran"] = merged
                print(f"[DEDUP] Gabung link {item['sumber']} → {ex['sumber']}: {item['judul']!r}")
        else:
            unik.append(item)

    print(f"[DEDUP] {len(hasil_baru)} → {len(unik)} item unik.")
    return unik


# =============================================================================
# MAIN
# Fase 1: scrape paralel
# Fase 2: LLM (judul semua sumber, link_pendaftaran IG)
# Fase 3: dedup vs DB + antar sumber
# Fase 4: simpan ke MongoDB
# =============================================================================
async def main():
    print("[INFO] Menghubungkan ke MongoDB...")
    client     = pymongo.MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION]

    data_di_db   = list(collection.find({}, {"id": 1, "link_direct": 1, "judul": 1, "_id": 0}))
    id_sudah_ada = {d["id"] for d in data_di_db if "id" in d}
    print(f"[INFO] {len(id_sudah_ada)} data sudah ada di DB.")

    # Fase 1
    results = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, id_sudah_ada),
        scrape_silomba(id_sudah_ada),
        asyncio.to_thread(scrape_instagram, id_sudah_ada),
    )
    hasil_mentah = [item for batch in results if isinstance(batch, list) for item in batch]
    print(f"[INFO] {len(hasil_mentah)} data mentah baru ditemukan.")

    if not hasil_mentah:
        print("[INFO] Tidak ada data baru.")
        client.close()
        return

    # Fase 2
    BATCH_SIZE = 15
    hasil_llm  = []
    for i in range(0, len(hasil_mentah), BATCH_SIZE):
        batch = hasil_mentah[i:i + BATCH_SIZE]
        print(f"[LLM] Batch {i // BATCH_SIZE + 1} ({len(batch)} item)...")
        hasil_llm.extend(proses_batch_dengan_gemini(batch))

    # Fase 3
    hasil_final = dedup_hasil(hasil_llm, data_di_db)
    if not hasil_final:
        print("[INFO] Semua data duplikat.")
        client.close()
        return

    # Fase 4
    result = collection.bulk_write([
        UpdateOne({"id": item["id"]}, {"$set": item}, upsert=True)
        for item in hasil_final
    ])
    print(f"[INFO] Disimpan: {result.upserted_count} baru, {result.modified_count} diperbarui.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
