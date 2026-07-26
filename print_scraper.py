#!/usr/bin/env python3
"""
print_scraper.py — fetch PRINT edition articles for ST, BT, and Berita Harian
(BH) from the Meltwater "consolidated.json" newspaper feed, looked up by
(source, date, title) — instead of scraping a live URL like news_scraper.py.

This is a Python port of the "GetPrint" Google Apps Script (Meltwater article
fetcher for Google Sheets), so it can be used from the same web UI / CLI as
the rest of this project.

Supported sources: BT (Business Times), ST (Straits Times), BH (Berita Harian),
LHZB (Lianhe Zaobao), SM/SMDN (Shin Min Daily News), TM (Tamil Murasu) — all
confirmed live on the Meltwater consolidated.json feed.

Usage:
    python print_scraper.py "ST, 24/07/2026, Some headline here"
    python print_scraper.py --file rows.txt   # one "Source, DD/MM/YYYY, Title" row per line
    python print_scraper.py --file rows.txt --out results.json
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Optional

import requests

MELTWATER_BASE_URL = "https://meltwaternews.com/ext/ftp_md/Newspaper_JSON"
SOURCE_LABELS = {
    "BT": "Business Times",
    "ST": "Straits Times",
    "BH": "Berita Harian",
    "LHZB": "Lianhe Zaobao",
    "SM": "Shin Min Daily News",
    "SMDN": "Shin Min Daily News",
    "TM": "Tamil Murasu",
}
SUPPORTED_SOURCES = list(SOURCE_LABELS.keys())
EXCLUDED_SOURCES = []  # nothing excluded — all six sources above are live on this feed

# The Meltwater feed's URL folder name doesn't always match the code used in
# the Sheet's Newspaper column — Shin Min's actual folder is "SMDN" even
# though the sheet (or a person typing) may use "SM". Map any input code to
# the real feed folder here; anything not listed just uses itself.
SOURCE_URL_SEGMENTS = {
    "SM": "SMDN",
}


def _url_segment(source: str) -> str:
    return SOURCE_URL_SEGMENTS.get(source, source)


@dataclass
class PrintArticleResult:
    source: str
    date: str
    title: str
    url: str = ""
    headline: Optional[str] = None
    subhead: Optional[str] = None
    author: Optional[str] = None
    body: Optional[str] = None
    word_count: int = 0
    error: Optional[str] = None
    error_type: Optional[str] = None


# ============================================================
# URL / DATE HELPERS
# ============================================================

def build_meltwater_url(source: str, date_str: str) -> str:
    return f"{MELTWATER_BASE_URL}/{source}/{date_str}/consolidated.json"


def format_date_to_yyyymmdd(date_val: str) -> str:
    """Accepts 'DD/MM/YYYY' (or 'D/M/YYYY') and returns 'YYYYMMDD'. Raises ValueError."""
    date_val = (date_val or "").strip()
    if not date_val:
        raise ValueError("Date is empty.")
    parts = date_val.split("/")
    if len(parts) != 3:
        raise ValueError(f"Expected DD/MM/YYYY format but got: '{date_val}'")
    try:
        day, month, year = (int(p) for p in parts)
    except ValueError:
        raise ValueError(f"Date parts are not numbers: '{date_val}'")
    if not (1 <= month <= 12):
        raise ValueError(f"Invalid month ({month}) in date: '{date_val}'")
    if not (1 <= day <= 31):
        raise ValueError(f"Invalid day ({day}) in date: '{date_val}'")
    if not (2000 <= year <= 2100):
        raise ValueError(f"Year out of range ({year}) in date: '{date_val}'")
    return f"{year:04d}{month:02d}{day:02d}"


# ============================================================
# TITLE MATCHING (strict, normalized — same rules as the Apps Script)
# ============================================================

_SMART_SINGLE = re.compile(r"[\u2018\u2019\u201A\u2032]")
_SMART_DOUBLE = re.compile(r"[\u201C\u201D\u201E\u2033]")
_DASHES = re.compile(r"[\u2013\u2014\u2015]")
_ELLIPSIS = re.compile(r"\u2026")
_WS = re.compile(r"\s+")


def normalize_for_comparison(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.strip().lower()
    text = _SMART_SINGLE.sub("'", text)
    text = _SMART_DOUBLE.sub('"', text)
    text = _DASHES.sub("-", text)
    text = _ELLIPSIS.sub("...", text)
    text = _WS.sub(" ", text)
    return text


def find_article_by_title(articles: dict, title: str) -> Optional[dict]:
    normalized_title = normalize_for_comparison(title)
    if not normalized_title:
        return None
    for article in articles.values():
        if not isinstance(article, dict):
            continue
        headline = article.get("headline")
        if not headline:
            continue
        if normalize_for_comparison(headline) == normalized_title:
            return article
    return None


# ============================================================
# JSON FETCHER
# ============================================================

def fetch_and_parse_json(url: str, timeout: int = 30):
    """Returns (data, error_dict). error_dict is None on success, else
    {"error": "...", "error_type": "FETCH"|"PARSE"}."""
    try:
        resp = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout)
    except requests.RequestException as e:
        return None, {"error": f"Network/fetch failed: {e}", "error_type": "FETCH"}

    if resp.status_code == 404:
        return None, {
            "error": f"JSON file not found (HTTP 404). Source/date may not exist: {url}",
            "error_type": "FETCH",
        }
    if resp.status_code == 403:
        return None, {"error": f"Access denied (HTTP 403). Check permissions for: {url}", "error_type": "FETCH"}
    if resp.status_code >= 500:
        return None, {"error": f"Server error (HTTP {resp.status_code}). Try again later.", "error_type": "FETCH"}
    if resp.status_code != 200:
        return None, {"error": f"Unexpected HTTP status {resp.status_code} for: {url}", "error_type": "FETCH"}

    content = resp.text
    if not content or not content.strip():
        return None, {"error": f"Response body is empty for: {url}", "error_type": "PARSE"}

    try:
        return json.loads(content), None
    except json.JSONDecodeError as e:
        return None, {"error": f"JSON parsing failed: {e}", "error_type": "PARSE"}


# ============================================================
# HTML STRIPPING / TEXT CLEANUP
# ============================================================

_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&#x27;": "'", "&#x2F;": "/", "&apos;": "'",
    "&nbsp;": " ", "&ndash;": "\u2013", "&mdash;": "\u2014",
    "&lsquo;": "\u2018", "&rsquo;": "\u2019", "&ldquo;": "\u201C",
    "&rdquo;": "\u201D", "&hellip;": "\u2026",
}
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    if not html:
        return ""
    text = _TAG_RE.sub("", html)
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    return text


def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", " ").replace("\n", " ")
    text = _WS.sub(" ", text)
    return text.strip()


# ============================================================
# STORY_XML PARSER
# ============================================================

_IMG_BLOCK_NESTED_RE = re.compile(
    r'<div[^>]*class="[^"]*article-in-image[^"]*"[^>]*>'
    r'(?:(?!<div[^>]*class="[^"]*article-in-image)[\s\S])*?</div>\s*</div>\s*</div>',
    re.IGNORECASE,
)
_IMG_BLOCK_SIMPLE_RE = re.compile(
    r'<div[^>]*class="[^"]*article-in-image[^"]*"[^>]*>(?:(?!<div)[\s\S])*?</div>',
    re.IGNORECASE,
)
_IMG_CAPTION_RE = re.compile(r'<div[^>]*class="[^"]*articleImageCaption[^"]*"[^>]*>[\s\S]*?</div>', re.IGNORECASE)
_IMG_CREDIT_RE = re.compile(r'<div[^>]*class="[^"]*articleImageCredit[^"]*"[^>]*>[\s\S]*?</div>', re.IGNORECASE)
_BYLINE_P_RE = re.compile(r'<p[^>]*class="[^"]*article-byline[^"]*"[^>]*>[\s\S]*?</p>', re.IGNORECASE)
_P_TAG_RE = re.compile(r'<p[^>]*class="([^"]*)"[^>]*>([\s\S]*?)</p>', re.IGNORECASE)


_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")
_FOOTER_PATTERNS_RE = re.compile(
    r"see\s+the\s+big\s+story|see\s+also|laporan\s+lanjut|baca\s+lanjut",
    re.IGNORECASE,
)


def _is_footer_paragraph(text: str) -> bool:
    """Trailing cross-reference / continuation notes that aren't part of the
    article, e.g. a bare byline email left in the body, 'SEE THE BIG STORY
    * A2, BUSINESS * A16' page pointers, or Malay 'Laporan lanjut' / 'Baca
    lanjut' continuation notes."""
    if not text:
        return False
    stripped = text.strip()
    if stripped.startswith(("\u25b6", "\u25ba")):  # ▶ / ►
        return True
    if _FOOTER_PATTERNS_RE.search(stripped):
        return True
    # A paragraph that, once its email address is stripped out, has nothing
    # (or just leftover bullets/dashes) left is a bare byline email, not
    # real article text.
    without_email = _EMAIL_RE.sub("", stripped).strip(" \u2022-\u2013\u2014.,;:")
    if _EMAIL_RE.search(stripped) and len(without_email) < 3:
        return True
    return False


