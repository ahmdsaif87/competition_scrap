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

IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
MONGO_URI = os.environ.get("MONGO_URI") 
DB_NAME = "competition_scraper"
COLLECTION_NAME = "competition"

try:
    gemini_client = genai.Client()
except Exception:
    gemini_client = None

def proses_batch_dengan_gemini(data_batch):
    if not gemini_client or not data_batch:
        return data_batch

    payload_llm = []
    for i in data_batch:
        payload_llm.append({
            "id": i["id"],
            "sumber": i["sumber"],
            "judul_mentah": i["judul"],
            "timeline_mentah": i["timeline"],
            "link_web": i["link_pendaftaran"],
            "teks_info": i["caption"][:1200]
        })

    prompt = f"""Kamu adalah AI Data Extractor. Ekstrak dan perbaiki data dari JSON Array berikut.
ATURAN WAJIB:
1. "judul": Temukan nama resmi acara/kompetisi dari "teks_info". HAPUS SEMUA emoji, simbol markdown (*, _), dan kalimat sapaan (seperti "Hi pelajar!", "Telah dibuka"). 
2. "timeline": Cari jadwal (Pendaftaran/Pelaksanaan) dari "teks_info" atau gunakan "timeline_mentah". Hapus emoji. Jika tidak ada, isi "".
3. "link_pendaftaran": Gabungkan URL dari "link_web" dan SEMUA URL pendaftaran di "teks_info". Pastikan output berupa array of strings.
4. JANGAN ubah "id".
5. Keluarkan HANYA JSON Array valid tanpa blok kode markdown.

Data Mentah:
{json.dumps(payload_llm, ensure_ascii=False)}"""

    for _ in range(3):
        try:
            res = gemini_client.models.generate_content(
                model="gemini-2.0-flash", 
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            
            hasil_llm = json.loads(res.text)
            llm_dict = {str(item.get("id")): item for item in hasil_llm}
            
            for item in data_batch:
                llm_data = llm_dict.get(str(item["id"]), {})
                item["judul"] = llm_data.get("judul", item["judul"])
                item["timeline"] = llm_data.get("timeline", item["timeline"])
                gabungan_link = item["link_pendaftaran"] + llm_data.get("link_pendaftaran", [])
                item["link_pendaftaran"] = list(dict.fromkeys(gabungan_link))
                
            time.sleep(3)
            return data_batch
        except Exception:
            time.sleep(15.0)

    return data_batch

def is_mahasiswa(teks):
    teks_lower = str(teks).lower()
    keywords = ['mahasiswa', 'mahasiswi', 'universitas', 'kampus', 's1', 'd3', 'd4', 'umum', 'undergraduate', 'diploma', 'student', 'university']
    reject_words = ['siswa', 'pelajar', 'smp', 'sd', 'mts', 'sekolah dasar', 'high schooler']
    
    if any(rw in teks_lower for rw in reject_words) and not any(kw in teks_lower for kw in keywords):
        return False
    return any(kw in teks_lower for kw in keywords)

def buat_id(judul, sumber):
    return hashlib.md5(f"{sumber}_{judul}".lower().strip().encode()).hexdigest()[:12]

def ekstrak_link(caption):
    links = re.findall(r'(https?://[^\s]+|www\.[^\s]+|bit\.ly/[^\s]+|linktr\.ee/[^\s]+|forms\.gle/[^\s]+|s\.id/[^\s]+)', caption)
    return [re.sub(r'[).,!]+$', '', l) for l in links] if links else []

def potong_html(dsoup, top, bot):
    for tag in dsoup(['nav', 'header', 'footer', 'aside', 'script', 'style']): tag.decompose()
    teks = dsoup.get_text(separator='\n')
    if top in teks and bot in teks: teks = teks.split(top)[-1].split(bot)[0]
    elif top in teks: teks = teks.split(top)[-1]
    return re.sub(r'\n\s*\n', '\n', teks).strip()

def ambil_detail_infolomba(link, a, id_sudah_ada, base_url, scraper):
    try:
        res = scraper.get(link, headers=HEADERS)
        if res.status_code != 200: return None
        dsoup = BeautifulSoup(res.text, 'html.parser')
        teks_konten = potong_html(dsoup, "Daftar Sekarang", "Laporkan Lomba")
        if not is_mahasiswa(teks_konten): return None

        judul_sementara = '-'.join(link.rstrip('/').split('/')[-1].replace('info-', '', 1).split('-')[:-1]).replace('-', ' ').title()
        uid = buat_id(judul_sementara, "infolomba.id")
        if uid in id_sudah_ada: return None

        poster_url = (a.find('img') or {}).get('src') or (a.find('img') or {}).get('data-src')
        if not poster_url:
            wadah = a.find_parent('div')
            poster_url = ((wadah.find_parent('div') or wadah).find('img') or {}).get('src') if wadah else ""
        if poster_url and not poster_url.startswith('http'): poster_url = urljoin(base_url, poster_url)
        if not poster_url: poster_url = next((urljoin(base_url, img.get('src') or '') for img in dsoup.find_all('img') if '/poster/' in (img.get('src') or '')), '')

        dsoup_ori = BeautifulSoup(res.text, 'html.parser') 
        btn = dsoup_ori.find(lambda t: t.name == 'a' and t.text and 'Daftar Sekarang' in t.text)
        link_pendaftaran = [btn['href']] if btn and btn.get('href') and btn['href'] not in ['#', ''] and not btn['href'].startswith('javascript') else []

        timeline = next((li.get_text(strip=True).replace('📅', '').replace('🗓', '').strip() for li in dsoup_ori.find_all(['li', 'div', 'p']) if ('📅' in li.text or '🗓' in li.text or re.search(r'\d{1,2}\s+[A-Za-z]+\s*-\s*\d{1,2}\s+[A-Za-z]+', li.text)) and len(li.get_text(strip=True)) < 50 and any(c.isdigit() for c in li.get_text(strip=True))), "")

        return {"id": uid, "sumber": "infolomba.id", "judul": judul_sementara, "poster": poster_url, "caption": teks_konten, "link_pendaftaran": link_pendaftaran, "timeline": timeline, "link_direct": link}
    except Exception: return None

def scrape_infolomba(id_sudah_ada):
    base_url = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()
    try:
        soup = BeautifulSoup(scraper.get(base_url, headers=HEADERS).text, 'html.parser')
        links_unik = list({urljoin(base_url, a.get('href', '')): a for a in soup.find_all('a', href=lambda h: h and 'info-' in h) if urljoin(base_url, a.get('href', '')).startswith(base_url + '/')}.items())[:15]
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            hasil = list(filter(None, executor.map(lambda item: ambil_detail_infolomba(item[0], item[1], id_sudah_ada, base_url, scraper), links_unik)))
        return hasil
    except Exception: return []

async def ambil_detail_silomba(card, browser, id_sudah_ada, base_url, semaphore):
    async with semaphore:
        judul_sementara = card.get('aria-label', '').replace('Lihat detail kompetisi ', '').strip() or (card.find(['h2', 'h3', 'h4']).text.strip() if card.find(['h2', 'h3', 'h4']) else "Tanpa judul")
        uid = buat_id(judul_sementara, "silomba.id")
        if uid in id_sudah_ada: return None

        link_detail = urljoin(base_url, card['href'])
        dp = await browser.new_page()
        try:
            await dp.goto(link_detail, wait_until="domcontentloaded", timeout=30000)
            dsoup_ori = BeautifulSoup(await dp.content(), 'html.parser')
            teks_konten = potong_html(dsoup_ori, "Deskripsi Lomba", "Persyaratan Pendaftaran")
            if not is_mahasiswa(teks_konten): return None

            poster = (dsoup_ori.find('img', src=lambda s: s and 'original-poster' in s) or dsoup_ori.find('img', src=lambda s: s and 'storage2.silomba.id' in s) or {}).get('src', '')
            link_pendaftaran = list(set([btn.get('href') for btn in dsoup_ori.find_all(lambda t: t.name == 'a' and t.text and any(x in t.text for x in ['Daftar', 'Website Resmi', 'Register'])) if btn.get('href') and btn.get('href') != '#' and not btn.get('href').startswith('javascript')]))
            timeline = next((p.get_text(separator=" ").strip() for p in dsoup_ori.find_all(['p', 'span', 'li']) if any(x in p.text for x in ['Batas Pengumpulan', 'Pendaftaran', 'Pelaksanaan']) and re.search(r'\d{1,2}\s+[A-Za-z]+\s+\d{4}', p.text)), "")
            
            return {"id": uid, "sumber": "silomba.id", "judul": judul_sementara, "poster": poster, "caption": teks_konten, "link_pendaftaran": link_pendaftaran, "timeline": timeline, "link_direct": link_detail}
        except Exception: return None
        finally: await dp.close()

async def scrape_silomba(id_sudah_ada):
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
            
            cards = soup.find(id='competition-section').find_all('a', href=lambda h: h and h.startswith('/lomba/')) if soup.find(id='competition-section') else []
            
            sem = asyncio.Semaphore(4)
            tasks = [ambil_detail_silomba(card, browser, id_sudah_ada, base_url, sem) for card in cards]
            return list(filter(None, await asyncio.gather(*tasks)))
        except Exception: return []
        finally: await browser.close()

def scrape_instagram(id_sudah_ada):
    if not IG_SESSION_ID: return []
    hasil, opts = [], Options()
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
            driver.get(f"https://www.instagram.com/{akun}/")
            time.sleep(random.randint(4, 6))
            if "Page Not Found" in driver.title: continue

            last_h = driver.execute_script("return document.body.scrollHeight")
            url_posts = []
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

                    try: caption = driver.find_element(By.XPATH, '//h1').text
                    except: caption = driver.find_element(By.XPATH, '//meta[@property="og:description"]').get_attribute('content').split(": ", 1)[-1].strip('"')

                    if not caption or not is_mahasiswa(caption): continue

                    judul_sementara = caption.split('\n')[0][:100].strip()
                    if not judul_sementara: judul_sementara = str(time.time())
                    
                    uid = buat_id(judul_sementara, f"IG @{akun}")
                    if uid in id_sudah_ada: continue

                    poster = driver.execute_script("var i=document.querySelectorAll('img');for(var j=0;j<i.length;j++){var s=i[j].src||'';if(!(i[j].alt||'').toLowerCase().includes('profile')&&!s.includes('150x150')&&(s.includes('scontent')||s.includes('cdninstagram'))){return i[j].srcset?i[j].srcset.split(',').pop().trim().split(' ')[0]:s;}}return '';") or driver.find_element(By.XPATH, '//meta[@property="og:image"]').get_attribute('content')
                    
                    link_awal = ekstrak_link(caption)

                    hasil.append({"id": uid, "sumber": f"IG @{akun}", "judul": judul_sementara, "poster": poster, "caption": caption, "link_pendaftaran": link_awal, "timeline": "", "link_direct": url})
                    id_sudah_ada.add(uid)
                except Exception: pass
    finally: driver.quit()
    return hasil

async def main():
    print("[INFO] Memulai proses scraping...")
    client = pymongo.MongoClient(MONGO_URI)
    collection = client[DB_NAME][COLLECTION_NAME]
    id_sudah_ada = set(i['id'] for i in collection.find({}, {"id": 1, "_id": 0}) if 'id' in i)

    results = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, id_sudah_ada),
        scrape_silomba(id_sudah_ada),
        asyncio.to_thread(scrape_instagram, id_sudah_ada)
    )

    hasil_baru = [i for r in results if isinstance(r, list) for i in r]
    print(f"[INFO] Total {len(hasil_baru)} data mentah baru ditemukan.")

    if hasil_baru:
        hasil_final = []
        for i in range(0, len(hasil_baru), 15):
            print(f"[INFO] Memproses batch LLM {i//15 + 1}...")
            hasil_final.extend(proses_batch_dengan_gemini(hasil_baru[i:i + 15]))
            
        collection.bulk_write([UpdateOne({'id': i['id']}, {'$set': i}, upsert=True) for i in hasil_final])
        print(f"[INFO] Database diperbarui dengan {len(hasil_final)} data.")
    else:
        print("[INFO] Tidak ada data baru.")

    client.close()

if __name__ == "__main__":
    asyncio.run(main())
