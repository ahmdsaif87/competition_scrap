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


IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

MAHASISWA_KEYWORDS = (
    "mahasiswa",
    "mahasiswi",
    "universitas",
    "kampus",
    "s1",
    "d3",
    "d4",
    "umum",
    "undergraduate",
    "diploma",
    "student",
    "university",
)

IG_ACCOUNTS = ("infolomba", "infolomba_gratis", "infolomba.olimpiade")
MAX_DETAIL_PAGES = 20
MAX_IG_POSTS_PER_ACCOUNT = 8
SILOMBA_DETAIL_CONCURRENCY = 4

URL_PATTERN = re.compile(
    r"(https?://[^\s]+|bit\.ly/[^\s]+|forms\.gle/[^\s]+|"
    r"linktr\.ee/[^\s]+|s\.id/[^\s]+)"
)
DATE_RANGE_PATTERN = re.compile(r"\d{1,2}\s+\w+\s*-\s*\d{1,2}\s+\w+\s+\d{4}")
DATE_PATTERN = re.compile(r"\d{1,2}\s+\w+\s+\d{4}")
SPACE_PATTERN = re.compile(r"\s+")


def is_untuk_mahasiswa(teks):
    teks_lower = str(teks).lower()
    return any(keyword in teks_lower for keyword in MAHASISWA_KEYWORDS)


def buat_id_unik(judul, sumber):
    raw = f"{sumber}_{judul}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def potong_teks(teks, max_len=500):
    teks = SPACE_PATTERN.sub(" ", str(teks)).strip()
    return teks if len(teks) <= max_len else f"{teks[:max_len]}..."


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


def src_gambar(tag):
    return (tag.get("src") or tag.get("data-src") or "") if tag else ""


def buat_item(sumber, judul, links_daftar, poster, caption, timeline):
    return {
        "sumber": sumber,
        "judul": judul,
        "link_pendaftaran": list(dict.fromkeys(links_daftar)),
        "poster": poster,
        "caption": potong_teks(caption),
        "timeline": timeline,
    }


def link_valid(href):
    return bool(href) and href != "#" and not href.startswith("javascript")


def ambil_link_daftar(soup, base_url=""):
    tombol = soup.find(
        lambda tag: tag.name == "a" and "Daftar Sekarang" in tag.get_text(strip=True)
    )

    if tombol and link_valid(tombol.get("href")):
        return [urljoin(base_url, tombol["href"]) if base_url else tombol["href"]]

    return []


def unique_by_judul(items):
    unique = {}

    for item in items:
        key = item["judul"].lower().strip()
        unique.setdefault(key, item)

    return list(unique.values())


def scrape_infolomba():
    print("Scraping infolomba.id...")

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

                if res.status_code != 200:
                    continue

                dsoup = BeautifulSoup(res.text, "html.parser")
                full_text = dsoup.get_text(" ", strip=True)

                if not is_untuk_mahasiswa(full_text):
                    continue

                poster_tag = dsoup.find(
                    "img",
                    src=lambda src: src and ("/poster/" in src or "poster" in src.lower()),
                )
                poster = urljoin(base_url, src_gambar(poster_tag)) if poster_tag else ""

                hasil.append(
                    buat_item(
                        sumber="infolomba.id",
                        judul=teks_tag(dsoup.find(["h1", "h2", "h3"])),
                        links_daftar=ambil_link_daftar(dsoup, base_url),
                        poster=poster,
                        caption=full_text,
                        timeline=ekstrak_timeline(full_text),
                    )
                )
            except Exception as e:
                print(f"Gagal detail infolomba: {e}")
    except Exception as e:
        print(f"Error infolomba: {e}")

    print(f"infolomba.id selesai: {len(hasil)}")
    return hasil


async def scrape_silomba_detail(browser, base_url, detail_url):
    page = await browser.new_page()

    try:
        await page.goto(detail_url, wait_until="networkidle")
        soup = BeautifulSoup(await page.content(), "html.parser")
        full_text = soup.get_text(" ", strip=True)

        if not is_untuk_mahasiswa(full_text):
            return None

        poster_tag = soup.find("img", src=lambda src: src and "original-poster" in src)
        poster_tag = poster_tag or soup.find("img", src=lambda src: src and "storage" in src)
        links_daftar = ambil_link_daftar(soup) or ekstrak_link_dari_teks(full_text)

        return buat_item(
            sumber="silomba.id",
            judul=teks_tag(soup.find(["h1", "h2"])),
            links_daftar=links_daftar,
            poster=urljoin(base_url, src_gambar(poster_tag)) if poster_tag else "",
            caption=full_text,
            timeline=ekstrak_timeline(full_text),
        )
    except Exception as e:
        print(f"Gagal detail silomba: {e}")
        return None
    finally:
        await page.close()


