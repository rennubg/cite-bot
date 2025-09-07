# === Clean citation bot: RMIT Harvard (Web + DOI), auto-detect, with helpful errors ===
import os, re, datetime, json, logging, html
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, Defaults, filters
)

from flask import Flask
from threading import Thread

# --- tiny keep-alive web server for Replit/UptimeRobot ---
webapp = Flask(__name__)

@webapp.get("/")
def _home():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 8080))  # Replit gives PORT automatically
    webapp.run(host="0.0.0.0", port=port)

# ---------- config ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
socket_timeout_seconds = 12  # network timeout

# Load token
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ---------- small utilities ----------
def html_escape(s: str | None) -> str:
    return html.escape(s or "", quote=False)

def today_dmy():
    """Day Month Year with Windows-friendly format."""
    fmt = "%#d %B %Y" if os.name == "nt" else "%-d %B %Y"
    return datetime.datetime.now().strftime(fmt)

def format_person_name(name):
    """
    Turn 'Greta Thunberg' -> 'Thunberg, G.'.
    If it looks like an organisation ('University', 'Ltd', '&', etc.), keep as-is.
    If already 'Last, F.' style, keep.
    """
    if not name:
        return None
    n = name.strip()
    lowered = n.lower()
    org_markers = [" inc", " ltd", " llc", " pte", "&", " company", " university", " gov", " ministry", " press", " bureau", " office", " department"]
    if "," in n or any(m in lowered for m in org_markers):
        return n
    parts = [p for p in n.split() if p]
    if len(parts) < 2:
        return n
    last = parts[-1]
    initials = " ".join([p[0].upper() + "." for p in parts[:-1] if p and p[0].isalpha()])
    return f"{last}, {initials}"

# ---------- HTTP fetch ----------
def safe_get(url: str, timeout=socket_timeout_seconds):
    # Stronger headers so more sites respond (esp. gov/org/uni)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; ARM64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "close",
    }
    return requests.get(url, headers=headers, timeout=timeout)