def parse_story_xml(xml: Optional[str]) -> list:
    """Returns an ordered list of {"type": "body"|"subheader", "text": str},
    skipping image captions/credits, bylines, footer/cross-reference notes,
    and any other <p> class."""
    if not xml:
        return []
    xml = _IMG_BLOCK_NESTED_RE.sub("", xml)
    xml = _IMG_BLOCK_SIMPLE_RE.sub("", xml)
    xml = _IMG_CAPTION_RE.sub("", xml)
    xml = _IMG_CREDIT_RE.sub("", xml)
    xml = _BYLINE_P_RE.sub("", xml)

    parts = []
    for m in _P_TAG_RE.finditer(xml):
        classes = m.group(1) or ""
        raw_content = m.group(2) or ""
        text = clean_text(strip_html(raw_content))
        if not text:
            continue
        if "article-subhead" in classes:
            parts.append({"type": "subheader", "text": text})
        elif "article-full-body" in classes:
            if _is_footer_paragraph(text):
                continue
            parts.append({"type": "body", "text": text})
        # any other class (byline, caption, credit, etc.) is skipped
    return parts


# Generic desk/publication bylines that Meltwater sometimes fills in when
# there's no actual named author (e.g. the byline field just says "Business
# Times" or "Newsdesk") — these aren't real bylines and should be treated
# the same as no author at all, so formatting goes straight to the body.
_GENERIC_BYLINE_RE = re.compile(
    r"^(the\s+)?(business times|straits times|berita harian|lianhe zaobao|"
    r"shin min daily news|tamil murasu|st|bt|bh|lhzb|sm|smdn|tm|"
    r"[a-z\s]*\b(desk|newsroom|wires|bureau|correspondents?|editorial team|"
    r"news team)\b)$",
    re.IGNORECASE,
)

