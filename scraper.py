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

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = "competition_scraper"
COLLECTION_NAME = "competition"

try:
    gemini_client = genai.Client()
except Exception:
    gemini_client = None


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def buat_id(judul: str, sumber: str) -> str:
    return hashlib.md5(f"{sumber}_{judul}".lower().strip().encode()).hexdigest()[:12]


def is_mahasiswa(teks: str) -> bool:
    teks_lower = str(teks).lower()
    keywords = [
        'mahasiswa', 'mahasiswi', 'universitas', 'kampus',
        's1', 'd3', 'd4', 'umum', 'undergraduate', 'diploma',
        'student', 'university',
    ]
    return any(kw in teks_lower for kw in keywords)


# ---------------------------------------------------------------------------
# LLM — ekstrak judul dari caption (semua sumber)
#      + ekstrak link_pendaftaran dari caption (khusus IG)
#
# Dipanggil SETELAH semua scraping selesai.
# ---------------------------------------------------------------------------
def proses_batch_dengan_gemini(data_batch: list[dict]) -> list[dict]:
    if not gemini_client or not data_batch:
        return data_batch

    # Payload minimal: id, sumber, caption saja
    payload_llm = [
        {
            "id": item["id"],
            "sumber": item["sumber"],
            "caption": item["caption"][:2000],
        }
        for item in data_batch
    ]

    prompt = f"""Kamu adalah AI Data Extractor untuk info lomba/kompetisi Indonesia. Proses setiap item dalam JSON Array berikut.

TUGAS PER FIELD:

1. "judul"
   - Temukan nama resmi acara/kompetisi dari "caption".
   - Hapus semua: emoji, simbol markdown (* _ # >), kata sapaan/pembuka ("Hai!", "Halo sobat", "Telah dibuka", "Yuk daftar", "Kabar gembira", "Are you ready", dll).
   - Ciri nama lomba: biasanya mengandung kata seperti Kompetisi, Competition, Lomba, Fest, Championship, Olympiad, Olimpiade, Hackathon, Call for, Festival, Award, Grant, atau nama event resmi lainnya.
   - Hasil akhir: nama lomba yang bersih dan singkat, tanpa kalimat berlebih.
   - Jika benar-benar tidak ditemukan, kembalikan "Tanpa Judul".

2. "link_pendaftaran"
   - Jika "sumber" mengandung "IG": ekstrak SEMUA URL yang ada di "caption" → array of strings. Jika tidak ada, kembalikan [].
   - Jika "sumber" TIDAK mengandung "IG": kembalikan [] karena link sudah di-scrape terpisah.

3. Jangan ubah nilai "id".
4. Output: HANYA JSON Array valid. Tanpa markdown code block, tanpa teks penjelasan apapun.

Format output setiap item:
{{"id": "...", "judul": "...", "link_pendaftaran": []}}

Data:
{json.dumps(payload_llm, ensure_ascii=False)}"""

    for attempt in range(3):
        try:
            res = gemini_client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            hasil_llm: list[dict] = json.loads(res.text)
            llm_map = {str(item["id"]): item for item in hasil_llm}

            for item in data_batch:
                llm = llm_map.get(str(item["id"]), {})

                # Judul: hasil LLM, fallback ke judul_kasar
                item["judul"] = llm.get("judul") or item["judul"]

                # Link pendaftaran: hanya update untuk IG
                if "IG" in item["sumber"]:
                    item["link_pendaftaran"] = list(dict.fromkeys(
                        llm.get("link_pendaftaran", [])
                    ))
                # non-IG: link_pendaftaran sudah di-scrape, tidak diubah

            time.sleep(3)
            return data_batch

        except Exception as e:
            print(f"[LLM] Error attempt {attempt + 1}: {e}")
            time.sleep(15)

    print("[LLM] Semua retry gagal, mengembalikan data mentah.")
    return data_batch


