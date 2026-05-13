"""Competition scraper: infolomba.id, silomba.id, and Instagram."""

from __future__ import annotations

import asyncio, hashlib, json, os, random, re, time, unicodedata
from urllib.parse import urljoin, urlparse

import cloudscraper, pymongo
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
    genai = genai_types = None

# ── Config ────────────────────────────────────────────────────────────────
IG_SESSION_ID = os.environ.get("IG_SESSION_ID", "")
MONGO_URI = os.environ.get("MONGO_URI")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DB_NAME = os.environ.get("DB_NAME", "competition_scraper")
COLLECTION = os.environ.get("COLLECTION", "competition")
IG_ACCOUNTS = [a.strip() for a in os.environ.get(
    "IG_ACCOUNTS", "infolomba,infolomba_gratis,infolomba.olimpiade"
).split(",") if a.strip()]
MAX_WEB = int(os.environ.get("MAX_WEB_ITEMS", "15"))
MAX_IG = int(os.environ.get("MAX_IG_POSTS_PER_ACCOUNT", "6"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA}

# ── Patterns & Keywords ──────────────────────────────────────────────────
URL_RE = re.compile(r"https?://[^\s<>'\"`)\]}]+", re.I)
IG_SC_RE = re.compile(r"/(?:p|reel)/([^/?#]+)/?")
TITLE_PFX_RE = re.compile(r"^\s*(?:judul|title|nama\s+lomba|competition|event)\s*[:\-]\s*", re.I)
OPEN_REG_RE = re.compile(r"open\s+registration\s*[:\-]\s*([^\]\n]+)", re.I)
WS_RE = re.compile(r"\s+")

KW_REG = {"daftar", "pendaftaran", "register", "registrasi", "registration", "apply", "submission", "submit"}
KW_NON_REG = {"guidebook", "booklet", "juknis", "contact", "kontak", "whatsapp", "wa.me", "cp", "narahubung", "email", "instagram", "tiktok", "youtube"}
KW_NOISE = {"link pendaftaran", "pendaftaran", "register", "registration", "apply now", "guidebook", "contact us", "whatsapp", "deadline", "benefit", "prize", "hadiah", "timeline", "save the date", "open registration", "closed registration", "terbuka untuk", "untuk mahasiswa"}
KW_MHSW = {"mahasiswa", "mahasiswi", "universitas", "kampus", "s1", "d3", "d4", "umum", "undergraduate", "diploma", "student", "university"}
BLOCKED_HOSTS = {"instagram.com", "facebook.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com", "wa.me", "api.whatsapp.com"}
FORM_HOSTS = {"forms.gle", "docs.google.com", "bit.ly", "s.id", "tinyurl.com", "lynk.id"}
STOPWORDS = {"the", "of", "and", "in", "on", "at", "to", "a", "an", "di", "ke", "se", "dan", "atau", "untuk", "dengan", "dalam", "dari", "oleh", "yang", "adalah", "ini", "itu"}
SRC_PRIO = {"infolomba.id": 0, "silomba.id": 1}
KW_COMP = {"lomba", "competition", "olimpiade", "challenge", "contest"}
KW_EVENT = {"conference", "summit", "bootcamp", "program", "award"}

# ── Helpers ───────────────────────────────────────────────────────────────
def _h(text, kws):
    lo = (text or "").lower()
    return any(k in lo for k in kws)

make_id = lambda t, s: hashlib.md5(f"{s}_{t}".lower().strip().encode()).hexdigest()[:12]
is_mahasiswa = lambda t: _h(t, KW_MHSW)
norm = lambda t: WS_RE.sub(" ", t or "").strip()

def clean_url(url, base=""):
    if not url: return ""
    url = url.strip().strip(".,;:!?\"'`)]}") 
    if url.startswith("//"): url = "https:" + url
    elif base and url.startswith("/"): url = urljoin(base, url)
    return url if url.startswith(("http://", "https://")) else ""

def strip_symbols(text):
    return "".join(" " if (unicodedata.category(c).startswith("S") and c not in {"&", "+", "#"}) else c for c in (text or ""))

def clean_title(text):
    text = strip_symbols(text)
    text = TITLE_PFX_RE.sub("", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r'[@#*_`>|""\'\']+', " ", text)
    text = re.sub(r"\b(?:caption|repost|info lomba|infolomba)\b", " ", text, flags=re.I)
    text = re.sub(r"\s*[-–—|:]\s*(?:open registration|registration|pendaftaran).*$", "", text, flags=re.I)
    return norm(text)[:140].strip(" -:|") or "Tanpa Judul"

def safe_json_loads(text):
    try: return json.loads(text)
    except Exception:
        m = re.search(r"\[.*\]", text or "", re.S)
        if m:
            try: return json.loads(m.group(0))
            except Exception: pass
    return []

def _strip_caption_for_llm(caption):
    """Strip URLs and excessive emojis from caption to save LLM tokens."""
    text = re.sub(r"https?://\S+", "", caption or "")
    text = re.sub(r"[🚨🔥✨💥🏅🏆🎫💰📅🗓📤🎯🎉⚠️📌📲🎬🎥💸👤👥🔗]+", "", text)
    return norm(text)[:1200]

# ── Soup helpers ──────────────────────────────────────────────────────────
def anchor_rows(soup, base=""):
    return [{"url": h, "label": norm(a.get_text(" "))}
            for a in soup.find_all("a", href=True) if (h := clean_url(a["href"], base))]

def best_poster(soup, base=""):
    for prop in ("og:image", "twitter:image"):
        kw = {"property": prop} if "og:" in prop else {"name": prop}
        n = soup.find("meta", attrs=kw)
        if n and (u := clean_url(n.get("content", ""), base)): return u
    for img in soup.find_all("img"):
        src = clean_url(img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "", base)
        if src and not any(s in src.lower() for s in ("logo", "avatar", "profile")): return src
    return ""

# ── Title extraction ──────────────────────────────────────────────────────
def _score(line, pos):
    lo = line.lower()
    return (100 - pos + 25 * any(w in lo for w in KW_COMP)
            + 10 * any(w in lo for w in KW_EVENT)
            + 8 * (line.isupper() and len(line) > 8)
            + 5 * bool(re.search(r"\b20\d{2}\b", line)))

def extract_title(caption):
    lines = [norm(l) for l in (caption or "").splitlines() if norm(l)]
    cands = []
    for i, line in enumerate(lines[:25]):
        m = OPEN_REG_RE.search(strip_symbols(line))
        if m:
            t = clean_title(m.group(1))
            if t != "Tanpa Judul": cands.append((_score(t, i) + 35, t)); continue
        if URL_RE.search(line) or len(norm(line)) < 6 or _h(line, KW_NOISE): continue
        t = clean_title(line)
        if t != "Tanpa Judul": cands.append((_score(t, i), t))
    if cands: return max(cands)[1]
    for l in lines:
        if not (URL_RE.search(l) or len(norm(l)) < 6 or _h(l, KW_NOISE)):
            t = clean_title(l)
            if t != "Tanpa Judul": return t
    return "Tanpa Judul"

# ── Link extraction ───────────────────────────────────────────────────────
def extract_urls(text):
    seen, r = set(), []
    for m in URL_RE.finditer(text or ""):
        u = clean_url(m.group(0))
        if u and u not in seen: seen.add(u); r.append(u)
    return r

def extract_reg_links(text="", anchors=None):
    anchors = anchors or []
    is_blocked = lambda u: urlparse(u).netloc.lower() in BLOCKED_HOSTS
    found = []
    for row in anchors:
        u = clean_url(row.get("url", ""))
        if u and not is_blocked(u) and _h(row.get("label", ""), KW_REG) and not _h(row.get("label", ""), KW_NON_REG):
            found.append(u)
    for i, line in enumerate((text or "").splitlines()):
        urls = extract_urls(line.strip())
        if not urls: continue
        ctx = " ".join((text or "").splitlines()[max(0, i-1):i+2])
        if _h(ctx, KW_REG) and not _h(line, KW_NON_REG):
            found.extend(u for u in urls if not is_blocked(u))
    if found: return list(dict.fromkeys(found))
    all_u = [r["url"] for r in anchors if r.get("url")] + extract_urls(text)
    return list(dict.fromkeys(
        u for raw in all_u if (u := clean_url(raw)) and not is_blocked(u)
        and any(h in urlparse(u).netloc.lower() for h in FORM_HOSTS)
    ))

# ── LLM (Gemini) ─────────────────────────────────────────────────────────
def _init_gemini():
    if not GEMINI_API_KEY or not genai: return None
    try: return genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e: print(f"[LLM] Gemini inactive: {e}"); return None

GCLIENT = _init_gemini()

_PROMPT = (
    'Per item kembalikan JSON array: [{"i":"id","j":"judul","l":["link"],'
    '"timeline":{"pendaftaran":"","pengumpulan":""},"kategori":"..."}]\n'
    "j=nama lomba/event saja tanpa emoji/tanggal/URL. "
    "l=URL pendaftaran saja bukan sosmed/guidebook. "
    "timeline: pendaftaran=tanggal daftar dibuka, pengumpulan=deadline submit, "
    'kosongkan "" jika tidak ada. '
    "kategori: pilih 1 dari IT|Business|Design|Science|Engineering|Social|Health|"
    "Education|Arts|Environment|Writing|Sports|Photography|Video|Music|Robotics|"
    'Debate|Entrepreneurship|Data Science|Cybersecurity|General.\nData:\n'
)

def _llm(prompt):
    if not GCLIENT or not genai_types: return []
    for attempt in range(3):
        try:
            r = GCLIENT.models.generate_content(
                model="gemini-2.0-flash-lite", contents=prompt,
                config=genai_types.GenerateContentConfig(response_mime_type="application/json"),
            )
            d = safe_json_loads(r.text or "")
            return d if isinstance(d, list) else []
        except Exception as e:
            print(f"[LLM] Error attempt {attempt+1}: {e}")
            time.sleep(10 * (attempt + 1))
    return []

def gemini_process(batch):
    if not GCLIENT or not batch: return batch
    payload = [{"i": it["id"], "j": it.get("judul", ""),
                "c": _strip_caption_for_llm(it.get("caption", "")),
                "l": it.get("link_pendaftaran", [])} for it in batch]
    lmap = {r["i"]: r for r in _llm(_PROMPT + json.dumps(payload, ensure_ascii=False))
            if isinstance(r, dict) and r.get("i")}
    for it in batch:
        r = lmap.get(it["id"], {})
        if r.get("j"):
            t = clean_title(r["j"])
            if t != "Tanpa Judul": it["judul"] = t
        if r.get("l"):
            lks = [u for raw in r["l"] if (u := clean_url(raw))]
            if lks: it["link_pendaftaran"] = list(dict.fromkeys(lks))
        tl = r.get("timeline")
        if isinstance(tl, dict):
            it["timeline"] = {"pendaftaran": tl.get("pendaftaran", ""),
                              "pengumpulan": tl.get("pengumpulan", "")}
        it["kategori"] = r.get("kategori", "General") or "General"
    time.sleep(2)
    return batch

# ── Build item ────────────────────────────────────────────────────────────
def _item(uid, src, title, poster, caption, links, url):
    return {"id": uid, "sumber": src, "judul": title, "poster": poster,
            "caption": caption, "link_pendaftaran": links, "link_direct": url,
            "timeline": {"pendaftaran": "", "pengumpulan": ""}, "kategori": "General"}

# ── Scraper: infolomba.id ─────────────────────────────────────────────────
def scrape_infolomba(seen_ids):
    print("[infolomba] Starting...")
    base = "https://infolomba.id"
    sc = cloudscraper.create_scraper()
    results = []
    try:
        resp = sc.get(base, headers=HEADERS, timeout=30); resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        links = {urljoin(base, a["href"]): a for a in soup.find_all("a", href=lambda h: h and "info-" in h)
                 if urljoin(base, a.get("href", "")).startswith(base + "/")}
        for link, anchor in list(links.items())[:MAX_WEB]:
            try:
                res = sc.get(link, headers=HEADERS, timeout=30)
                if res.status_code != 200: continue
                ds = BeautifulSoup(res.text, "html.parser")
                ft = ds.get_text("\n")
                if not is_mahasiswa(ft): continue
                th = ds.find(["h1", "h2"])
                slug = "-".join(link.rstrip("/").split("/")[-1].replace("info-", "", 1).split("-")[:-1]).replace("-", " ").title()
                title = clean_title(th.get_text(" ") if th else slug)
                uid = make_id(title, "infolomba.id")
                if uid in seen_ids: continue
                if "Daftar Sekarang" in ft and "Laporkan Lomba" in ft:
                    caption = "\n".join(l.strip() for l in ft.split("Daftar Sekarang")[-1].split("Laporkan Lomba")[0].splitlines() if l.strip())
                else:
                    caption = "\n".join(l.strip() for l in ft.splitlines() if l.strip())[:2500]
                poster = best_poster(ds, base)
                if not poster:
                    img = anchor.find("img") or {}
                    poster = clean_url(img.get("src") or img.get("data-src") or "", base)
                results.append(_item(uid, "infolomba.id", title, poster, caption,
                                     extract_reg_links(ft, anchor_rows(ds, base)), link))
                seen_ids.add(uid)
            except Exception as e: print(f"[infolomba] Skip {link}: {e}")
    except Exception as e: print(f"[infolomba] Error: {e}")
    print(f"[infolomba] Done: {len(results)} items"); return results

# ── Scraper: silomba.id ───────────────────────────────────────────────────
async def scrape_silomba(seen_ids):
    print("[silomba] Starting...")
    base = "https://silomba.id"
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            pg = await browser.new_page(user_agent=UA)
            await pg.goto(base, wait_until="networkidle", timeout=45000)
            await pg.wait_for_selector("#competition-section", timeout=15000)
            await pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await pg.wait_for_timeout(2000)
            soup = BeautifulSoup(await pg.content(), "html.parser"); await pg.close()
            section = soup.find(id="competition-section")
            if not section: return results
            for card in section.find_all("a", href=lambda h: h and h.startswith("/lomba/"))[:MAX_WEB]:
                raw = (card.get("aria-label", "").replace("Lihat detail kompetisi ", "").strip()
                       or ((h := card.find(["h1","h2","h3","h4"])) and h.get_text(" ").strip())
                       or "Tanpa Judul")
                title = clean_title(raw)
                uid = make_id(title, "silomba.id")
                if uid in seen_ids: continue
                detail = urljoin(base, card["href"])
                poster = caption = ""; lks = []
                try:
                    dp = await browser.new_page(user_agent=UA)
                    await dp.goto(detail, wait_until="networkidle", timeout=45000)
                    ds = BeautifulSoup(await dp.content(), "html.parser"); await dp.close()
                    ft = ds.get_text("\n")
                    if not is_mahasiswa(ft): continue
                    poster = best_poster(ds, base)
                    caption = (ft.split("Deskripsi Lomba")[-1].strip() if "Deskripsi Lomba" in ft
                               else "\n".join(l.strip() for l in ft.splitlines() if l.strip())[:2500])
                    lks = extract_reg_links(ft, anchor_rows(ds, base))
                except Exception as e: print(f"[silomba] Detail failed {detail}: {e}")
                results.append(_item(uid, "silomba.id", title, poster, caption, lks, detail))
                seen_ids.add(uid)
        except Exception as e: print(f"[silomba] Error: {e}")
        finally: await browser.close()
    print(f"[silomba] Done: {len(results)} items"); return results

# ── Scraper: Instagram ────────────────────────────────────────────────────
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

def _chrome():
    opts = Options()
    for a in ("--headless=new", "--disable-gpu", "--no-sandbox",
              "--disable-dev-shm-usage", f"user-agent={UA}"):
        opts.add_argument(a)
    if os.path.exists("/opt/chrome/chrome"): opts.binary_location = "/opt/chrome/chrome"
    svc = Service("/usr/bin/chromedriver") if os.path.exists("/usr/bin/chromedriver") else Service()
    return webdriver.Chrome(service=svc, options=opts)

def _ig_posts(driver, acct):
    driver.get(f"https://www.instagram.com/{acct}/")
    time.sleep(random.randint(4, 6))
    if "page not found" in driver.title.lower(): return []
    seen, urls, lh = set(), [], driver.execute_script("return document.body.scrollHeight")
    for _ in range(3):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.randint(2, 4))
        for el in driver.find_elements(By.XPATH, '//a[contains(@href,"/p/") or contains(@href,"/reel/")]'):
            href = el.get_attribute("href")
            if href and href not in seen: seen.add(href); urls.append(href)
        nh = driver.execute_script("return document.body.scrollHeight")
        if nh == lh: break
        lh = nh
    return urls

