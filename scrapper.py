import os
import json
import time
import random
import asyncio
import re
import hashlib
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

# ─── KONFIGURASI ───────────────────────────────
IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
OUTPUT_FILE = "api_collabfinder.json"

# ─── UTILS ─────────────────────────────────────
def is_untuk_mahasiswa(teks):
    teks_lower = str(teks).lower()
    keywords = ['mahasiswa', 'mahasiswi', 'universitas', 'kampus', 's1', 'd3', 'd4',
                'umum', 'undergraduate', 'diploma', 'student', 'university']
    return any(kw in teks_lower for kw in keywords)

def buat_id_unik(judul, sumber):
    """Buat hash unik dari judul+sumber untuk deteksi duplikat"""
    raw = f"{sumber}_{judul}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:12]

def load_data_lama():
    """Load data yang sudah ada untuk cek duplikat"""
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def ekstrak_judul_dari_caption(caption):
    baris_semua = [b.strip() for b in caption.split('\n') if len(b.strip()) > 5]
    if not baris_semua:
        return "Tanpa Judul"
    for baris in baris_semua[:5]:
        match = re.search(r'\[(.*?)\]', baris)
        if match and len(match.group(1).strip()) > 5:
            return match.group(1).strip()
    keywords = ['kompetisi', 'competition', 'lomba', 'fest', 'championship',
                'olympiad', 'olimpiade', 'hackathon', 'call for', 'nasional', 'international']
    gimmicks = ['calling out', 'hello', 'halo', 'are you ready', 'siapkan', 'kabar gembira']
    for baris in baris_semua[:5]:
        if any(g in baris.lower() for g in gimmicks):
            continue
        if any(kw in baris.lower() for kw in keywords):
            return baris[:150]
    for baris in baris_semua[:3]:
        if not any(g in baris.lower() for g in gimmicks):
            return baris[:150]
    return baris_semua[0][:150]

def ekstrak_link_pendaftaran(caption):
    pola = r'(https?://[^\s]+|bit\.ly/[^\s]+|linktr\.ee/[^\s]+|forms\.gle/[^\s]+|s\.id/[^\s]+)'
    links = re.findall(pola, caption)
    if links:
        return [re.sub(r'[).,!]+$', '', l) for l in links]
    if "bio" in caption.lower():
        return ["Link Belum Tersedia"]
    return []


# ─── 1. INFOLOMBA.ID ───────────────────────────
def scrape_infolomba(id_sudah_ada):
    print(" infolomba.id...")
    base_url = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()
    hasil = []

    try:
        req = scraper.get(base_url, headers=HEADERS)
        soup = BeautifulSoup(req.text, 'html.parser')

        seen = set()
        links_unik = []
        for a in soup.find_all('a', href=lambda h: h and 'info-' in h):
            link = urljoin(base_url, a.get('href', ''))
            if link.startswith(base_url + '/') and link not in seen:
                seen.add(link)
                links_unik.append((link, a))

        print(f"{len(links_unik)} link ditemukan")

        for link, a in links_unik[:15]:
            try:
                res = scraper.get(link, headers=HEADERS)
                if res.status_code != 200:
                    continue

                dsoup = BeautifulSoup(res.text, 'html.parser')
                full_text = dsoup.get_text(separator=' ')

                if not is_untuk_mahasiswa(full_text):
                    continue

                # Judul dari slug URL
                slug = link.rstrip('/').split('/')[-1]
                slug = slug.replace('info-', '', 1)
                slug = '-'.join(slug.split('-')[:-1])
                judul = slug.replace('-', ' ').title()

                # Cek duplikat
                uid = buat_id_unik(judul, "infolomba.id")
                if uid in id_sudah_ada:
                    print(f"   Duplikat: {judul[:50]}")
                    continue

                # Poster
                poster_url = ''
                img = a.find('img')
                if img:
                    poster_url = img.get('src') or img.get('data-src', '')
                else:
                    wadah = a.find_parent('div')
                    if wadah:
                        wadah_luas = wadah.find_parent('div') or wadah
                        img_card = wadah_luas.find('img')
                        if img_card:
                            poster_url = img_card.get('src') or img_card.get('data-src', '')
                if poster_url and not poster_url.startswith('http'):
                    poster_url = urljoin(base_url, poster_url)
                if not poster_url:
                    for img in dsoup.find_all('img'):
                        src = img.get('src') or img.get('data-src', '')
                        if src and '/poster/' in src:
                            poster_url = urljoin(base_url, src)
                            break

                # Caption
                caption = "Deskripsi tidak ditemukan"
                if "Daftar Sekarang" in full_text and "Laporkan Lomba" in full_text:
                    raw = full_text.split("Daftar Sekarang")[-1].split("Laporkan Lomba")[0]
                    caption = '\n'.join([l.strip() for l in raw.split('\n') if l.strip()])

                # Link pendaftaran
                link_pendaftaran = []
                btn = dsoup.find(lambda t: t.name == 'a' and t.text and 'Daftar Sekarang' in t.text)
                if btn and btn.get('href') and btn['href'] not in ['#', ''] and not btn['href'].startswith('javascript'):
                    link_pendaftaran.append(btn['href'])

                hasil.append({
                    "id": uid,
                    "sumber": "infolomba.id",
                    "judul": judul,
                    "poster": poster_url,
                    "caption": caption[:500] + "...",
                    "link_pendaftaran": link_pendaftaran,
                    "link_direct": link
                })
                id_sudah_ada.add(uid)
                print(f"   {judul[:60]}")

            except Exception as e:
                print(f"   {e}")

    except Exception as e:
        print(f" {e}")

    return hasil