# ---------- HTML meta helpers ----------
def extract_meta(soup, *selectors):
    for name, attr in selectors:
        tag = soup.find("meta", {attr: name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None

def extract_meta_all(soup, name, attr="name"):
    vals = []
    for tag in soup.find_all("meta", {attr: name}):
        c = tag.get("content")
        if c:
            vals.append(c.strip())
    return vals

def parse_year(s):
    if not s:
        return None
    try:
        dt = dateparser.parse(s, fuzzy=True)
        if dt:
            return dt.year
    except Exception:
        pass
    m = re.search(r"(19|20)\d{2}", s)
    return int(m.group(0)) if m else None

# ---------- JSON-LD grab ----------
def from_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type", "")
            types = [t.lower()] if isinstance(t, str) else [x.lower() for x in t] if isinstance(t, list) else []
            if any(x in ("article","newsarticle","blogposting","webpage") for x in types) or obj.get("headline") or obj.get("name"):
                title = obj.get("headline") or obj.get("name")
                pub = obj.get("publisher")
                site = (pub or {}).get("name") if isinstance(pub, dict) else pub
                # author can be dict or list
                author = obj.get("author")
                if isinstance(author, list) and author:
                    a0 = author[0]
                    author_name = a0.get("name") if isinstance(a0, dict) else str(a0)
                elif isinstance(author, dict):
                    author_name = author.get("name")
                else:
                    author_name = author if isinstance(author, str) else None
                year = parse_year(obj.get("datePublished") or obj.get("dateModified"))
                return title, site, author_name, year
    return None, None, None, None

# ---------- DOI / Crossref ----------
def extract_doi(text: str):
    """Return DOI like 10.xxxx/xxxxx from raw text or DOI URL."""
    if not text:
        return None
    m = re.search(r'(10\.\d{4,9}/[^\s<>"]+)', text)
    return m.group(1) if m else None

def author_list_from_crossref(authors):
    """Crossref dicts -> 'Last, F. M., ... & Last, F.'"""
    names = []
    for a in authors or []:
        family = (a.get("family") or "").strip()
        given  = (a.get("given") or "").strip()
        if family and given:
            names.append(f"{given} {family}")
        elif family:
            names.append(family)
        elif given:
            names.append(given)
    if not names:
        return None
    formatted = [format_person_name(n) for n in names]
    return formatted[0] if len(formatted) == 1 else ", ".join(formatted[:-1]) + " & " + formatted[-1]

def cite_from_crossref(doi: str):
    """Fetch metadata for a DOI."""
    url = f"https://api.crossref.org/works/{doi}"
    r = requests.get(url, timeout=socket_timeout_seconds, headers={"User-Agent": "RMIT-Harvard-CiteBot/1.0"})
    r.raise_for_status()
    item = (r.json().get("message") or {})

    title = " ".join(item.get("title") or []) or None

    # year
    year = None
    for k in ("published-print", "published-online", "issued"):
        dp = (item.get(k) or {}).get("date-parts")
        if isinstance(dp, list) and dp and isinstance(dp[0], list) and dp[0]:
            year = dp[0][0]; break

    author_display = author_list_from_crossref(item.get("author"))
    publisher = (item.get("publisher") or "").strip() or None
    container = " ".join(item.get("container-title") or []) or None  # journal/series
    volume = (item.get("volume") or "").strip() or None
    issue  = (item.get("issue") or "").strip() or None
    pages  = (item.get("page") or "").strip() or None
    series = item.get("number") or None
    typ    = (item.get("type") or "").lower()

    return {
        "title": title,
        "year": year,
        "author_display": author_display,
        "publisher": publisher,
        "container": container,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "series": series,
        "type": typ,
        "doi": doi,
        "doi_url": f"https://doi.org/{doi}",
    }

# ---------- site-specific hints ----------
def detect_nber_wp(url):
    m = re.search(r"/papers/w(\d+)", url or "")
    return m.group(1) if m else None

# ---------- scraping for normal websites ----------
def scrape_citation_bits(url):
    r = safe_get(url); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # JSON-LD first
    t2, s2, a2, y2 = from_jsonld(soup)

    # Highwire/GS meta (academic)
    title_hw = extract_meta(soup, ("citation_title","name")) or None
    authors_hw = extract_meta_all(soup, "citation_author")
    doi_meta  = extract_meta(soup, ("citation_doi","name"))
    year_hw = parse_year(
        extract_meta(soup, ("citation_publication_date","name")) or
        extract_meta(soup, ("citation_date","name"))
    )

    title = t2 or title_hw or extract_meta(soup, ("og:title","property"), ("twitter:title","name")) \
            or (soup.title.string.strip() if soup.title and soup.title.string else None)
    site_name = s2 or extract_meta(soup, ("og:site_name","property"))
    author_meta = None
    if isinstance(a2, str):
        author_meta = a2
    elif authors_hw:
        author_meta = authors_hw[0]
    else:
        author_meta = extract_meta(soup, ("author","name"), ("article:author","property"))

    host = urlparse(url).netloc
    author_display = format_person_name(author_meta or site_name or host)

    date_raw = (y2 and str(y2)) or extract_meta(soup, ("article:published_time","property"),
                                                ("og:updated_time","property"),
                                                ("datePublished","itemprop"),
                                                ("date","name"),
                                                ("pubdate","name"))
    year = y2 or year_hw or parse_year(date_raw)

    return {
        "title": title,
        "site_name": site_name,
        "author_display": author_display,
        "year": year,
        "doi_meta": doi_meta,
        "nber_no": detect_nber_wp(url),
    }

# ---------- RMIT Harvard formatters ----------
def build_rmit_web(author_display, year, title, site_name, url):
    """
    Web page:
    Author/Org (Year) Title of document, Site name website, accessed Day Month Year. URL.
    Title in italics (HTML <i>...)
    """
    y = f"({year})" if year else "(n.d.)"
    t = html_escape(title or url)
    s = html_escape(site_name or urlparse(url).netloc)
    accessed = today_dmy()
    return f"{html_escape(author_display)} {y} <i>{t}</i>, {s} website, accessed {accessed}. {html_escape(url)}."

def build_rmit_working_paper(author_display, year, title, series, publisher, url):
    """
    Working paper/report:
    Author(s) (Year) Title, Series/Number, Publisher, accessed Day Month Year. URL.
    Title in italics.
    """
    y = f"({year})" if year else "(n.d.)"
    accessed = today_dmy()
    return f"{html_escape(author_display)} {y} <i>{html_escape(title or url)}</i>, {html_escape(series)}, {html_escape(publisher)}, accessed {accessed}. {html_escape(url)}."

def build_rmit_journal_article(author_display, year, title, journal, volume=None, issue=None, pages=None, url_or_doi=None):
    """
    Journal article (plain-text Harvard with italics on journal):
    Author(s) (Year) 'Article title', <i>Journal Title</i>, volume(issue), pages, accessed Day Month Year. URL/DOI.
    NB: Some RMIT variants italicise the article title instead; we follow common Harvard: journal italicised, title in quotes.
    """
    y = f"({year})" if year else "(n.d.)"
    vol_issue = ""
    if volume and issue:
        vol_issue = f"{volume}({issue})"
    elif volume:
        vol_issue = f"{volume}"
    parts = [p for p in [vol_issue, pages] if p]
    tail = ", ".join(parts) if parts else ""
    acc = today_dmy()
    end = f", accessed {acc}. {html_escape(url_or_doi or '')}".rstrip()
    title_q = html_escape(title or "")
    jour = html_escape(journal or "Journal")
    core = f"{html_escape(author_display)} {y} '{title_q}', <i>{jour}</i>"
    if tail:
        core += f", {tail}"
    core += end if end.endswith(".") else end + "."
    return core

# ---------- in-text ----------
def build_intext(author_display, year):
    y = str(year) if year else "n.d."
    lead = author_display.split(",")[0] if "," in author_display else author_display
    lead = re.sub(r"\s+", " ", lead).strip()
    return f"({html_escape(lead)} {y})"

# ---------- bot commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "Hi! Just paste a <b>URL</b> or a <b>DOI</b> and I’ll return an RMIT Harvard reference + in-text.\n"
        "Tips:\n"
        "• Web pages → I use: <i>Title</i>, Site name website, accessed …\n"
        "• Journal articles (DOI) → I fetch authors/year/title/journal from Crossref.\n"
        "• If a site blocks bots (e.g., ScienceDirect), paste the DOI instead."
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

# (you can still use /citedoi if you like commands)
async def citedoi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = " ".join(context.args).strip()
    doi = extract_doi(arg)
    if not doi:
        await update.message.reply_text("Usage: /citedoi <DOI>\nExample: /citedoi 10.1016/j.marpol.2023.105848")
        return
    try:
        meta = cite_from_crossref(doi)
        # Decide template: journal (has container) vs report (has series) vs web fallback
        if meta["container"] and (meta["volume"] or meta["pages"]):
            ref = build_rmit_journal_article(
                meta["author_display"] or (meta["publisher"] or "Author"),
                meta["year"], meta["title"], meta["container"], meta["volume"], meta["issue"], meta["pages"], meta["doi_url"]
            )
        elif meta["series"]:
            series = meta["series"]
            publisher = meta["publisher"] or (meta["container"] or "Publisher")
            ref = build_rmit_working_paper(
                meta["author_display"] or publisher, meta["year"], meta["title"], series, publisher, meta["doi_url"]
            )
        else:
            site_name = meta["container"] or meta["publisher"] or "Publisher"
            ref = build_rmit_web(
                meta["author_display"] or site_name, meta["year"], meta["title"], site_name, meta["doi_url"]
            )
        intext = build_intext(meta["author_display"] or (meta["publisher"] or "Author"), meta["year"])
        await update.message.reply_html(f"<b>Reference</b>\n{ref}\n\n<b>In-text</b>\n{intext}")
    except requests.exceptions.RequestException as e:
        await update.message.reply_text(
            f"Could not fetch DOI metadata ({type(e).__name__}). If you have the article URL, paste it, "
            "or use /fix to override fields."
        )

# auto-detect messages: DOI or URL (no /cite needed)
async def auto_cite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    # 1) DOI first
    doi = extract_doi(text)
    if doi:
        try:
            meta = cite_from_crossref(doi)
            if meta["container"] and (meta["volume"] or meta["pages"]):
                ref = build_rmit_journal_article(
                    meta["author_display"] or (meta["publisher"] or "Author"),
                    meta["year"], meta["title"], meta["container"], meta["volume"], meta["issue"], meta["pages"], meta["doi_url"]
                )
            elif meta["series"]:
                series = meta["series"]
                publisher = meta["publisher"] or (meta["container"] or "Publisher")
                ref = build_rmit_working_paper(
                    meta["author_display"] or publisher, meta["year"], meta["title"], series, publisher, meta["doi_url"]
                )
            else:
                site_name = meta["container"] or meta["publisher"] or "Publisher"
                ref = build_rmit_web(
                    meta["author_display"] or site_name, meta["year"], meta["title"], site_name, meta["doi_url"]
                )
            intext = build_intext(meta["author_display"] or (meta["publisher"] or "Author"), meta["year"])
            await update.message.reply_html(f"<b>Reference</b>\n{ref}\n\n<b>In-text</b>\n{intext}")
        except requests.exceptions.RequestException as e:
            await update.message.reply_text(
                f"Could not fetch DOI metadata ({type(e).__name__}). "
                "If you have the article’s web link, paste it; otherwise use /citedoi <DOI> or /fix to override."
            )
        return

    # 2) URL
    if text.lower().startswith("http://") or text.lower().startswith("https://"):
        url = text
        try:
            bits = scrape_citation_bits(url)

            # If page exposes DOI meta, prefer DOI route
            if bits.get("doi_meta"):
                doi2 = extract_doi(bits["doi_meta"])
                if doi2:
                    meta = cite_from_crossref(doi2)
                    if meta["container"] and (meta["volume"] or meta["pages"]):
                        ref = build_rmit_journal_article(
                            meta["author_display"] or (meta["publisher"] or "Author"),
                            meta["year"], meta["title"], meta["container"], meta["volume"], meta["issue"], meta["pages"], meta["doi_url"]
                        )
                    elif meta["series"]:
                        series = meta["series"]
                        publisher = meta["publisher"] or (meta["container"] or "Publisher")
                        ref = build_rmit_working_paper(
                            meta["author_display"] or publisher, meta["year"], meta["title"], series, publisher, meta["doi_url"]
                        )
                    else:
                        site_name = meta["container"] or meta["publisher"] or "Publisher"
                        ref = build_rmit_web(
                            meta["author_display"] or site_name, meta["year"], meta["title"], site_name, meta["doi_url"]
                        )
                    intext = build_intext(meta["author_display"] or (meta["publisher"] or "Author"), meta["year"])
                    await update.message.reply_html(f"<b>Reference</b>\n{ref}\n\n<b>In-text</b>\n{intext}")
                    return

            # NBER working paper tweak
            if bits.get("nber_no"):
                series = f"NBER Working Paper No. {bits['nber_no']}"
                publisher = "National Bureau of Economic Research"
                ref = build_rmit_working_paper(
                    bits["author_display"], bits["year"], bits["title"], series, publisher, url
                )
            else:
                ref = build_rmit_web(
                    bits["author_display"], bits["year"], bits["title"], bits["site_name"], url
                )
            intext = build_intext(bits["author_display"], bits["year"])
            await update.message.reply_html(f"<b>Reference</b>\n{ref}\n\n<b>In-text</b>\n{intext}")
        except requests.exceptions.HTTPError:
            await update.message.reply_text(
                "That site refused the request (HTTP error). If this is a journal article (e.g., ScienceDirect/Wiley), "
                "please paste the DOI (like 10.xxxx/xxxxx) or a doi.org link."
            )
        except requests.exceptions.ConnectionError:
            await update.message.reply_text(
                "Network error trying to reach that site. Check your internet or try again. "
                "If it’s a journal article, paste the DOI instead."
            )
        except requests.exceptions.Timeout:
            await update.message.reply_text(
                "Timed out fetching that page. If it’s an academic article, paste the DOI."
            )
        except Exception as e:
            logging.exception("Unhandled scrape error")
            await update.message.reply_text(
                "Sorry, I couldn’t build a citation for that page. If it’s academic, paste the DOI; "
                "otherwise you can /fix author=... year=... title=... site=..."
            )
        return

# ---------- app wiring ----------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing in .env")
    defaults = Defaults(parse_mode=ParseMode.HTML)  # enables <i> italics, <b> bold
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    # start the keep-alive web server in a background thread
    Thread(target=run_web, daemon=True).start()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("citedoi", citedoi))

    # Auto-detect plain messages (no /cite required)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_cite))

    print("Citation bot running. Paste a URL or DOI in Telegram. Press Ctrl+C here to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
