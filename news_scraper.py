#!/usr/bin/env python3
"""
news_scraper.py — extract headline, author, and clean body text from a news
article URL, filtering out ads, nav, related-links, and other boilerplate.

Works across English, Chinese, Tamil, and Malay articles (e.g. Straits Times,
CNA, Zaobao, Berita Harian, Tamil Murasu, etc).

Usage:
    python news_scraper.py "https://www.straitstimes.com/..." ["https://..." ...]
    python news_scraper.py --file urls.txt
    python news_scraper.py --file urls.txt --out results.json

Requires: trafilatura, langdetect, requests, beautifulsoup4
    pip install trafilatura langdetect requests beautifulsoup4 --break-system-packages
"""

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional

import requests
import trafilatura
from bs4 import BeautifulSoup

try:
    from langdetect import detect as _langdetect
except ImportError:  # pragma: no cover
    _langdetect = None


CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

# Full-width sentence terminators used in Chinese (also common in some Malay/Tamil
# reprints of CJK-sourced text, harmless to include).
CJK_SENTENCE_END = re.compile(r"(?<=[。！？])\s*")

# Latin-script sentence splitter: break after ./!/? when followed by whitespace
# and then a capital letter, digit, or opening quote — a reasonable heuristic for
# English, Malay, and Tamil (which use Latin-style punctuation). Not perfect
# around abbreviations (e.g. "U.S.", "Dr.") but errs toward not over-splitting.
LATIN_SENTENCE_END = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"\'\u2018\u201c\u2019\u201d])')


# Common abbreviations that end in a period but should NOT be treated as a
# sentence boundary (e.g. "Dr. Smith" should stay together).
_ABBREVIATIONS = [
    "Dr", "Mr", "Mrs", "Ms", "Jr", "Sr", "Prof", "Rev", "Gov", "Sen", "Rep",
    "Capt", "Gen", "Adm", "Col", "Sgt", "St", "vs", "etc", "Inc", "Ltd", "Co",
    "No", "U.S", "U.K", "U.N", "E.U",
]
_ABBREV_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in _ABBREVIATIONS) + r")\.",
    flags=re.IGNORECASE,
)
_ABBREV_PLACEHOLDER = "\u0000"


_SUBHEADER_MAX_LEN = 60


def _is_subheader_line(line: str) -> bool:
    """A line counts as a subheader (e.g. 'TINJAUAN', 'OVERVIEW', '背景',
    'பின்னணி') when it's short and has no sentence-ending punctuation. For
    Latin-script languages (English, Malay) it must additionally be entirely
    uppercase; Chinese and Tamil have no letter case, so a short standalone
    line with no internal comma/clause punctuation is treated as a label."""
    line = line.strip()
    if not line or len(line) > _SUBHEADER_MAX_LEN:
        return False
    if re.search(r"[.!?\u3002\uff01\uff1f][\"'\u2019\u201d]?$", line):
        return False
    letters = re.sub(r"[^A-Za-z\u00C0-\u024F]", "", line)
    if letters:
        return letters.isupper()
    # No Latin letters at all (Chinese / Tamil script): treat a short,
    # single-phrase line with no internal punctuation as a subheader label.
    if len(line) <= 12 and not re.search(r"[,\uFF0C\u3001:\uFF1A]", line):
        return True
    return False


def _split_block_into_sentences(flat: str) -> list:
    """Sentence-split a single flattened block of body text (no subheaders)."""
    flat = re.sub(r"[ \t]+", " ", flat).strip()
    if not flat:
        return []
    if CJK_RE.search(flat):
        sentences = CJK_SENTENCE_END.split(flat)
    else:
        # Temporarily hide periods in known abbreviations so they don't get
        # mistaken for sentence-ending punctuation.
        protected = _ABBREV_PATTERN.sub(lambda m: m.group(1) + _ABBREV_PLACEHOLDER, flat)
        parts = LATIN_SENTENCE_END.split(protected)
        sentences = [p.replace(_ABBREV_PLACEHOLDER, ".") for p in parts]
    return [s.strip() for s in sentences if s.strip()]


def split_into_sentences(text: str) -> str:
    """Re-split body text so every sentence becomes its own paragraph (blank
    line between each), regardless of how the source site originally grouped
    its <p> tags. Subheader lines (short, ALL CAPS, no ending punctuation —
    e.g. 'TINJAUAN') are kept isolated on their own line, with body text
    before and after, instead of being merged into the surrounding sentence.
    Works for English, Chinese, Tamil, and Malay."""
    if not text:
        return text

    raw_lines = [ln.strip() for ln in re.split(r"\n+", text) if ln.strip()]
    if not raw_lines:
        return text

    output_parts = []
    current_block = []

    def flush_block():
        if current_block:
            block_text = " ".join(current_block)
            output_parts.extend(_split_block_into_sentences(block_text))
            current_block.clear()

    for line in raw_lines:
        if _is_subheader_line(line):
            flush_block()
            output_parts.append(line)
        else:
            current_block.append(line)
    flush_block()

    return "\n\n".join(output_parts)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