async def scrape_silomba():
    print("Scraping silomba.id...")

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

            if not section:
                return hasil

            detail_urls = [
                urljoin(base_url, card["href"])
                for card in section.find_all(
                    "a", href=lambda href: href and href.startswith("/lomba/")
                )
            ][:MAX_DETAIL_PAGES]

            semaphore = asyncio.Semaphore(SILOMBA_DETAIL_CONCURRENCY)

            async def scrape_detail_terbatas(detail_url):
                async with semaphore:
                    return await scrape_silomba_detail(browser, base_url, detail_url)

            detail_items = await asyncio.gather(
                *(scrape_detail_terbatas(detail_url) for detail_url in detail_urls)
            )
            hasil.extend(item for item in detail_items if item)
        except Exception as e:
            print(f"Error silomba: {e}")
        finally:
            await page.close()
            await browser.close()

    print(f"silomba.id selesai: {len(hasil)}")
    return hasil


def buat_chrome_options():
    options = Options()

    for arg in (
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        f"user-agent={HEADERS['User-Agent']}",
    ):
        options.add_argument(arg)

    options.binary_location = "/opt/chrome/chrome"
    return options


def ambil_caption_instagram(driver):
    try:
        return driver.find_element(By.XPATH, "//h1").text
    except Exception:
        pass

    try:
        meta = driver.find_element(
            By.XPATH, '//meta[@property="og:description"]'
        ).get_attribute("content")
        return meta.split(": ", 1)[1] if ": " in meta else meta
    except Exception:
        return driver.title


def scrape_instagram():
    if not IG_SESSION_ID:
        print("IG_SESSION_ID belum diset")
        return []

    print("Scraping Instagram...")

    hasil = []
    driver = webdriver.Chrome(
        service=Service("/usr/bin/chromedriver"),
        options=buat_chrome_options(),
    )

    try:
        driver.get("https://www.instagram.com/")
        time.sleep(3)
        driver.add_cookie(
            {"name": "sessionid", "value": IG_SESSION_ID, "domain": ".instagram.com"}
        )
        driver.refresh()
        time.sleep(5)

        for akun in IG_ACCOUNTS:
            driver.get(f"https://www.instagram.com/{akun}/")
            time.sleep(random.randint(4, 6))

            post_links = []

            for _ in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(random.randint(2, 4))

                elems = driver.find_elements(
                    By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]'
                )
                post_links.extend(
                    href for elem in elems if (href := elem.get_attribute("href"))
                )

            for post_url in list(dict.fromkeys(post_links))[:MAX_IG_POSTS_PER_ACCOUNT]:
                try:
                    driver.get(post_url)

                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.TAG_NAME, "img"))
                        )
                    except Exception:
                        pass

                    time.sleep(random.randint(3, 5))
                    caption = ambil_caption_instagram(driver)

                    if not caption or not is_untuk_mahasiswa(caption):
                        continue

                    lines = [
                        line.strip()
                        for line in caption.splitlines()
                        if len(line.strip()) > 5
                    ]

                    try:
                        poster = driver.find_element(
                            By.XPATH, '//meta[@property="og:image"]'
                        ).get_attribute("content")
                    except Exception:
                        poster = ""

                    hasil.append(
                        buat_item(
                            sumber=f"IG @{akun}",
                            judul=lines[0][:150] if lines else "Tanpa Judul",
                            links_daftar=ekstrak_link_dari_teks(caption),
                            poster=poster,
                            caption=caption,
                            timeline=ekstrak_timeline(caption),
                        )
                    )
                except Exception as e:
                    print(f"Gagal post IG: {e}")
    except Exception as e:
        print(f"Error IG: {e}")
    finally:
        driver.quit()

    print(f"Instagram selesai: {len(hasil)}")
    return hasil


async def main():
    results = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba),
        scrape_silomba(),
        asyncio.to_thread(scrape_instagram),
    )
    semua = [item for result in results if isinstance(result, list) for item in result]

    print(json.dumps(unique_by_judul(semua), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