# ---------------------------------------------------------------------------
# SCRAPER: infolomba.id
# Scrape : caption, poster, sumber, link_direct, link_pendaftaran
# LLM    : judul
# ---------------------------------------------------------------------------
def scrape_infolomba(id_sudah_ada: set) -> list[dict]:
    print("[infolomba] Mulai scraping...")
    base_url = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()
    hasil = []

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

                dsoup = BeautifulSoup(res.text, 'html.parser')
                full_text = dsoup.get_text(separator=' ')
                if not is_mahasiswa(full_text):
                    continue

                # Judul kasar dari slug URL — hanya sebagai seed untuk buat_id & fallback
                judul_kasar = (
                    '-'.join(link.rstrip('/').split('/')[-1].replace('info-', '', 1).split('-')[:-1])
                    .replace('-', ' ').title()
                )
                uid = buat_id(judul_kasar, "infolomba.id")
                if uid in id_sudah_ada:
                    continue

                # Caption: konten utama halaman (LLM cari judul dari sini)
                if "Daftar Sekarang" in full_text and "Laporkan Lomba" in full_text:
                    caption = '\n'.join(
                        l.strip() for l in
                        full_text.split("Daftar Sekarang")[-1].split("Laporkan Lomba")[0].split('\n')
                        if l.strip()
                    )
                else:
                    caption = full_text[:2000]

                # Poster
                poster_url = (
                    (anchor.find('img') or {}).get('src')
                    or (anchor.find('img') or {}).get('data-src')
                )
                if not poster_url:
                    wadah = anchor.find_parent('div')
                    img_card = (wadah.find_parent('div') or wadah).find('img') if wadah else None
                    poster_url = (img_card or {}).get('src') or (img_card or {}).get('data-src', '')
                if poster_url and not poster_url.startswith('http'):
                    poster_url = urljoin(base_url, poster_url)
                if not poster_url:
                    poster_url = next(
                        (urljoin(base_url, img.get('src') or img.get('data-src', ''))
                         for img in dsoup.find_all('img')
                         if '/poster/' in (img.get('src') or img.get('data-src', ''))),
                        ''
                    )

                # Link pendaftaran: scrape dari tombol
                btn = dsoup.find(lambda t: t.name == 'a' and t.text and 'Daftar Sekarang' in t.text)
                link_pendaftaran = (
                    [btn['href']]
                    if btn and btn.get('href')
                    and btn['href'] not in ['#', '']
                    and not btn['href'].startswith('javascript')
                    else []
                )

                hasil.append({
                    "id": uid,
                    "sumber": "infolomba.id",
                    "judul": judul_kasar,       # LLM ganti dengan nama resmi
                    "poster": poster_url,
                    "caption": caption,
                    "link_pendaftaran": link_pendaftaran,
                    "link_direct": link,
                })
                id_sudah_ada.add(uid)

            except Exception:
                pass

    except Exception as e:
        print(f"[infolomba] Error: {e}")

    print(f"[infolomba] Selesai: {len(hasil)} data")
    return hasil


# ---------------------------------------------------------------------------
# SCRAPER: silomba.id
# Scrape : caption, poster, sumber, link_direct, link_pendaftaran
# LLM    : judul
# ---------------------------------------------------------------------------
async def scrape_silomba(id_sudah_ada: set) -> list[dict]:
    print("[silomba] Mulai scraping...")
    base_url = "https://silomba.id"
    hasil = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
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
                # Judul kasar dari aria-label — hanya seed untuk buat_id & fallback
                judul_kasar = (
                    card.get('aria-label', '').replace('Lihat detail kompetisi ', '').strip()
                    or (card.find(['h2', 'h3', 'h4']).text.strip()
                        if card.find(['h2', 'h3', 'h4']) else "Tanpa judul")
                )
                uid = buat_id(judul_kasar, "silomba.id")
                if uid in id_sudah_ada:
                    continue

                link_detail = urljoin(base_url, card['href'])
                poster, caption, link_pendaftaran = '', '', []

                try:
                    dp = await browser.new_page()
                    await dp.goto(link_detail, wait_until="networkidle")
                    dsoup = BeautifulSoup(await dp.content(), 'html.parser')
                    await dp.close()

                    full_text = dsoup.get_text(separator=' ')
                    if not is_mahasiswa(full_text):
                        continue

                    poster = (
                        dsoup.find('img', src=lambda s: s and 'original-poster' in s)
                        or dsoup.find('img', src=lambda s: s and 'storage2.silomba.id' in s)
                        or {}
                    ).get('src', '')

                    # Caption: ambil dari section deskripsi (LLM cari judul dari sini)
                    caption = (
                        full_text.split("Deskripsi Lomba")[-1].strip()
                        if "Deskripsi Lomba" in full_text
                        else full_text[:2000]
                    )

                    link_pendaftaran = list(set(
                        btn.get('href')
                        for btn in dsoup.find_all(
                            lambda t: t.name == 'a' and t.text
                            and any(x in t.text for x in ['Daftar', 'Website Resmi', 'Register'])
                        )
                        if btn.get('href')
                        and btn.get('href') != '#'
                        and not btn.get('href').startswith('javascript')
                    ))

                except Exception:
                    pass

                hasil.append({
                    "id": uid,
                    "sumber": "silomba.id",
                    "judul": judul_kasar,       # LLM ganti dengan nama resmi
                    "poster": poster,
                    "caption": caption,
                    "link_pendaftaran": link_pendaftaran,
                    "link_direct": link_detail,
                })
                id_sudah_ada.add(uid)

        except Exception as e:
            print(f"[silomba] Error: {e}")
        finally:
            await browser.close()

    print(f"[silomba] Selesai: {len(hasil)} data")
    return hasil