def _ig_post(driver, url, acct, seen_ids):
    driver.get(url)
    try: WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "article")))
    except: WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "img")))
    time.sleep(random.randint(3, 5))
    caption = ""
    if h1s := driver.find_elements(By.XPATH, "//article//h1"): caption = h1s[0].text
    if not caption:
        if ms := driver.find_elements(By.XPATH, '//meta[@property="og:description"]'):
            raw = ms[0].get_attribute("content") or ""
            caption = raw.split(": ", 1)[1] if ": " in raw else raw
    caption = re.sub(r"^\s*[^:\n]{1,80}\s+on Instagram:\s*", "", (caption or driver.title).strip(), flags=re.I)
    caption = re.sub(r'^\s*"|\"\s*$', "", caption).strip()
    if not caption or not is_mahasiswa(caption): return None
    sc = (IG_SC_RE.search(url or "") or type("", (), {"group": lambda s, x: url})()).group(1)
    uid = make_id(sc, f"IG @{acct}")
    if uid in seen_ids: return None
    poster = driver.execute_script(_IG_POSTER_JS)
    if not poster:
        if og := driver.find_elements(By.XPATH, '//meta[@property="og:image"]'):
            poster = og[0].get_attribute("content") or ""
    return _item(uid, f"IG @{acct}", extract_title(caption), poster, caption,
                 extract_reg_links(caption), url)

