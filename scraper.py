import os
import time
import random
import asyncio
import re
import hashlib
import json
import threading
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

IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
MONGO_URI = os.environ.get("MONGO_URI") 
DB_NAME = "competition_scraper"
COLLECTION_NAME = "competition"

try:
    gemini_client = genai.Client()
except Exception as e:
    print(f"[WARN] Gagal inisialisasi Gemini Client: {e}")
    gemini_client = None

# --- SISTEM ANTREAN GEMINI (GLOBAL LOCK) ---
gemini_lock = threading.Lock()
last_gemini_call = 0.0

def proses_item_dengan_gemini(sumber, teks_mentah, links_mentah, poster_url, judul_mentah):
    global last_gemini_call
    
    fallback_dict = {
        "sumber": sumber,
        "judul": judul_mentah,
        "link_pendaftaran": links_mentah,
        "poster": poster_url,
        "caption": teks_mentah[:800] + "..." if len(teks_mentah) > 800 else teks_mentah,
        "timeline": ""
    }

    if not gemini_client:
        return fallback_dict

    prompt = f"""Tugasmu adalah menganalisis teks informasi lomba dan merapikannya ke dalam format JSON.
1. "sumber": Gunakan nilai "{sumber}".
2. "judul": EKSTRAK judul acara/kompetisi utama langsung dari "Teks Asli". Abaikan "Judul Mentah" jika isinya "Tanpa Judul" atau tidak relevan. Buat judul yang bersih dan profesional.
3. "link_pendaftaran": Pastikan ini berupa array berisi string URL valid (gabungkan dari "Link Pendaftaran Mentah" dan URL pendaftaran yang mungkin tersembunyi di "Teks Asli").
4. "poster": Gunakan URL "{poster_url}".
5. "caption": Buat caption yang sudah diperindah dari "Teks Asli" (ABAIKAN teks menu navigasi website seperti Home, Tips, Bookmark, dsb jika masih ada). Rapikan paragraf, perbaiki typo, dan buang hashtag spam.
6. "timeline": Ekstrak tanggal/timeline pendaftaran atau pelaksanaan lomba dari "Teks Asli".

Keluarkan HANYA dalam format JSON dengan struktur yang persis seperti ini:
{{
    "sumber": "string",
    "judul": "string",
    "link_pendaftaran": ["string"],
    "poster": "string",
    "caption": "string",
    "timeline": "string"
}}

Data Mentah:
Judul Mentah: {judul_mentah}
Link Pendaftaran Mentah: {links_mentah}
Teks Asli: {teks_mentah}"""

    with gemini_lock:
        waktu_sekarang = time.time()
        jeda_berlalu = waktu_sekarang - last_gemini_call
        
        if jeda_berlalu < 4.2:
            time.sleep(4.2 - jeda_berlalu)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = gemini_client.models.generate_content(
                    model="gemini-2.0-flash", 
                    contents=prompt,
                    config={"response_mime_type": "application/json"}
                )
                
                hasil_llm = json.loads(response.text)
                last_gemini_call = time.time()
                
                for key in fallback_dict.keys():
                    if key not in hasil_llm:
                        hasil_llm[key] = fallback_dict[key]
                        
                return hasil_llm

            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "quota" in error_str or "rate limit" in error_str:
                    wait_time = 15.0
                    print(f"  [WAIT] Gemini Limit. Istirahat {wait_time} detik...")
                    time.sleep(wait_time)
                else:
                    print(f"  [WARN] Error Gemini: {e}")
                    last_gemini_call = time.time() 
                    return fallback_dict

        print("  [WARN] Gagal memanggil Gemini setelah 3 kali mencoba.")
        last_gemini_call = time.time()
        return fallback_dict


def is_untuk_mahasiswa(teks):
    teks_lower = str(teks).lower()
    keywords = ['mahasiswa', 'mahasiswi', 'universitas', 'kampus', 's1', 'd3', 'd4', 'umum', 'undergraduate', 'diploma', 'student', 'university']
    return any(kw in teks_lower for kw in keywords)

def buat_id_unik(judul, sumber):
    raw = f"{sumber}_{judul}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def get_existing_ids_from_db(collection):
    data_lama = collection.find({}, {"id": 1, "_id": 0})
    return set(item['id'] for item in data_lama if 'id' in item)

