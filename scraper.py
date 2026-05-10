import asyncio
import hashlib
import json
import os
import random
import re
import time
from urllib.parse import urljoin

import cloudscraper
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from groq import Groq

IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

MAHASISWA_KEYWORDS = (
    "mahasiswa", "mahasiswi", "universitas", "kampus", "s1", 
    "d3", "d4", "umum", "undergraduate", "diploma", "student", "university",
)

IG_ACCOUNTS = ("infolomba", "infolomba_gratis", "infolomba.olimpiade")
MAX_DETAIL_PAGES = 10
MAX_IG_POSTS_PER_ACCOUNT = 5
SILOMBA_DETAIL_CONCURRENCY = 4

URL_PATTERN = re.compile(r"(https?://[^\s]+|bit\.ly/[^\s]+|forms\.gle/[^\s]+|linktr\.ee/[^\s]+|s\.id/[^\s]+)")
DATE_RANGE_PATTERN = re.compile(r"\d{1,2}\s+\w+\s*-\s*\d{1,2}\s+\w+\s+\d{4}")
DATE_PATTERN = re.compile(r"\d{1,2}\s+\w+\s+\d{4}")
SPACE_PATTERN = re.compile(r"\s+")


def proses_item_dengan_groq(sumber, teks_mentah, links_mentah, poster_url, judul_mentah, timeline_mentah):
    fallback_dict = {
        "sumber": sumber,
        "judul": judul_mentah,
        "link_pendaftaran": links_mentah,
        "poster": poster_url,
        "caption": teks_mentah[:1000] + "..." if len(teks_mentah) > 1000 else teks_mentah,
        "timeline": timeline_mentah
    }

    if not groq_client:
        return fallback_dict

    prompt = f"""
    Tugasmu adalah menganalisis teks informasi lomba dan merapikannya ke dalam format JSON.
    1. "sumber": Gunakan nilai "{sumber}".
    2. "judul": EKSTRAK judul acara/kompetisi utama langsung dari "Teks Asli". Abaikan "Judul Mentah" jika isinya "Tanpa Judul", terpotong, atau tidak relevan. Buat judul yang bersih dan profesional (buang sapaan dan emoji).
    3. "link_pendaftaran": Pastikan ini berupa array berisi string URL valid (gabungkan dari "Link Pendaftaran Mentah" dan URL yang mungkin tersembunyi di "Teks Asli").
    4. "poster": Gunakan URL "{poster_url}".
    5. "caption": Buat caption yang sudah diperindah dari "Teks Asli", rapikan paragraf, perbaiki typo, dan buang hashtag spam.
    6. "timeline": Ekstrak atau rapikan tanggal/timeline lomba dari "Teks Asli" atau "Timeline Mentah".

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
    Timeline Mentah: {timeline_mentah}
    Teks Asli: {teks_mentah}
    """

    try:
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful API that strictly outputs valid JSON only. Do not wrap the response in markdown blocks."
                },
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            model="qwen/qwen3-32b",
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        
        hasil_llm = json.loads(chat_completion.choices[0].message.content)
        
        for key in fallback_dict.keys():
            if key not in hasil_llm:
                hasil_llm[key] = fallback_dict[key]
                
        return hasil_llm

    except Exception as e:
        print(f"[WARN] Error memanggil Groq API: {e}")
        return fallback_dict


def is_untuk_mahasiswa(teks):
    teks_lower = str(teks).lower()
    return any(keyword in teks_lower for keyword in MAHASISWA_KEYWORDS)

def ekstrak_link_dari_teks(teks):
    links = []
    for link in URL_PATTERN.findall(str(teks)):
        bersih = re.sub(r"[),.!]+$", "", link)
        links.append(bersih if bersih.startswith("http") else f"https://{bersih}")
    return list(dict.fromkeys(links))

def ekstrak_timeline(teks):
    teks = str(teks)
    rentang = DATE_RANGE_PATTERN.search(teks)
    if rentang:
        return rentang.group(0)
    tanggal = DATE_PATTERN.findall(teks)
    return f"{tanggal[0]} - {tanggal[-1]}" if len(tanggal) >= 2 else ""

def teks_tag(tag, default="Tanpa Judul"):
    return tag.get_text(strip=True) if tag else default

def link_valid(href):
    return bool(href) and href != "#" and not href.startswith("javascript")