LANG_LABELS = {
    "en": "English",
    "zh-cn": "Chinese",
    "zh-tw": "Chinese",
    "zh": "Chinese",
    "ta": "Tamil",
    "ms": "Malay",
    "id": "Malay",  # langdetect often can't tell Malay/Indonesian apart; close enough
}


@dataclass
class ArticleResult:
    url: str
    language: str
    headline: Optional[str]
    author: Optional[str]
    date: Optional[str]
    body: Optional[str]
    word_count: int
    extraction_method: str
    error: Optional[str] = None


def fetch_html(url: str, timeout: int = 20) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def detect_language(text: str) -> str:
    if not text:
        return "unknown"
    # Quick script-based check first (cheap, reliable for CJK/Tamil)
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    if re.search(r"[\u0b80-\u0bff]", text):
        return "ta"
    # Fall back to langdetect for Latin-script languages (en vs ms)
    if _langdetect:
        try:
            code = _langdetect(text[:2000])
            return code
        except Exception:
            pass
    return "en"


def extract_author_fallback(soup: BeautifulSoup) -> Optional[str]:
    """Try common places sites put author info, roughly most-to-least reliable."""
    # 1. JSON-LD schema.org NewsArticle/Article
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            if isinstance(author, dict):
                name = author.get("name")
                if name:
                    return name.strip()
            elif isinstance(author, list):
                names = [a.get("name") for a in author if isinstance(a, dict) and a.get("name")]
                if names:
                    return ", ".join(names)
            elif isinstance(author, str) and author.strip():
                return author.strip()

    # 2. Meta tags
    meta_candidates = [
        ("meta", {"name": "author"}),
        ("meta", {"property": "article:author"}),
        ("meta", {"name": "sailthru.author"}),
        ("meta", {"name": "byl"}),
    ]
    for tag, attrs in meta_candidates:
        el = soup.find(tag, attrs=attrs)
        if el and el.get("content"):
            return el["content"].strip()

    # 3. Common byline CSS patterns
    byline_selectors = [
        "[class*='byline']", "[class*='author']", "[itemprop='author']",
        "[data-testid*='author']",
    ]
    for sel in byline_selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            # avoid grabbing huge blocks by mistake
            if text and len(text) < 120:
                return re.sub(r"^(by|By|BY)[:\s]+", "", text).strip()

    return None


def extract_body_fallback(soup: BeautifulSoup) -> Optional[str]:
    """Generic largest-text-block heuristic when trafilatura comes up short."""
    for tag in soup(["script", "style", "nav", "aside", "footer", "header",
                      "form", "iframe", "noscript"]):
        tag.decompose()
    # kill obvious ad/related/social containers
    junk_patterns = re.compile(
        r"(advert|sponsor|promo|related|share|social|subscribe|newsletter|"
        r"comment|outbrain|taboola|widget)", re.I
    )
    for el in soup.find_all(attrs={"class": junk_patterns}):
        el.decompose()
    for el in soup.find_all(attrs={"id": junk_patterns}):
        el.decompose()

    candidates = soup.find_all(["article", "div", "section"])
    best, best_len = None, 0
    for c in candidates:
        paras = c.find_all("p", recursive=True)
        text = "\n".join(p.get_text(" ", strip=True) for p in paras)
        if len(text) > best_len:
            best, best_len = text, len(text)
    return best.strip() if best else None


def extract_headline_fallback(soup: BeautifulSoup) -> Optional[str]:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    if soup.title:
        return soup.title.get_text(strip=True)
    return None


def _is_cna_watch_url(url: str) -> bool:
    return bool(re.search(r"channelnewsasia\.com/watch/", url, re.I))


def extract_video_caption(soup: BeautifulSoup) -> Optional[str]:
    """For CNA /watch/ video pages: pull ONLY the video's caption/synopsis
    from the page's description meta tags. Ignores everything else on the
    page (related-video captions, navigation, transcripts, etc.), which is
    what was causing duplicated text when scraping these pages."""
    for tag, attrs in (
        ("meta", {"property": "og:description"}),
        ("meta", {"name": "twitter:description"}),
        ("meta", {"name": "description"}),
    ):
        el = soup.find(tag, attrs=attrs)
        if el and el.get("content"):
            content = el["content"].strip()
            if content:
                return content
    return None


