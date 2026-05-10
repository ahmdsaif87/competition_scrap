import os
import time
import random
import asyncio
import re
import hashlib
import json
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor
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


def ekstrak_link(teks: str) -> list[str]:
    """Ekstrak semua URL dari teks secara kasar (akan dipakai LLM sebagai petunjuk)."""
    links = re.findall(
        r'(https?://[^\s]+|www\.[^\s]+|bit\.ly/[^\s]+|linktr\.ee/[^\s]+|forms\.gle/[^\s]+|s\.id/[^\s]+)',
        teks
    )
    return [re.sub(r'[).,!]+$', '', l) for l in links]


def potong_html(dsoup: BeautifulSoup, hapus_setelah: str | None = None, hapus_mulai: str | None = None) -> str:
    """
    Ambil teks bersih dari halaman.
    - hapus_mulai  : buang semua teks SEBELUM marker ini (opsional)
    - hapus_setelah: buang semua teks SETELAH marker ini (opsional)
    """
    for tag in dsoup(['nav', 'header', 'footer', 'aside', 'script', 'style']):
        tag.decompose()
    teks = dsoup.get_text(separator='\n')
    if hapus_mulai and hapus_mulai in teks:
        teks = teks.split(hapus_mulai, 1)[0]        # buang dari marker ke bawah
    if hapus_setelah and hapus_setelah in teks:
        teks = teks.split(hapus_setelah, 1)[-1]     # ambil dari marker ke bawah
    return re.sub(r'\n\s*\n', '\n', teks).strip()


def is_mahasiswa(teks: str) -> bool:
    teks_lower = str(teks).lower()
    keywords = [
        'mahasiswa', 'mahasiswi', 'universitas', 'kampus',
        's1', 'd3', 'd4', 'umum', 'undergraduate', 'diploma',
        'student', 'university',
    ]
    reject_words = ['siswa', 'pelajar', 'smp', 'sd', 'mts', 'sekolah dasar', 'high schooler']
    if any(rw in teks_lower for rw in reject_words) and not any(kw in teks_lower for kw in keywords):
        return False
    return any(kw in teks_lower for kw in keywords)