def scrape_instagram(seen_ids):
    if not IG_SESSION_ID: print("[IG] IG_SESSION_ID not set, skipping."); return []
    print("[IG] Starting...")
    results, driver = [], _chrome()
    try:
        driver.get("https://www.instagram.com/"); time.sleep(3)
        driver.add_cookie({"name": "sessionid", "value": IG_SESSION_ID, "domain": ".instagram.com"})
        driver.refresh(); time.sleep(5)
        if "login" in driver.current_url.lower(): print("[IG] Invalid session."); return []
        for acct in IG_ACCOUNTS:
            for url in _ig_posts(driver, acct)[:MAX_IG]:
                try:
                    it = _ig_post(driver, url, acct, seen_ids)
                    if it: results.append(it); seen_ids.add(it["id"])
                except Exception as e: print(f"[IG] Skip {url}: {e}")
    except Exception as e: print(f"[IG] Error: {e}")
    finally: driver.quit()
    print(f"[IG] Done: {len(results)} items"); return results

# ── Deduplication ─────────────────────────────────────────────────────────
def _tok(title):
    return {t for t in re.sub(r"[^\w\s]", " ", (title or "").lower()).split()
            if t not in STOPWORDS and len(t) > 1}

def _jac(a, b): return len(a & b) / len(a | b) if a and b else 0.0