def ekstrak_judul_dari_caption(caption):
    baris_semua = [b.strip() for b in caption.split('\n') if len(b.strip()) > 5]
    if not baris_semua: return "Tanpa Judul"
    for baris in baris_semua[:5]:
        match = re.search(r'\[(.*?)\]', baris)
        if match and len(match.group(1).strip()) > 5: return match.group(1).strip()
    keywords = ['kompetisi', 'competition', 'lomba', 'fest', 'championship', 'olympiad', 'olimpiade', 'hackathon', 'call for', 'nasional', 'international']
    gimmicks = ['calling out', 'hello', 'halo', 'are you ready', 'siapkan', 'kabar gembira']
    for baris in baris_semua[:5]:
        if any(g in baris.lower() for g in gimmicks): continue
        if any(kw in baris.lower() for kw in keywords): return baris[:150]
    for baris in baris_semua[:3]:
        if not any(g in baris.lower() for g in gimmicks): return baris[:150]
    return baris_semua[0][:150]

def ekstrak_link_pendaftaran(caption):
    pola = r'(https?://[^\s]+|bit\.ly/[^\s]+|linktr\.ee/[^\s]+|forms\.gle/[^\s]+|s\.id/[^\s]+)'
    links = re.findall(pola, caption)
    if links: return [re.sub(r'[).,!]+$', '', l) for l in links]
    if "bio" in caption.lower(): return ["Link Belum Tersedia"]
    return []

# --- PEMBERSIH TEKS HTML (SANDWICH METHOD) ---
def bersihkan_teks_web(dsoup, pemisah_atas, pemisah_bawah):
    # Hapus tag sampah secara brutal
    for tag in dsoup(['nav', 'header', 'footer', 'aside', 'script', 'style']):
        tag.decompose()
    
    # Ambil teks sisa
    teks_kotor = dsoup.get_text(separator='\n')
    
    # Potong bagian tengahnya saja (Sandwich method)
    if pemisah_atas in teks_kotor and pemisah_bawah in teks_kotor:
        teks_bersih = teks_kotor.split(pemisah_atas)[-1].split(pemisah_bawah)[0]
    elif pemisah_atas in teks_kotor:
        teks_bersih = teks_kotor.split(pemisah_atas)[-1]
    else:
        teks_bersih = teks_kotor
        
    # Rapikan enter berlebih
    return re.sub(r'\n\s*\n', '\n', teks_bersih).strip()


def scrape_infolomba(id_sudah_ada):
    print("[INFO] Mulai infolomba.id...")
    base_url = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()
    hasil = []
    try:
        soup = BeautifulSoup(scraper.get(base_url, headers=HEADERS).text, 'html.parser')
        links_unik = list({urljoin(base_url, a.get('href', '')): a for a in soup.find_all('a', href=lambda h: h and 'info-' in h) if urljoin(base_url, a.get('href', '')).startswith(base_url + '/')}.items())

        for link, a in links_unik[:15]:
            try:
                res = scraper.get(link, headers=HEADERS)
                if res.status_code != 200: continue
                dsoup = BeautifulSoup(res.text, 'html.parser')
                
                # Gunakan pembersih teks khusus Infolomba
                teks_konten = bersihkan_teks_web(dsoup, "Daftar Sekarang", "Laporkan Lomba")
                
                if not is_untuk_mahasiswa(teks_konten): continue

                judul_mentah = '-'.join(link.rstrip('/').split('/')[-1].replace('info-', '', 1).split('-')[:-1]).replace('-', ' ').title()
                uid = buat_id_unik(judul_mentah, "infolomba.id")
                
                if uid in id_sudah_ada: continue

                poster_url = (a.find('img') or {}).get('src') or (a.find('img') or {}).get('data-src')
                if not poster_url:
                    wadah = a.find_parent('div')
                    img_card = (wadah.find_parent('div') or wadah).find('img') if wadah else None
                    poster_url = (img_card or {}).get('src') or (img_card or {}).get('data-src', '')
                if poster_url and not poster_url.startswith('http'): poster_url = urljoin(base_url, poster_url)
                if not poster_url:
                    poster_url = next((urljoin(base_url, img.get('src') or img.get('data-src', '')) for img in dsoup.find_all('img') if '/poster/' in (img.get('src') or img.get('data-src', ''))), '')

                # Refresh parser untuk cari button pendaftaran
                dsoup_ori = BeautifulSoup(res.text, 'html.parser') 
                btn = dsoup_ori.find(lambda t: t.name == 'a' and t.text and 'Daftar Sekarang' in t.text)
                link_pendaftaran = [btn['href']] if btn and btn.get('href') and btn['href'] not in ['#', ''] and not btn['href'].startswith('javascript') else []

                item_llm = proses_item_dengan_gemini("infolomba.id", teks_konten, link_pendaftaran, poster_url, judul_mentah)

                hasil.append({
                    "id": uid, 
                    "sumber": item_llm.get("sumber", "infolomba.id"), 
                    "judul": item_llm.get("judul", judul_mentah), 
                    "poster": item_llm.get("poster", poster_url), 
                    "caption": item_llm.get("caption", "Deskripsi tidak ditemukan"), 
                    "link_pendaftaran": item_llm.get("link_pendaftaran", link_pendaftaran), 
                    "timeline": item_llm.get("timeline", ""),
                    "link_direct": link
                })
                id_sudah_ada.add(uid)
                print(f"  [OK] {item_llm.get('judul', '')[:50]}")
            except Exception: pass
    except Exception as e: print(f"[ERROR] Error Infolomba: {e}")
    print(f"[INFO] Selesai infolomba.id: {len(hasil)} data baru")
    return hasil