# Same idea as _GENERIC_BYLINE_RE above, but for the Chinese-language feeds
# (LHZB, SM/SMDN) — Meltwater sometimes fills the byline with a generic
# wire/desk label instead of a real reporter's name (e.g. "本报讯", "综合
# 报道", "联合早报"). Without this, those placeholder labels would slip
# through and get printed where the author's name should be. If your feed
# uses a placeholder not listed here, let Claude know the exact text so it
# can be added.
_GENERIC_BYLINE_ZH_RE = re.compile(
    r"^(本报讯|本报记者|本报综合|综合报道|综合|编辑部|新闻中心|采访\s*[/／]?\s*报道|"
    r"记者|特约记者|通讯员|联合早报|联合早报讯|来源|讯)$"
)


def extract_author(article: dict) -> str:
    byline = article.get("byline")
    if not byline or not isinstance(byline, list) or not byline:
        return ""
    first = byline[0]
    if not isinstance(first, dict):
        return ""
    name = str(first.get("name") or "")
    name = re.sub(r"\S+@\S+\.\S+", "", name).strip()   # strip emails
    name = re.sub(r"^by\s+", "", name, flags=re.IGNORECASE).strip()  # strip "By "
    # Meltwater sometimes appends the author's title/position after the name
    # (e.g. "Jane Tan, Senior Correspondent" or "Jane Tan | China Bureau
    # Chief") — keep only the actual name, before that separator.
    name = re.split(r"\s*[,|]\s*", name, maxsplit=1)[0].strip()
    if not name:
        return ""
    if _GENERIC_BYLINE_RE.match(name) or _GENERIC_BYLINE_ZH_RE.match(name):
        return ""
    # Leftover punctuation/whitespace-only "names" (e.g. a bare "-" or "—"
    # left after stripping an email) aren't a real byline either.
    if not re.search(r"[^\W\d_]", name, flags=re.UNICODE):
        return ""
    return name