def dedup(items, db_data, thr=0.6):
    db_urls = {d["link_direct"] for d in db_data if d.get("link_direct")}
    db_toks = [_tok(d["judul"]) for d in db_data if d.get("judul")]
    uniq = []
    for it in items:
        it["judul"] = clean_title(it.get("judul", ""))
        it["link_pendaftaran"] = list(dict.fromkeys(
            u for raw in it.get("link_pendaftaran", []) if (u := clean_url(raw))))
        tok = _tok(it.get("judul", ""))
        lk = it.get("link_direct", "")
        if lk and lk in db_urls: continue
        if any(_jac(tok, dt) >= thr for dt in db_toks): continue
        di = next((i for i, ex in enumerate(uniq)
                    if _jac(tok, _tok(ex.get("judul", ""))) >= thr), None)
        if di is not None:
            ex = uniq[di]
            merged = list(dict.fromkeys(ex.get("link_pendaftaran", []) + it.get("link_pendaftaran", [])))
            if SRC_PRIO.get(it["sumber"], 2) < SRC_PRIO.get(ex["sumber"], 2):
                it["link_pendaftaran"] = merged; uniq[di] = it
            else:
                uniq[di]["link_pendaftaran"] = merged
        else:
            uniq.append(it)
    print(f"[DEDUP] {len(items)} -> {len(uniq)} unique"); return uniq