# Known trailing boilerplate blocks to strip out of the body, per outlet.
_BOILERPLATE_PATTERNS = [
    # Business Times newsletter promo, tacked on at the end of articles.
    re.compile(
        r"Decoding Asia newsletter:\s*your guide to navigating Asia in a new "
        r"global order\.\s*Sign up here to get Decoding Asia newsletter\.\s*"
        r"Delivered to your inbox\.\s*Free\.?",
        re.IGNORECASE,
    ),
]


def _strip_boilerplate(body: Optional[str]) -> Optional[str]:
    if not body:
        return body
    for pattern in _BOILERPLATE_PATTERNS:
        body = pattern.sub("", body)
    return body.strip()


def _strip_leading_headline(body: Optional[str], headline: Optional[str]) -> Optional[str]:
    """ST/BT (and similar) sometimes repeat the headline verbatim as the
    first line of the extracted body. Drop it so the body actually starts
    at the first real paragraph."""
    if not body or not headline:
        return body
    headline_clean = headline.strip()
    if not headline_clean:
        return body
    body_stripped = body.lstrip()
    match = re.match(re.escape(headline_clean), body_stripped, re.IGNORECASE)
    if match:
        remainder = body_stripped[match.end():]
        remainder = remainder.lstrip(" \n\t.:-\u2013\u2014\"'")
        return remainder
    return body


def scrape(url: str, delay: float = 0.0) -> ArticleResult:
    if delay:
        time.sleep(delay)
    try:
        html = fetch_html(url)
    except Exception as e:
        return ArticleResult(url, "unknown", None, None, None, None, 0, "none", error=str(e))

    method = "trafilatura"
    author = date = headline = body = None
    soup = BeautifulSoup(html, "html.parser")

    if _is_cna_watch_url(url):
        # Video pages: only the caption/synopsis, nothing else on the page.
        headline = extract_headline_fallback(soup)
        body = extract_video_caption(soup)
        method = "video_caption"
    else:
        # --- Primary: trafilatura (handles boilerplate/ad removal + multilingual) ---
        tf_json = trafilatura.extract(
            html,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            url=url,
        )
        if tf_json:
            try:
                meta = json.loads(tf_json)
                headline = meta.get("title")
                author = meta.get("author")
                date = meta.get("date")
                body = meta.get("text")
            except json.JSONDecodeError:
                pass

        # --- Fallback: BeautifulSoup heuristics for whatever trafilatura missed ---
        if not body or len(body) < 200:
            fb_body = extract_body_fallback(soup)
            if fb_body and (not body or len(fb_body) > len(body)):
                body = fb_body
                method = "fallback" if method != "trafilatura" else "trafilatura+fallback_body"
        if not author:
            author = extract_author_fallback(soup)
            if author:
                method += "+fallback_author"
        if not headline:
            headline = extract_headline_fallback(soup)

    # --- Drop a duplicated headline from the start of the body, and strip
    # known per-outlet boilerplate (e.g. BT's newsletter promo) ---
    body = _strip_leading_headline(body, headline)
    body = _strip_boilerplate(body)

    # --- Split body into one-sentence-per-paragraph (blank line between each) ---
    if body:
        body = split_into_sentences(body)

    lang_code = detect_language((headline or "") + " " + (body or ""))
    language = LANG_LABELS.get(lang_code, lang_code)

    word_count = len(body.split()) if body else 0

    return ArticleResult(
        url=url,
        language=language,
        headline=headline.strip() if headline else None,
        author=author.strip() if author else None,
        date=date,
        body=body.strip() if body else None,
        word_count=word_count,
        extraction_method=method,
    )


def main():
    ap = argparse.ArgumentParser(description="Extract clean article text from news URLs.")
    ap.add_argument("urls", nargs="*", help="Article URLs")
    ap.add_argument("--file", help="Path to a text file with one URL per line")
    ap.add_argument("--out", help="Write results as JSON to this path")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between requests (default 1.0)")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            urls.extend(line.strip() for line in f if line.strip())

    if not urls:
        ap.error("Provide at least one URL, or --file with a list of URLs.")

    results = []
    for i, url in enumerate(urls):
        result = scrape(url, delay=args.delay if i > 0 else 0)
        results.append(result)
        print(f"\n{'='*80}\nURL: {result.url}")
        if result.error:
            print(f"  ERROR: {result.error}")
            continue
        print(f"Language: {result.language}")
        print(f"Headline: {result.headline}")
        print(f"Author:   {result.author}")
        print(f"Date:     {result.date}")
        print(f"Word count: {result.word_count}")
        print(f"(extraction method: {result.extraction_method})")
        print("-" * 80)
        print(result.body)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(results)} result(s) to {args.out}")


if __name__ == "__main__":
    main()