# ─── 2. SILOMBA.ID ─────────────────────────────
async def scrape_silomba(id_sudah_ada):
    print("silomba.id...")
    base_url = "https://silomba.id"
    hasil = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        page = await browser.new_page()
        await page.goto(base_url, wait_until="networkidle")
        await page.wait_for_selector("#competition-section", timeout=15000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        html = await page.content()
        await page.close()

        soup = BeautifulSoup(html, 'html.parser')
        section = soup.find(id='competition-section')
        if not section:
            print(" Section tidak ditemukan")
            await browser.close()
            return hasil

        cards = section.find_all('a', href=lambda h: h and h.startswith('/lomba/'))
        print(f"{len(cards)} card ditemukan")

        for card in cards:
            judul = card.get('aria-label', '').replace('Lihat detail kompetisi ', '').strip()
            if not judul:
                h = card.find(['h2', 'h3', 'h4'])
                judul = h.text.strip() if h else "Tanpa judul"

            # Cek duplikat
            uid = buat_id_unik(judul, "silomba.id")
            if uid in id_sudah_ada:
                print(f"   Duplikat: {judul[:50]}")
                continue

            link_detail = urljoin(base_url, card['href'])
            poster = ''
            caption = "Deskripsi tidak ditemukan"
            link_pendaftaran = []

            try:
                dp = await browser.new_page()
                await dp.goto(link_detail, wait_until="networkidle")
                await dp.wait_for_timeout(2000)
                dhtml = await dp.content()
                await dp.close()

                dsoup = BeautifulSoup(dhtml, 'html.parser')
                full_text = dsoup.get_text(separator=' ')

                if not is_untuk_mahasiswa(full_text):
                    print(f"   Skip: {judul[:50]}")
                    continue

                # Poster original (bebas watermark)
                imgs = dsoup.find_all('img', src=lambda s: s and 'original-poster' in s)
                if imgs:
                    poster = imgs[0].get('src', '')
                else:
                    fallback = dsoup.find_all('img', src=lambda s: s and 'storage2.silomba.id' in s)
                    if fallback:
                        poster = fallback[0].get('src', '')

                if "Deskripsi Lomba" in full_text:
                    caption = full_text.split("Deskripsi Lomba")[-1].strip()[:500] + "..."

                # Link pendaftaran
                for btn in dsoup.find_all(lambda t: t.name == 'a' and t.text and
                                          any(x in t.text for x in ['Daftar', 'Website Resmi', 'Register'])):
                    href = btn.get('href', '')
                    if href and href != '#' and not href.startswith('javascript') and href not in link_pendaftaran:
                        link_pendaftaran.append(href)

            except Exception as e:
                print(f"   {e}")

            hasil.append({
                "id": uid,
                "sumber": "silomba.id",
                "judul": judul,
                "poster": poster,
                "caption": caption,
                "link_pendaftaran": link_pendaftaran,
                "link_direct": link_detail
            })
            id_sudah_ada.add(uid)
            print(f"   {judul[:55]} | poster: {'✓' if poster else '✗'}")

        await browser.close()

    return hasil


# ─── 3. INSTAGRAM ──────────────────────────────
def scrape_instagram(id_sudah_ada):
    if not IG_SESSION_ID:
        print(" IG_SESSION_ID tidak diset, skip Instagram")
        return []

    print("Instagram...")
    hasil = []

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    chrome_options.binary_location = "/opt/chrome/chrome"
    service = Service(executable_path="/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=chrome_options)

    try:
        # Inject cookie
        driver.get("https://www.instagram.com/")
        time.sleep(3)
        driver.add_cookie({'name': 'sessionid', 'value': IG_SESSION_ID, 'domain': '.instagram.com'})
        driver.refresh()
        time.sleep(5)

        if "login" in driver.current_url:
            print("   Cookie expired atau tidak valid")
            return []

        print("Login berhasil via cookie")

        for akun in ['infolomba', 'infolomba_gratis', 'infolomba.olimpiade']:
            print(f"\n   @{akun}...")
            url_posts = []

            driver.get(f"https://www.instagram.com/{akun}/")
            time.sleep(random.randint(4, 6))

            if "Page Not Found" in driver.title:
                print(f"   @{akun} tidak ditemukan")
                continue

            # Scroll & kumpulkan URL post
            last_height = driver.execute_script("return document.body.scrollHeight")
            for scroll in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(random.randint(2, 4))
                links = driver.find_elements(By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]')
                for l in links:
                    href = l.get_attribute('href')
                    if href and href not in url_posts:
                        url_posts.append(href)
                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height:
                    break
                last_height = new_height

            url_posts = list(dict.fromkeys(url_posts))[:6]
            print(f"  → {len(url_posts)} URL akan diproses")

            for url in url_posts:
                try:
                    driver.get(url)
                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.TAG_NAME, "img"))
                        )
                    except:
                        pass
                    time.sleep(random.randint(3, 5))

                    # Caption
                    caption = ""
                    try:
                        caption = driver.find_element(By.XPATH, '//h1').text
                    except:
                        try:
                            meta = driver.find_element(By.XPATH, '//meta[@property="og:description"]').get_attribute('content')
                            caption = meta.split(": ", 1)[1].strip('"') if ": " in meta else meta
                        except:
                            caption = driver.title

                    if not caption or not is_untuk_mahasiswa(caption):
                        print(f"Skip")
                        continue

                    judul = ekstrak_judul_dari_caption(caption)

                    # Cek duplikat
                    uid = buat_id_unik(judul, f"IG @{akun}")
                    if uid in id_sudah_ada:
                        print(f"Duplikat: {judul[:50]}")
                        continue

                    # Poster
                    poster = ""
                    try:
                        poster = driver.execute_script("""
                            var imgs = document.querySelectorAll('img');
                            for (var i = 0; i < imgs.length; i++) {
                                var src = imgs[i].src || '';
                                var alt = (imgs[i].alt || '').toLowerCase();
                                if (alt.includes('profile') || src.includes('150x150')) continue;
                                if (src.includes('scontent') || src.includes('cdninstagram')) {
                                    var srcset = imgs[i].srcset;
                                    if (srcset) {
                                        var parts = srcset.split(',');
                                        return parts[parts.length-1].trim().split(' ')[0];
                                    }
                                    return src;
                                }
                            }
                            return '';
                        """)
                    except:
                        pass
                    if not poster:
                        try:
                            poster = driver.find_element(By.XPATH, '//meta[@property="og:image"]').get_attribute('content')
                        except:
                            pass

                    link_pendaftaran = ekstrak_link_pendaftaran(caption)

                    hasil.append({
                        "id": uid,
                        "sumber": f"IG @{akun}",
                        "judul": judul,
                        "poster": poster,
                        "caption": caption[:500] + "...",
                        "link_pendaftaran": link_pendaftaran,
                        "link_direct": url
                    })
                    id_sudah_ada.add(uid)
                    print(f"     {judul[:55]}")

                except Exception as e:
                    print(f"     {e}")

    finally:
        driver.quit()

    return hasil


# ─── MAIN ──────────────────────────────────────
async def main():
    # Load data lama & kumpulkan ID yang sudah ada
    data_lama = load_data_lama()
    id_sudah_ada = set(item.get('id', '') for item in data_lama)
    print(f" Data lama: {len(data_lama)} item, {len(id_sudah_ada)} ID unik\n")

    # Scrape semua sumber
    hasil_baru = []
    hasil_baru += scrape_infolomba(id_sudah_ada)
    hasil_baru += await scrape_silomba(id_sudah_ada)
    hasil_baru += scrape_instagram(id_sudah_ada)

    # Gabung data lama + baru (data baru di depan)
    hasil_final = hasil_baru + data_lama

    print(f"\n{'='*50}")
    print(f" {len(hasil_baru)} data baru | Total: {len(hasil_final)}")
    print(f"{'='*50}")

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(hasil_final, f, indent=4, ensure_ascii=False)
    print(f" Disimpan ke {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