# ---------------------------------------------------------------------------
# LLM PROCESSING
# Dipanggil SETELAH semua scraping selesai.
# Tugas LLM:
#   - Semua sumber  → ekstrak/perbaiki "judul" dan "timeline" dari caption
#   - Hanya IG      → ekstrak "link_pendaftaran" dari caption
# ---------------------------------------------------------------------------
def proses_batch_dengan_gemini(data_batch: list[dict]) -> list[dict]:
    """
    Kirim batch ke Gemini. Payload seminimal mungkin agar hemat token:
      - id          → kunci pencocok
      - sumber      → agar LLM tahu apakah perlu ekstrak link (IG) atau tidak
      - judul_kasar → hasil scrape mentah, LLM perbaiki
      - caption     → sumber kebenaran utama
    Output LLM per item: { id, judul, timeline, link_pendaftaran[] }
    link_pendaftaran hanya diisi LLM untuk item IG; item lain tetap pakai
    link_pendaftaran yang sudah di-scrape.
    """
    if not gemini_client or not data_batch:
        return data_batch

    # Payload seminimal mungkin.
    # - infolomba/silomba: kirim caption penuh (sudah bersih dari scraper)
    # - IG: kirim caption + judul_kasar sebagai petunjuk awal saja
    payload_llm = []
    for item in data_batch:
        entry: dict = {
            "id": item["id"],
            "sumber": item["sumber"],
            # Caption di-trim 2000 char — cukup untuk judul + timeline
            "caption": item["caption"][:2000],
        }
        # Untuk IG, sertakan judul_kasar sebagai konteks awal (LLM tetap harus cari dari caption)
        if "IG" in item["sumber"]:
            entry["judul_kasar"] = item["judul"]
        payload_llm.append(entry)

    prompt = f"""Kamu adalah AI Data Extractor untuk info lomba/kompetisi. Proses setiap item dalam JSON Array berikut.

TUGAS PER FIELD:

1. "judul"
   - Temukan nama resmi acara/kompetisi dari field "caption".
   - Hapus semua: emoji, simbol markdown (* _ # >), kata sapaan ("Hai!", "Halo sobat", "Telah dibuka", "Yuk daftar", dll).
   - Hasil akhir harus berupa nama lomba/kompetisi yang bersih dan singkat.
   - Untuk sumber IG: "judul_kasar" hanyalah petunjuk awal — WAJIB cari nama resmi dari "caption", jangan pakai judul_kasar mentah jika caption mengandung nama lebih lengkap/resmi.

2. "timeline"
   - Cari info jadwal dari "caption": tanggal pendaftaran, pelaksanaan, pengumuman, dll.
   - Hapus emoji dari hasil. Format bebas, pertahankan info tanggal aslinya.
   - Jika tidak ada informasi jadwal sama sekali, kembalikan "".
   - PENTING: Setiap item WAJIB dicari timelinenya dari captionnya MASING-MASING. Jangan copy timeline dari item lain.

3. "link_pendaftaran"
   - Jika "sumber" mengandung kata "IG": ekstrak SEMUA URL yang ada di "caption" → array of strings. Jika tidak ada, kembalikan [].
   - Jika "sumber" TIDAK mengandung "IG": kembalikan [] (link sudah di-scrape terpisah).

4. Jangan ubah nilai "id".
5. Output: HANYA JSON Array valid. Tanpa markdown code block, tanpa teks penjelasan apapun.

Format output setiap item (wajib ada semua field):
{{"id": "...", "judul": "...", "timeline": "...", "link_pendaftaran": []}}

Data yang harus diproses:
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
                item["judul"] = llm.get("judul") or item["judul"]
                item["timeline"] = llm.get("timeline", item.get("timeline", ""))

                # Gabungkan link: sumber IG → scrape (kosong) + LLM; lainnya → tetap scrape
                if "IG" in item["sumber"]:
                    llm_links = llm.get("link_pendaftaran", [])
                    item["link_pendaftaran"] = list(dict.fromkeys(llm_links))
                # non-IG: link_pendaftaran sudah di-set saat scraping, tidak diubah

            time.sleep(3)
            return data_batch

        except Exception as e:
            print(f"[LLM] Error attempt {attempt + 1}: {e}")
            time.sleep(15)

    print("[LLM] Semua retry gagal, mengembalikan data mentah.")
    return data_batch


# ---------------------------------------------------------------------------
# SCRAPER: infolomba.id
# Yang di-scrape: caption, poster, sumber, link_direct, link_pendaftaran
# LLM akan isi: judul (perbaiki), timeline
# ---------------------------------------------------------------------------
def _ambil_detail_infolomba(link: str, anchor, id_sudah_ada: set, base_url: str, scraper) -> dict | None:
    try:
        res = scraper.get(link, headers=HEADERS)
        if res.status_code != 200:
            return None

        dsoup = BeautifulSoup(res.text, 'html.parser')
        # Ambil dari awal halaman, buang footer mulai "Laporkan Lomba"
        # agar judul resmi & timeline di bagian atas halaman tetap masuk caption
        caption = potong_html(dsoup, hapus_mulai="Laporkan Lomba")

        if not is_mahasiswa(caption):
            return None

        # Judul kasar dari slug URL (LLM akan perbaiki)
        judul_kasar = (
            '-'.join(link.rstrip('/').split('/')[-1].replace('info-', '', 1).split('-')[:-1])
            .replace('-', ' ').title()
        )
        uid = buat_id(judul_kasar, "infolomba.id")
        if uid in id_sudah_ada:
            return None

        # --- Poster ---
        poster_url = (
            (anchor.find('img') or {}).get('src')
            or (anchor.find('img') or {}).get('data-src')
        )
        if not poster_url:
            wadah = anchor.find_parent('div')
            poster_url = (
                ((wadah.find_parent('div') or wadah).find('img') or {}).get('src')
                if wadah else ""
            )
        if poster_url and not poster_url.startswith('http'):
            poster_url = urljoin(base_url, poster_url)
        if not poster_url:
            poster_url = next(
                (urljoin(base_url, img.get('src', ''))
                 for img in dsoup.find_all('img')
                 if '/poster/' in (img.get('src') or '')),
                '',
            )

        # --- Link pendaftaran (scrape langsung, bukan LLM) ---
        btn = dsoup.find(lambda t: t.name == 'a' and t.text and 'Daftar Sekarang' in t.text)
        link_pendaftaran = (
            [btn['href']]
            if btn
            and btn.get('href')
            and btn['href'] not in ['#', '']
            and not btn['href'].startswith('javascript')
            else []
        )

        return {
            "id": uid,
            "sumber": "infolomba.id",
            "judul": judul_kasar,
            "poster": poster_url,
            "caption": caption,
            "link_pendaftaran": link_pendaftaran,
            "timeline": "",          # diisi LLM dari caption
            "link_direct": link,
        }
    except Exception:
        return None


def scrape_infolomba(id_sudah_ada: set) -> list[dict]:
    base_url = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()
    try:
        soup = BeautifulSoup(scraper.get(base_url, headers=HEADERS).text, 'html.parser')
        links_unik = list({
            urljoin(base_url, a.get('href', '')): a
            for a in soup.find_all('a', href=lambda h: h and 'info-' in h)
            if urljoin(base_url, a.get('href', '')).startswith(base_url + '/')
        }.items())[:15]

        with ThreadPoolExecutor(max_workers=5) as ex:
            hasil = list(filter(None, ex.map(
                lambda item: _ambil_detail_infolomba(item[0], item[1], id_sudah_ada, base_url, scraper),
                links_unik,
            )))
        return hasil
    except Exception:
        return []


# ---------------------------------------------------------------------------
# SCRAPER: silomba.id
# Yang di-scrape: caption, poster, sumber, link_direct, link_pendaftaran
# LLM akan isi: judul (perbaiki), timeline
# ---------------------------------------------------------------------------
async def _ambil_detail_silomba(card, browser, id_sudah_ada: set, base_url: str, semaphore) -> dict | None:
    async with semaphore:
        judul_kasar = (
            card.get('aria-label', '').replace('Lihat detail kompetisi ', '').strip()
            or (card.find(['h2', 'h3', 'h4']).text.strip() if card.find(['h2', 'h3', 'h4']) else "Tanpa judul")
        )
        uid = buat_id(judul_kasar, "silomba.id")
        if uid in id_sudah_ada:
            return None

        link_detail = urljoin(base_url, card['href'])
        page = await browser.new_page()
        try:
            await page.goto(link_detail, wait_until="domcontentloaded", timeout=30000)
            dsoup = BeautifulSoup(await page.content(), 'html.parser')
            # Ambil seluruh konten halaman, buang hanya footer mulai "Bagikan Lomba"
            # agar timeline yang ada di luar section deskripsi tetap masuk caption
            caption = potong_html(dsoup, hapus_mulai="Bagikan Lomba")

            if not is_mahasiswa(caption):
                return None

            # --- Poster ---
            poster = (
                dsoup.find('img', src=lambda s: s and 'original-poster' in s)
                or dsoup.find('img', src=lambda s: s and 'storage2.silomba.id' in s)
                or {}
            ).get('src', '')

            # --- Link pendaftaran (scrape langsung) ---
            link_pendaftaran = list(set(
                btn.get('href')
                for btn in dsoup.find_all(
                    lambda t: t.name == 'a' and t.text
                    and any(x in t.text for x in ['Daftar', 'Website Resmi', 'Register'])
                )
                if btn.get('href') and btn.get('href') != '#' and not btn.get('href').startswith('javascript')
            ))

            return {
                "id": uid,
                "sumber": "silomba.id",
                "judul": judul_kasar,
                "poster": poster,
                "caption": caption,
                "link_pendaftaran": link_pendaftaran,
                "timeline": "",          # diisi LLM dari caption
                "link_direct": link_detail,
            }
        except Exception:
            return None
        finally:
            await page.close()


async def scrape_silomba(id_sudah_ada: set) -> list[dict]:
    base_url = "https://silomba.id"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector("#competition-section", timeout=15000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            soup = BeautifulSoup(await page.content(), 'html.parser')
            await page.close()

            section = soup.find(id='competition-section')
            cards = section.find_all('a', href=lambda h: h and h.startswith('/lomba/')) if section else []

            sem = asyncio.Semaphore(4)
            tasks = [_ambil_detail_silomba(card, browser, id_sudah_ada, base_url, sem) for card in cards]
            return list(filter(None, await asyncio.gather(*tasks)))
        except Exception:
            return []
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# SCRAPER: Instagram
# Yang di-scrape: caption, poster, sumber, link_direct
# link_pendaftaran → KOSONG dulu, LLM yang ekstrak dari caption
# LLM akan isi: judul, timeline, link_pendaftaran
# ---------------------------------------------------------------------------
def scrape_instagram(id_sudah_ada: set) -> list[dict]:
    if not IG_SESSION_ID:
        return []

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

                    try:
                        caption = driver.find_element(By.XPATH, '//h1').text
                    except Exception:
                        caption = (
                            driver.find_element(
                                By.XPATH, '//meta[@property="og:description"]'
                            ).get_attribute('content')
                            .split(": ", 1)[-1]
                            .strip('"')
                        )

                    if not caption or not is_mahasiswa(caption):
                        continue

                    # Judul kasar dari baris pertama caption; LLM akan perbaiki
                    judul_kasar = caption.split('\n')[0][:100].strip() or str(time.time())
                    uid = buat_id(judul_kasar, f"IG @{akun}")
                    if uid in id_sudah_ada:
                        continue

                    poster = driver.execute_script(
                        "var i=document.querySelectorAll('img');"
                        "for(var j=0;j<i.length;j++){"
                        "  var s=i[j].src||'';"
                        "  if(!(i[j].alt||'').toLowerCase().includes('profile')"
                        "  &&!s.includes('150x150')"
                        "  &&(s.includes('scontent')||s.includes('cdninstagram'))){"
                        "    return i[j].srcset"
                        "      ?i[j].srcset.split(',').pop().trim().split(' ')[0]:s;"
                        "  }"
                        "} return '';"
                    ) or driver.find_element(
                        By.XPATH, '//meta[@property="og:image"]'
                    ).get_attribute('content')

                    # link_pendaftaran sengaja KOSONG — LLM yang ekstrak dari caption
                    hasil.append({
                        "id": uid,
                        "sumber": f"IG @{akun}",
                        "judul": judul_kasar,       # LLM perbaiki
                        "poster": poster,
                        "caption": caption,
                        "link_pendaftaran": [],     # diisi LLM
                        "timeline": "",             # diisi LLM
                        "link_direct": url,
                    })
                    id_sudah_ada.add(uid)

                except Exception:
                    pass

    finally:
        driver.quit()

    return hasil


# ---------------------------------------------------------------------------
# MAIN
# Alur: scrape semua → kumpulkan raw JSON → kirim ke LLM batch → simpan DB
# ---------------------------------------------------------------------------
async def main():
    print("[INFO] Memulai proses scraping...")
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

    hasil_mentah: list[dict] = [item for batch in results if isinstance(batch, list) for item in batch]
    print(f"[INFO] Total {len(hasil_mentah)} data mentah baru ditemukan.")

    if not hasil_mentah:
        print("[INFO] Tidak ada data baru.")
        client.close()
        return

    # === FASE 2: LLM PROCESSING (setelah semua scraping selesai) ===
    # Kirim ke LLM dalam batch agar hemat token & request
    BATCH_SIZE = 15
    hasil_final: list[dict] = []
    for i in range(0, len(hasil_mentah), BATCH_SIZE):
        batch = hasil_mentah[i:i + BATCH_SIZE]
        print(f"[LLM] Memproses batch {i // BATCH_SIZE + 1} ({len(batch)} item)...")
        hasil_final.extend(proses_batch_dengan_gemini(batch))

    # === FASE 3: SIMPAN KE DATABASE ===
    collection.bulk_write([
        UpdateOne({'id': item['id']}, {'$set': item}, upsert=True)
        for item in hasil_final
    ])
    print(f"[INFO] Database diperbarui dengan {len(hasil_final)} data.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