def ambil_link_daftar(soup, base_url=""):
    tombol = soup.find(lambda tag: tag.name == "a" and "Daftar Sekarang" in tag.get_text(strip=True))
    if tombol and link_valid(tombol.get("href")):
        return [urljoin(base_url, tombol["href"]) if base_url else tombol["href"]]
    return []

def unique_by_judul(items):
    unique = {}
    for item in items:
        key = item.get("judul", "").lower().strip()
        unique.setdefault(key, item)
    return list(unique.values())


def scrape_infolomba():
    print("[INFO] Scraping infolomba.id...")
    hasil = []
    base_url = "https://infolomba.id"
    scraper = cloudscraper.create_scraper()

    try:
        home = scraper.get(base_url, headers=HEADERS, timeout=20)
        home.raise_for_status()
        soup = BeautifulSoup(home.text, "html.parser")
        detail_links = {
            urljoin(base_url, tag["href"])
            for tag in soup.find_all("a", href=True)
            if "info-" in tag["href"] and urljoin(base_url, tag["href"]).startswith(base_url)
        }

        for link in list(detail_links)[:MAX_DETAIL_PAGES]:
            try:
                res = scraper.get(link, headers=HEADERS, timeout=20)
                if res.status_code != 200: continue

                dsoup = BeautifulSoup(res.text, "html.parser")
                full_text = dsoup.get_text(" ", strip=True)
                if not is_untuk_mahasiswa(full_text): continue

                poster_url = ""
                for img in dsoup.find_all('img'):
                    src = img.get('src') or img.get('data-src', '')
                    if src and '/poster/' in src:
                        poster_url = urljoin(base_url, src)
                        break

                judul_mentah = teks_tag(dsoup.find(["h1", "h2", "h3"]))
                links_mentah = ambil_link_daftar(dsoup, base_url)
                timeline_mentah = ekstrak_timeline(full_text)

                item_llm = proses_item_dengan_groq("infolomba.id", full_text, links_mentah, poster_url, judul_mentah, timeline_mentah)
                hasil.append(item_llm)
                
                print(f"  [OK] {item_llm.get('judul', '')[:50]}")
            except Exception as e:
                print(f"  [WARN] Gagal detail infolomba: {e}")
    except Exception as e:
        print(f"[ERROR] Error infolomba: {e}")

    return hasil


async def scrape_silomba_detail(browser, base_url, detail_url):
    page = await browser.new_page()
    try:
        await page.goto(detail_url, wait_until="networkidle")
        soup = BeautifulSoup(await page.content(), "html.parser")
        full_text = soup.get_text(" ", strip=True)

        if not is_untuk_mahasiswa(full_text):
            return None

        poster = ""
        semua_img = soup.find_all('img', src=lambda s: s and 'original-poster' in s)
        if semua_img:
            poster = semua_img[0].get('src', '')
        else:
            fallback = soup.find_all('img', src=lambda s: s and 'storage2.silomba.id' in s)
            if fallback: poster = fallback[0].get('src', '')

        poster_url = urljoin(base_url, poster) if poster else ""
        links_mentah = ambil_link_daftar(soup) or ekstrak_link_dari_teks(full_text)
        judul_mentah = teks_tag(soup.find(["h1", "h2"]))
        timeline_mentah = ekstrak_timeline(full_text)

        item_llm = await asyncio.to_thread(proses_item_dengan_groq, "silomba.id", full_text, links_mentah, poster_url, judul_mentah, timeline_mentah)
        print(f"  [OK] {item_llm.get('judul', '')[:50]}")
        return item_llm
    except Exception as e:
        print(f"  [WARN] Gagal detail silomba: {e}")
        return None
    finally:
        await page.close()


async def scrape_silomba():
    print("[INFO] Scraping silomba.id...")
    hasil = []
    base_url = "https://silomba.id"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(base_url, wait_until="networkidle")
            await page.wait_for_selector("#competition-section", timeout=15000)

            soup = BeautifulSoup(await page.content(), "html.parser")
            section = soup.find(id="competition-section")
            if not section: return hasil

            detail_urls = [
                urljoin(base_url, card["href"])
                for card in section.find_all("a", href=lambda href: href and href.startswith("/lomba/"))
            ][:MAX_DETAIL_PAGES]

            semaphore = asyncio.Semaphore(SILOMBA_DETAIL_CONCURRENCY)

            async def scrape_detail_terbatas(detail_url):
                async with semaphore:
                    return await scrape_silomba_detail(browser, base_url, detail_url)

            detail_items = await asyncio.gather(*(scrape_detail_terbatas(url) for url in detail_urls))
            hasil.extend(item for item in detail_items if item)
        except Exception as e:
            print(f"[ERROR] Error silomba: {e}")
        finally:
            await page.close()
            await browser.close()

    return hasil