# ---------------------------------------------------------------------------
# SCRAPER: Instagram
# Scrape : caption, poster, sumber, link_direct
# LLM    : judul + link_pendaftaran (keduanya dari caption)
# ---------------------------------------------------------------------------
def scrape_instagram(id_sudah_ada: set) -> list[dict]:
    if not IG_SESSION_ID:
        print("[IG] IG_SESSION_ID tidak diset, skip.")
        return []

    print("[IG] Mulai scraping...")
    hasil = []
    opts = Options()
    for arg in [
        "--headless=new", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", f"user-agent={HEADERS['User-Agent']}",
    ]:
        opts.add_argument(arg)
    opts.binary_location = "/opt/chrome/chrome"
    driver = webdriver.Chrome(
        service=Service(executable_path="/usr/bin/chromedriver"), options=opts
    )

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

            last_h = driver.execute_script("return document.body.scrollHeight")
            url_posts = []
            for _ in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(random.randint(2, 4))
                url_posts.extend(
                    l.get_attribute('href')
                    for l in driver.find_elements(
                        By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]'
                    )
                    if l.get_attribute('href')
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

                    # Baris pertama caption sebagai seed untuk buat_id; LLM ganti judulnya
                    judul_kasar = caption.split('\n')[0][:100].strip() or str(time.time())
                    uid = buat_id(judul_kasar, f"IG @{akun}")
                    if uid in id_sudah_ada:
                        continue

                    poster = driver.execute_script(
                        "var i=document.querySelectorAll('img');"
                        "for(var j=0;j<i.length;j++){"
                        "var s=i[j].src||'';"
                        "if((i[j].alt||'').toLowerCase().includes('profile')||s.includes('150x150'))continue;"
                        "if(s.includes('scontent')||s.includes('cdninstagram')){"
                        "var sr=i[j].srcset;return sr?sr.split(',').pop().trim().split(' ')[0]:s;"
                        "}}return '';"
                    ) or (
                        driver.find_element(
                            By.XPATH, '//meta[@property="og:image"]'
                        ).get_attribute('content')
                        if driver.find_elements(By.XPATH, '//meta[@property="og:image"]')
                        else ""
                    )

                    hasil.append({
                        "id": uid,
                        "sumber": f"IG @{akun}",
                        "judul": judul_kasar,   # LLM ganti dengan nama resmi dari caption
                        "poster": poster,
                        "caption": caption,
                        "link_pendaftaran": [], # LLM ekstrak dari caption
                        "link_direct": url,
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


# ---------------------------------------------------------------------------
# MAIN
# Alur: scrape paralel → raw JSON → LLM batch → simpan DB
# ---------------------------------------------------------------------------
async def main():
    print("[INFO] Menghubungkan ke MongoDB...")
    client = pymongo.MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION_NAME]
    id_sudah_ada = set(
        i['id'] for i in collection.find({}, {"id": 1, "_id": 0}) if 'id' in i
    )

    # === FASE 1: SCRAPING PARALEL ===
    results = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, id_sudah_ada),
        scrape_silomba(id_sudah_ada),
        asyncio.to_thread(scrape_instagram, id_sudah_ada),
    )

    hasil_mentah: list[dict] = [
        item for batch in results if isinstance(batch, list) for item in batch
    ]
    print(f"[INFO] Total {len(hasil_mentah)} data mentah baru ditemukan.")

    if not hasil_mentah:
        print("[INFO] Tidak ada data baru.")
        client.close()
        return

    # === FASE 2: LLM — ekstrak judul (semua) + link_pendaftaran (IG) ===
    BATCH_SIZE = 15
    hasil_final: list[dict] = []
    for i in range(0, len(hasil_mentah), BATCH_SIZE):
        batch = hasil_mentah[i:i + BATCH_SIZE]
        print(f"[LLM] Batch {i // BATCH_SIZE + 1} ({len(batch)} item)...")
        hasil_final.extend(proses_batch_dengan_gemini(batch))

    # === FASE 3: SIMPAN KE DATABASE ===
    result = collection.bulk_write([
        UpdateOne({'id': item['id']}, {'$set': item}, upsert=True)
        for item in hasil_final
    ])
    print(f"[INFO] Disimpan: {result.upserted_count} baru, {result.modified_count} update.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