# English-language sources — headline is excluded for these (per Aldrin's
# format rule). Everything else (BH/Malay, LHZB & SM/SMDN/Chinese, TM/Tamil) keeps
# the headline as the first line.
ENGLISH_SOURCES = {"BT", "ST"}


def format_article(article: dict, source: str = "") -> str:
    """Format rule:
      - English (BT, ST): Author / Body [/ Subheader / Body ...]
      - Chinese, Malay, Tamil (BH, LHZB, SM, TM): Headline / Author / Body [/ Subheader / Body ...]
    The top-level 'subhead' (deck) field is always excluded — only the
    mid-body subheaders inside story_xml are kept, glued to the paragraph
    that follows them.

    For ST/BT specifically: if the very first piece of story_xml content is
    itself a subheader, it would land right under the author line and read
    like a second headline/subheadline directly after the byline — so that
    leading subheader is dropped for these two sources. It's still kept if
    it shows up later, in between body paragraphs."""
    output = []

    is_english = (source or "").strip().upper() in ENGLISH_SOURCES

    if not is_english:
        headline = clean_text(article.get("headline") or "")
        if headline:
            output.append(headline)
            output.append("")

    author = extract_author(article)
    if author:
        output.append(author)
        output.append("")

    parts = parse_story_xml(article.get("story_xml") or "")
    if is_english and parts and parts[0]["type"] == "subheader":
        parts = parts[1:]

    for part in parts:
        if part["type"] == "subheader":
            output.append(part["text"])
        else:
            output.append(part["text"])
            output.append("")

    while output and output[-1] == "":
        output.pop()

    return "\n".join(output)


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def _lookup_and_format(source: str, date_val: str, title: str,
                        json_cache: Optional[dict] = None):
    """Core shared logic: resolve date -> fetch/parse JSON -> match title ->
    format. Returns (fields_dict, error_dict, url) — exactly one of
    fields_dict/error_dict is not None. fields_dict has headline/subhead/
    author/body/word_count. error_dict has 'error' and 'error_type'."""
    try:
        date_str = format_date_to_yyyymmdd(date_val)
    except ValueError as e:
        return None, {"error": str(e), "error_type": "DATE"}, ""

    url = build_meltwater_url(_url_segment(source), date_str)

    cache_key = f"{source}_{date_str}"
    if json_cache is not None and cache_key in json_cache:
        articles, fetch_err = json_cache[cache_key]
    else:
        articles, fetch_err = fetch_and_parse_json(url)
        if json_cache is not None:
            json_cache[cache_key] = (articles, fetch_err)

    if fetch_err:
        return None, fetch_err, url

    if not isinstance(articles, dict) or not articles:
        return None, {"error": f"JSON is empty or invalid for {source} {date_str}", "error_type": "PARSE"}, url

    article = find_article_by_title(articles, title)
    if not article:
        return None, {"error": f"Title not matched in {source} {date_str} JSON.", "error_type": "NOT_FOUND"}, url

    if not article.get("story_xml") or not str(article["story_xml"]).strip():
        return None, {
            "error": "Article matched but story_xml is empty. May be a teaser/headline-only entry.",
            "error_type": "EMPTY_BODY",
        }, url

    try:
        formatted = format_article(article, source)
    except Exception as e:
        return None, {"error": f"Article found but formatting failed: {e}", "error_type": "FORMAT"}, url

    if not formatted or not formatted.strip():
        return None, {
            "error": "Article formatted but result is empty. story_xml may only contain images/captions.",
            "error_type": "EMPTY_BODY",
        }, url

    fields = {
        "headline": clean_text(article.get("headline") or "") or None,
        "subhead": clean_text(article.get("subhead") or "") or None,
        "author": extract_author(article) or None,
        "body": formatted,
        "word_count": len(formatted.split()),
    }
    return fields, None, url