# ── Entry point ───────────────────────────────────────────────────────────
async def main():
    if not MONGO_URI: raise RuntimeError("MONGO_URI is not set.")
    print("[INFO] Connecting to MongoDB...")
    client = pymongo.MongoClient(MONGO_URI)
    col = client[DB_NAME][COLLECTION]
    db_data = list(col.find({}, {"id": 1, "link_direct": 1, "judul": 1, "_id": 0}))
    seen = {d["id"] for d in db_data if "id" in d}
    print(f"[INFO] {len(seen)} existing records in DB.")

    batches = await asyncio.gather(
        asyncio.to_thread(scrape_infolomba, seen),
        scrape_silomba(seen),
        asyncio.to_thread(scrape_instagram, seen),
    )
    raw = [it for b in batches if isinstance(b, list) for it in b]
    print(f"[INFO] {len(raw)} new raw items found.")
    if not raw: print("[INFO] No new data."); client.close(); return

    processed = []
    for i in range(0, len(raw), 15):
        batch = raw[i:i+15]
        print(f"[LLM] Batch {i//15+1} ({len(batch)} items)...")
        processed.extend(gemini_process(batch))

    final = dedup(processed, db_data)
    if not final: print("[INFO] All duplicates."); client.close(); return
    r = col.bulk_write([UpdateOne({"id": it["id"]}, {"$set": it}, upsert=True) for it in final])
    print(f"[INFO] Saved: {r.upserted_count} new, {r.modified_count} updated.")
    client.close()

if __name__ == "__main__":
    asyncio.run(main())