async def scrape_silomba(id_sudah_ada):
    print("[INFO] Mulai silomba.id...")
    base_url = "https://silomba.id"
    hasil = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector("#competition-section", timeout=15000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            soup = BeautifulSoup(await page.content(), 'html.parser')
            section = soup.find(id='competition-section')
            if not section: return hasil

            for card in section.find_all('a', href=lambda h: h and h.startswith('/lomba/')):
                judul_mentah = card.get('aria-label', '').replace('Lihat detail kompetisi ', '').strip() or (card.find(['h2', 'h3', 'h4']).text.strip() if card.find(['h2', 'h3', 'h4']) else "Tanpa judul")
                uid = buat_id_unik(judul_mentah, "silomba.id")
                
                if uid in id_sudah_ada: continue

                link_detail = urljoin(base_url, card['href'])
                poster, link_pendaftaran = '', []
                try:
                    dp = await browser.new_page()
                    await dp.goto(link_detail, wait_until="domcontentloaded", timeout=45000)
                    dsoup = BeautifulSoup(await dp.content(), 'html.parser')
                    await dp.close()
                    
                    # Gunakan pembersih teks khusus Silomba
                    teks_konten = bersihkan_teks_web(dsoup, "Deskripsi Lomba", "Persyaratan Pendaftaran")
                    if not is_untuk_mahasiswa(teks_konten): continue

                    # Refresh parser untuk cari elemen asli
                    dsoup_ori = BeautifulSoup(await dp.content(), 'html.parser')
                    poster = (dsoup_ori.find('img', src=lambda s: s and 'original-poster' in s) or dsoup_ori.find('img', src=lambda s: s and 'storage2.silomba.id' in s) or {}).get('src', '')
                    link_pendaftaran = list(set([btn.get('href') for btn in dsoup_ori.find_all(lambda t: t.name == 'a' and t.text and any(x in t.text for x in ['Daftar', 'Website Resmi', 'Register'])) if btn.get('href') and btn.get('href') != '#' and not btn.get('href').startswith('javascript')]))
                    
                    item_llm = await asyncio.to_thread(proses_item_dengan_gemini, "silomba.id", teks_konten, link_pendaftaran, poster, judul_mentah)

                    hasil.append({
                        "id": uid, 
                        "sumber": item_llm.get("sumber", "silomba.id"), 
                        "judul": item_llm.get("judul", judul_mentah), 
                        "poster": item_llm.get("poster", poster), 
                        "caption": item_llm.get("caption", "Deskripsi tidak ditemukan"), 
                        "link_pendaftaran": item_llm.get("link_pendaftaran", link_pendaftaran), 
                        "timeline": item_llm.get("timeline", ""),
                        "link_direct": link_detail
                    })
                    id_sudah_ada.add(uid)
                    print(f"  [OK] {item_llm.get('judul', '')[:50]}")
                except Exception: pass
        except Exception as e: print(f"[ERROR] Error Silomba: {e}")
        finally: await browser.close()
    print(f"[INFO] Selesai silomba.id: {len(hasil)} data baru")
    return hasil

def scrape_instagram(id_sudah_ada):
    if not IG_SESSION_ID:
        print("[WARN] IG_SESSION_ID tidak diset")
        return []
    print("[INFO] Mulai Instagram...")
    hasil = []
    opts = Options()
    for arg in ["--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", f"user-agent={HEADERS['User-Agent']}"]: opts.add_argument(arg)
    opts.binary_location = "/opt/chrome/chrome"
    driver = webdriver.Chrome(service=Service(executable_path="/usr/bin/chromedriver"), options=opts)

    try:
        driver.get("https://www.instagram.com/")
        time.sleep(3)
        driver.add_cookie({'name': 'sessionid', 'value': IG_SESSION_ID, 'domain': '.instagram.com'})
        driver.refresh()
        time.sleep(5)
        if "login" in driver.current_url: return []

        for akun in ['infolomba', 'infolomba_gratis', 'infolomba.olimpiade']:
            url_posts = []
            driver.get(f"https://www.instagram.com/{akun}/")
            time.sleep(random.randint(4, 6))
            if "Page Not Found" in driver.title: continue

            last_h = driver.execute_script("return document.body.scrollHeight")
            for _ in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(random.randint(2, 4))
                url_posts.extend([l.get_attribute('href') for l in driver.find_elements(By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]') if l.get_attribute('href')])
                new_h = driver.execute_script("return document.body.scrollHeight")
                if new_h == last_h: break
                last_h = new_h

            for url in list(dict.fromkeys(url_posts))[:6]:
                try:
                    driver.get(url)
                    try: WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "img")))
                    except: pass
                    time.sleep(random.randint(3, 5))

                    caption = ""
                    try: caption = driver.find_element(By.XPATH, '//h1').text
                    except:
                        try:
                            m = driver.find_element(By.XPATH, '//meta[@property="og:description"]').get_attribute('content')
                            caption = m.split(": ", 1)[1].strip('"') if ": " in m else m
                        except: caption = driver.title

                    if not caption or not is_untuk_mahasiswa(caption): continue

                    judul_mentah = ekstrak_judul_dari_caption(caption)
                    uid = buat_id_unik(judul_mentah, f"IG @{akun}")
                    
                    if uid in id_sudah_ada: continue

                    poster = driver.execute_script("""
                        var imgs = document.querySelectorAll('img');
                        for (var i = 0; i < imgs.length; i++) {
                            var src = imgs[i].src || '';
                            if ((imgs[i].alt || '').toLowerCase().includes('profile') || src.includes('150x150')) continue;
                            if (src.includes('scontent') || src.includes('cdninstagram')) {
                                var s = imgs[i].srcset; return s ? s.split(',').pop().trim().split(' ')[0] : src;
                            }
                        } return '';
                    """) or (driver.find_element(By.XPATH, '//meta[@property="og:image"]').get_attribute('content') if driver.find_elements(By.XPATH, '//meta[@property="og:image"]') else "")

                    links_mentah = ekstrak_link_pendaftaran(caption)

                    item_llm = proses_item_dengan_gemini(f"IG @{akun}", caption, links_mentah, poster, judul_mentah)

                    hasil.append({
                        "id": uid, 
                        "sumber": item_llm.get("sumber", f"IG @{akun}"), 
                        "judul": item_llm.get("judul", judul_mentah), 
                        "poster": item_llm.get("poster", poster), 
                        "caption": item_llm.get("caption", caption[:500] + "..."), 
                        "link_pendaftaran": item_llm.get("link_pendaftaran", links_mentah), 
                        "timeline": item_llm.get("timeline", ""),
                        "link_direct": url
                    })
                    id_sudah_ada.add(uid)
                    print(f"  [OK] {item_llm.get('judul', '')[:50]}")
                except Exception: pass
    except Exception as e: print(f"[ERROR] Error IG: {e}")
    finally: driver.quit()
    print(f"[INFO] Selesai Instagram: {len(hasil)} data baru")
    return hasil

async def main():
    print("[INFO] Menghubungkan ke MongoDB...")
    client = pymongo.MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION_NAME]
    
    id_sudah_ada = get_existing_ids_from_db(collection)

    results = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, id_sudah_ada),
        scrape_silomba(id_sudah_ada),
        asyncio.to_thread(scrape_instagram, id_sudah_ada)
    )

    hasil_baru = [item for res in results if isinstance(res, list) for item in res]
    print(f"\n[INFO] Mendapatkan total {len(hasil_baru)} data baru.")

    if hasil_baru:
        result = collection.bulk_write([UpdateOne({'id': item['id']}, {'$set': item}, upsert=True) for item in hasil_baru])
        print(f"[INFO] Disimpan ke MongoDB: {result.upserted_count} baru, {result.modified_count} update.")
    else:
        print("[INFO] Tidak ada data baru untuk disimpan.")

    client.close()

if __name__ == "__main__":
    asyncio.run(main())