# ============================================================
# SINGLE-ROW ENTRY POINT (Source, Date, Title)
# ============================================================

def prefetch_json_cache(rows, max_workers: int = 4) -> dict:
    """Pre-fetch every distinct (source, date) consolidated.json page a
    batch of PrintTableRow will need, concurrently, and return a ready-made
    json_cache dict for fetch_table_row()/fetch_print_article(). A batch of
    many pasted rows usually only touches a handful of distinct dates —
    fetching those few pages in parallel up front (instead of one row at a
    time, each waiting on the network in turn) is what lets a large batch
    finish inside a hosting platform's request timeout."""
    unique_sources = {}
    for row in rows:
        source = (getattr(row, "source", "") or "").strip().upper()
        date_val = (getattr(row, "article_date", "") or "").strip()
        if not source or not date_val:
            continue
        if source in EXCLUDED_SOURCES or source not in SUPPORTED_SOURCES:
            continue
        try:
            date_str = format_date_to_yyyymmdd(date_val)
        except ValueError:
            continue
        key = f"{source}_{date_str}"
        unique_sources.setdefault(key, (source, date_str))

    json_cache = {}
    if not unique_sources:
        return json_cache

    def _fetch(item):
        key, (source, date_str) = item
        url = build_meltwater_url(_url_segment(source), date_str)
        return key, fetch_and_parse_json(url)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for key, result in executor.map(_fetch, unique_sources.items()):
            json_cache[key] = result

    return json_cache


def fetch_print_article(source: str, date_val: str, title: str,
                         delay: float = 0.0, json_cache: Optional[dict] = None) -> PrintArticleResult:
    """Fetch + format one print article. json_cache (dict) lets a batch of
    rows share one JSON download per (source, date) pair, same as the Apps
    Script's per-run cache."""
    if delay:
        time.sleep(delay)

    source = (source or "").strip().upper()
    date_val = (date_val or "").strip()
    title = (title or "").strip()
    result = PrintArticleResult(source=source, date=date_val, title=title)

    missing = []
    if not date_val:
        missing.append("Date")
    if not source:
        missing.append("Newspaper")
    if not title:
        missing.append("Title")
    if missing:
        result.error = "Required: " + ", ".join(missing)
        result.error_type = "MISSING"
        return result

    if source in EXCLUDED_SOURCES:
        result.error = f"Source '{source}' is excluded (not on this feed)."
        result.error_type = "SKIPPED"
        return result

    if source not in SUPPORTED_SOURCES:
        result.error = f"Source '{source}' not supported. Only {', '.join(SUPPORTED_SOURCES)}."
        result.error_type = "SKIPPED"
        return result

    fields, err, url = _lookup_and_format(source, date_val, title, json_cache)
    result.url = url
    if err:
        result.error = err["error"]
        result.error_type = err["error_type"]
        return result

    result.headline = fields["headline"]
    result.subhead = fields["subhead"]
    result.author = fields["author"]
    result.body = fields["body"]
    result.word_count = fields["word_count"]
    return result


def parse_row(line: str):
    """Parse one 'Source, DD/MM/YYYY, Title' (comma- or tab-separated) input
    line into (source, date, title). Returns None for a blank line."""
    line = line.rstrip("\n").strip()
    if not line:
        return None
    parts = line.split("\t", 2) if "\t" in line else line.split(",", 2)
    parts = [p.strip() for p in parts]
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


# ============================================================
# TABLE-PASTE ENTRY POINT (Publish Date, Article Date, Newspaper,
# Article Type, Section, Title, URL — i.e. columns A-G pasted straight
# from the Google Sheet, tab-separated)
# ============================================================

@dataclass
class PrintTableRow:
    publish_date: str = ""
    article_date: str = ""
    source: str = ""
    article_type: str = ""
    section: str = ""
    title: str = ""
    url: str = ""
    headline: Optional[str] = None
    subhead: Optional[str] = None
    author: Optional[str] = None
    body: Optional[str] = None
    word_count: int = 0
    status: str = ""  # "ok" | "skipped" | "error"
    error: Optional[str] = None
    error_type: Optional[str] = None