def buat_chrome_options():
    options = Options()
    for arg in ("--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", f"user-agent={HEADERS['User-Agent']}"):
        options.add_argument(arg)
    options.binary_location = "/opt/chrome/chrome"
    return options

def ambil_caption_instagram(driver):
    try:
        return driver.find_element(By.XPATH, "//h1").text
    except Exception:
        try:
            meta = driver.find_element(By.XPATH, '//meta[@property="og:description"]').get_attribute("content")
            return meta.split(": ", 1)[1] if ": " in meta else meta
        except Exception:
            return driver.title


def scrape_instagram():
    if not IG_SESSION_ID:
        print("[WARN] IG_SESSION_ID belum diset")
        return []

    print("[INFO] Scraping Instagram...")
    hasil = []
    driver = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=buat_chrome_options())

    try:
        driver.get("https://www.instagram.com/")
        time.sleep(3)
        driver.add_cookie({"name": "sessionid", "value": IG_SESSION_ID, "domain": ".instagram.com"})
        driver.refresh()
        time.sleep(5)

        for akun in IG_ACCOUNTS:
            driver.get(f"https://www.instagram.com/{akun}/")
            time.sleep(random.randint(4, 6))

            post_links = []
            for _ in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(random.randint(2, 4))
                elems = driver.find_elements(By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]')
                post_links.extend(href for elem in elems if (href := elem.get_attribute("href")))

            for post_url in list(dict.fromkeys(post_links))[:MAX_IG_POSTS_PER_ACCOUNT]:
                try:
                    driver.get(post_url)
                    try:
                        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "img")))
                    except Exception:
                        pass
                    time.sleep(random.randint(3, 5))
                    caption_mentah = ambil_caption_instagram(driver)

                    if not caption_mentah or not is_untuk_mahasiswa(caption_mentah):
                        continue

                    js_get_img = """
                    var imgs = document.querySelectorAll('img');
                    for (var i = 0; i < imgs.length; i++) {
                        var alt = (imgs[i].alt || '').toLowerCase();
                        var src = (imgs[i].src || '');
                        if (alt.includes('profile') || alt.includes('logo') || src.includes('150x150')) continue;
                        if (src.includes('scontent') || src.includes('cdninstagram')) {
                            var srcset = imgs[i].srcset;
                            if (srcset) {
                                var parts = srcset.split(',');
                                return parts[parts.length - 1].trim().split(' ')[0];
                            }
                            return src; 
                        }
                    }
                    return '';
                    """
                    poster = driver.execute_script(js_get_img)
                    
                    links_mentah = ekstrak_link_dari_teks(caption_mentah)
                    timeline_mentah = ekstrak_timeline(caption_mentah)

                    item_llm = proses_item_dengan_groq(f"IG @{akun}", caption_mentah, links_mentah, poster, "Tanpa Judul", timeline_mentah)
                    hasil.append(item_llm)
                    
                    print(f"  [OK] {item_llm.get('judul', '')[:50]}")
                except Exception as e:
                    print(f"  [WARN] Gagal post IG: {e}")
    except Exception as e:
        print(f"[ERROR] Error IG: {e}")
    finally:
        driver.quit()

    return hasil


async def main():
    results = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba),
        scrape_silomba(),
        asyncio.to_thread(scrape_instagram),
    )
    semua = [item for result in results if isinstance(result, list) for item in result]
    
    hasil_akhir = unique_by_judul(semua)
    json_output = json.dumps(hasil_akhir, indent=2, ensure_ascii=False)
    
    print("\n[INFO] === HASIL AKHIR ===")
    print(json_output)
    
    with open('api_collabfinder.json', 'w', encoding='utf-8') as f:
        f.write(json_output)


if __name__ == "__main__":
    asyncio.run(main())