_TABLE_COLUMNS = 7  # Publish Date, Article Date, Newspaper, Article Type, Section, Title, URL


def parse_table_paste(text: str) -> list:
    """Parse a block of tab-separated rows (as pasted straight out of Google
    Sheets, columns A-G) into a list of PrintTableRow. Blank lines are
    skipped. Missing trailing columns are padded with ''."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split("\t")]
        while len(cells) < _TABLE_COLUMNS:
            cells.append("")
        publish_date, article_date, source, article_type, section, title, url = cells[:_TABLE_COLUMNS]
        rows.append(PrintTableRow(
            publish_date=publish_date,
            article_date=article_date,
            source=source.upper(),
            article_type=article_type,
            section=section,
            title=title,
            url=url,
        ))
    return rows


def fetch_table_row(row: PrintTableRow, delay: float = 0.0,
                     json_cache: Optional[dict] = None) -> PrintTableRow:
    """Fills in headline/body/etc on a PrintTableRow in place (also returns
    it). Mirrors the Apps Script's 'only process Print type, skip TM
    silently' behaviour."""
    if delay:
        time.sleep(delay)

    article_type_norm = (row.article_type or "").strip().lower()
    if article_type_norm and article_type_norm != "print":
        row.status = "skipped"
        row.error = f"Article Type is '{row.article_type}', not Print — skipped."
        row.error_type = "NOT_PRINT"
        return row

    missing = []
    if not row.article_date:
        missing.append("Article Date")
    if not row.source:
        missing.append("Newspaper")
    if not row.title:
        missing.append("Title")
    if missing:
        row.status = "error"
        row.error = "Required: " + ", ".join(missing)
        row.error_type = "MISSING"
        return row

    if row.source in EXCLUDED_SOURCES:
        row.status = "skipped"
        row.error = f"Source '{row.source}' is excluded (not on this feed)."
        row.error_type = "SKIPPED"
        return row

    if row.source not in SUPPORTED_SOURCES:
        row.status = "error"
        row.error = f"Source '{row.source}' not supported. Only {', '.join(SUPPORTED_SOURCES)}."
        row.error_type = "SKIPPED"
        return row

    fields, err, url = _lookup_and_format(row.source, row.article_date, row.title, json_cache)
    if not row.url:
        row.url = url
    if err:
        row.status = "error"
        row.error = err["error"]
        row.error_type = err["error_type"]
        return row

    row.headline = fields["headline"]
    row.subhead = fields["subhead"]
    row.author = fields["author"]
    row.body = fields["body"]
    row.word_count = fields["word_count"]
    row.status = "ok"
    return row


def main():
    ap = argparse.ArgumentParser(
        description="Fetch PRINT edition articles for ST/BT/BH from Meltwater's consolidated JSON feed."
    )
    ap.add_argument("rows", nargs="*", help="Rows like 'ST, 24/07/2026, Some headline'")
    ap.add_argument("--file", help="Path to a text file, one 'Source, DD/MM/YYYY, Title' row per line")
    ap.add_argument("--out", help="Write results as JSON to this path")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between requests (default 1.0)")
    args = ap.parse_args()

    raw_rows = list(args.rows)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            raw_rows.extend(line for line in f if line.strip())

    if not raw_rows:
        ap.error("Provide at least one row, or --file with a list of rows.")

    json_cache = {}
    results = []
    for i, raw in enumerate(raw_rows):
        parsed = parse_row(raw)
        if not parsed:
            continue
        source, date_val, title = parsed
        result = fetch_print_article(
            source, date_val, title,
            delay=args.delay if i > 0 else 0,
            json_cache=json_cache,
        )
        results.append(result)

        print(f"\n{'=' * 80}\n{result.source} | {result.date} | {result.title}")
        if result.error:
            print(f"  [{result.error_type}] {result.error}")
            continue
        print(f"Headline:   {result.headline}")
        print(f"Author:     {result.author}")
        print(f"Word count: {result.word_count}")
        print("-" * 80)
        print(result.body)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(results)} result(s) to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
