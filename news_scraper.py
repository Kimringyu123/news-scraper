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

_NORM_WS_RE = re.compile(r"\s+")


def _normalize_for_match(text: Optional[str]) -> str:
    """Loosely normalize text (case, smart quotes/dashes, whitespace,
    trailing punctuation) so a headline or dek can be matched against a
    scraped line even if quote style or a trailing period differs."""
    if not text:
        return ""
    text = text.strip().lower()
    text = re.sub(r"[\u2018\u2019\u201a\u2032]", "'", text)
    text = re.sub(r"[\u201c\u201d\u201e\u2033]", '"', text)
    text = re.sub(r"[\u2013\u2014\u2015]", "-", text)
    text = _NORM_WS_RE.sub(" ", text)
    return text.strip(" .!?\"'")


def clean_paragraphs(text: str) -> str:
    """Normalize body text into the paragraph shape a reader actually sees
    on the page — one blank line between paragraphs, in the site's original
    order — instead of re-splitting every sentence onto its own line. The
    old per-sentence splitter mis-detected periods inside abbreviations and
    dates (e.g. "Jun. 26.") as sentence boundaries, wrongly cutting them in
    two. Also drops stray button/hyperlink chrome lines (Share, Subscribe,
    bare URLs, icon-only lines) that occasionally get scraped in alongside
    real paragraphs. Doesn't merge separate paragraphs together, and doesn't
    leave them jammed with no spacing between them either."""
    if not text:
        return text

    raw_lines = [ln.strip() for ln in re.split(r"\n+", text) if ln.strip()]
    if not raw_lines:
        return text

    kept = [ln for ln in raw_lines if not _is_junk_ui_line(ln)]
    return "\n\n".join(kept)


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


# Generic desk/publication "bylines" that some sites fill in when there's
# no actual named author (e.g. the author field just says "Straits Times"
# or "Newsdesk") — these aren't real bylines and should be treated the same
# as no author at all, rather than printed as if they were one. Mirrors the
# same fix applied to print_scraper.py.
_GENERIC_BYLINE_RE = re.compile(
    r"^(the\s+)?(straits times|business times|berita harian|lianhe zaobao|"
    r"shin min daily news|tamil murasu|channel\s*newsasia|cna|today|"
    r"mothership|zaobao|8world|"
    r"[a-z\s]*\b(desk|newsroom|news\s*desk|wires|bureau|correspondents?|"
    r"editorial team|news team|staff writer|staff reporter)\b)$",
    re.IGNORECASE,
)
_GENERIC_BYLINE_ZH_RE = re.compile(
    r"^(本报讯|本报记者|本报综合|综合报道|综合|编辑部|新闻中心|"
    r"采访\s*[/／]?\s*报道|记者|特约记者|通讯员|联合早报|联合早报讯|来源|讯)$"
)


def _get_site_name(soup: BeautifulSoup) -> str:
    """The site's own name, if declared in its metadata — used to catch a
    publication naming itself as the 'author' instead of a real byline."""
    for tag, attrs in (
        ("meta", {"property": "og:site_name"}),
        ("meta", {"name": "application-name"}),
    ):
        el = soup.find(tag, attrs=attrs)
        if el and el.get("content"):
            return el["content"].strip()
    return ""


def _clean_author(name: Optional[str], soup: BeautifulSoup) -> Optional[str]:
    """Reject a raw 'author' value that's actually a generic desk/wire
    label or the publication's own name, rather than a real byline."""
    if not name:
        return None
    name = str(name).strip()
    name = re.sub(r"\S+@\S+\.\S+", "", name).strip()  # strip emails
    name = re.sub(r"^by\s+", "", name, flags=re.IGNORECASE).strip()  # strip "By "
    name = re.split(r"\s*[,|]\s*", name, maxsplit=1)[0].strip()
    if not name:
        return None
    if _GENERIC_BYLINE_RE.match(name) or _GENERIC_BYLINE_ZH_RE.match(name):
        return None
    site_name = _get_site_name(soup)
    if site_name and _normalize_for_match(name) == _normalize_for_match(site_name):
        return None
    # Leftover punctuation/whitespace-only "names" aren't a real byline either.
    if not re.search(r"[^\W\d_]", name, flags=re.UNICODE):
        return None
    return name


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


# Class-name fragments sites commonly use to mark up the short subheadline/
# dek/standfirst line shown under the headline (e.g. BT's "[SINGAPORE]..."
# style dek, Mothership's italic one-liner). This is checked separately from
# the meta description because the two often don't match word-for-word —
# relying on meta description alone missed real subheadlines that get
# scraped into the body as if they were the first paragraph.
_DEK_CLASS_KEYWORDS = (
    "subheadline", "sub-headline", "sub_headline", "sub-heading", "subheading",
    "standfirst", "dek", "deck", "article-summary", "excerpt", "article-dek",
)


def extract_dek_fallback(soup: BeautifulSoup) -> Optional[str]:
    """Find the actual on-page subheadline/dek element, distinct from (and
    checked in addition to) the page's meta description."""
    el = soup.find(attrs={"itemprop": "alternativeHeadline"})
    if el:
        text = el.get_text(" ", strip=True)
        if text:
            return text
    for tag in soup.find_all(class_=True):
        classes = " ".join(tag.get("class") or []).lower()
        if any(kw in classes for kw in _DEK_CLASS_KEYWORDS):
            text = tag.get_text(" ", strip=True)
            if text and 3 <= len(text) <= 300:
                return text
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


def _flex(phrase: str) -> str:
    """Turn a literal phrase into a regex that tolerates however the source
    site happened to wrap/space its whitespace (newlines, extra spaces,
    non-breaking spaces, etc.) between words."""
    return r"\s+".join(re.escape(w) for w in phrase.split())


# Known trailing boilerplate blocks to strip out of the body, per outlet.
_BOILERPLATE_PATTERNS = [
    # Business Times newsletter promo, tacked on at the end of articles.
    re.compile(
        r"Decoding Asia newsletter:\s*your guide to navigating Asia in a new "
        r"global order\.\s*Sign up here to get Decoding Asia newsletter\.\s*"
        r"Delivered to your inbox\.\s*Free\.?",
        re.IGNORECASE,
    ),
    # Berita Mediacorp / Berita Harian online newsletter opt-in block, tacked
    # on at the end (or occasionally the start) of the article body — not
    # part of the actual article, so it should never appear in the copied
    # full text.
    re.compile(
        _flex("Ikuti perkembangan kami dan dapatkan Berita Terkini") + r"\.?\s*"
        + _flex("Langgani buletin emel kami") + r"\.?\s*"
        + _flex(
            "Dengan mengklik hantar, saya bersetuju data peribadi saya boleh "
            "digunakan untuk menghantar artikel dari Berita, tawaran promosi "
            "dan juga untuk penyelidikan dan analisis"
        ) + r"\.?",
        re.IGNORECASE,
    ),
    # 8world.com "listen to this article" audio-widget leftover text,
    # tacked onto the end of some articles: "新功能! 听新闻，按这里! 我要
    # 听，按这里! UH-OH! 难免有故障。请稍后再试。" (tolerant of full-width
    # vs half-width punctuation, in case the site varies it).
    re.compile(
        r"新功能\s*[!！]\s*"
        r"听新闻\s*[，,]\s*按这里\s*[!！]\s*"
        r"我要听\s*[，,]\s*按这里\s*[!！]\s*"
        r"UH-OH\s*[!！]\s*难免有故障\s*[。.]\s*请稍后再试\s*[。.]?",
        re.IGNORECASE,
    ),
    # 8world.com video pages: app/newsletter promo block ("每日接收头条新闻
    # 内容 一机在手，掌握天下事 天天送上精选好内容！") tacked onto the page,
    # not part of the actual video's caption/content.
    re.compile(
        r"每日接收头条新闻内容\s*"
        r"一机在手\s*[，,]\s*掌握天下事\s*"
        r"天天送上精选好内容\s*[!！]?",
        re.IGNORECASE,
    ),
    # 8world.com video pages: in-article dictionary/vocab-tool promo block
    # ("新功能 查看词典，学华文！点击文内词语，看词语解释、发音及翻译"),
    # not part of the actual video's caption/content.
    re.compile(
        r"新功能\s*"
        r"查看词典\s*[，,]\s*学华文\s*[!！]\s*"
        r"点击文内词语\s*[，,]\s*看词语解释\s*[、,，]\s*发音及翻译",
        re.IGNORECASE,
    ),
]


def _strip_boilerplate(body: Optional[str]) -> Optional[str]:
    if not body:
        return body
    for pattern in _BOILERPLATE_PATTERNS:
        body = pattern.sub("", body)
    return body.strip()


# Whole-line markers for ads, related-article call-outs, and social/newsletter
# prompts that sometimes get scraped in as if they were part of the article
# body. Matched against a *whole* line (after stripping) so real sentences
# that merely mention e.g. "shares" or "read" in passing are left alone —
# only lines that ARE one of these call-outs (optionally followed by a short
# fragment, like "Related: Some other headline") get dropped.
_JUNK_LINE_RE = re.compile(
    r"^(advertisement|advertisment|ads?|sponsored( content)?|"
    r"read also|read more|also read|related read(ing)?s?|"
    r"related stor(y|ies)|related articles?|related news|more on this( topic| story)?|"
    r"subscribe to (our |the )?newsletter|sign up for (our |the )?newsletter|"
    r"follow us on (facebook|twitter|instagram|telegram|tiktok)|"
    r"share this (article|story)|"
    r"baca juga|berita berkaitan|artikel berkaitan|ikuti kami di|langgani buletin|"
    r"相关新闻|延伸阅读|更多阅读|点击查看|广告|"
    r"தொடர்புடைய செய்திகள்|மேலும் படிக்க)"
    r"\s*[:\uff1a\u2013\u2014-]?\s*.*$",
    re.IGNORECASE,
)


def _strip_junk_lines(body: Optional[str]) -> Optional[str]:
    """Drop whole lines that are ads, related-article call-outs, or social/
    newsletter prompts rather than actual article text."""
    if not body:
        return body
    lines = body.split("\n")
    kept = [ln for ln in lines if not _JUNK_LINE_RE.match(ln.strip())]
    return "\n".join(kept).strip()


# Lines that are UI chrome, not article text: bare share/follow/subscribe
# prompts, button labels, and standalone links/icons that sometimes get
# scraped in alongside the real paragraphs (distinct from _JUNK_LINE_RE
# above, which targets ad/related/newsletter call-outs specifically).
_UI_CHROME_LINE_RE = re.compile(
    r"^(share|tweet|pin( it)?|whatsapp|telegram|line|copy link|print( this)?|"
    r"email this( article)?|listen to this article|listen now|play audio|"
    r"click here|tap here|read the full story|full story|comments?( \(\d+\))?|"
    r"log ?in|sign ?in|sign ?up|log ?out|download( our| the)? app|"
    r"get the app|open in app|see more|show more|load more|back to top|"
    r"skip to (main )?content|menu|search|ai generated( summary)?)\s*$",
    re.IGNORECASE,
)
_BARE_URL_LINE_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_SYMBOL_ONLY_LINE_RE = re.compile(r"^[\W_]+$")

# Photo/image credit lines (e.g. "Images via Ng Chee Meng/Facebook, Jasmin
# Lau/Facebook, David Neo/Facebook", "Photo courtesy of Reuters") — these are
# picture captions, not article text, and shouldn't be copied as part of it.
_CAPTION_CREDIT_RE = re.compile(
    r"^(image|images|photo|photos|picture|pictures|screengrab|screenshot|"
    r"gif|graphic|infographic)s?\s*(via|courtesy of|by|credit:?|source:?)\b.*$",
    re.IGNORECASE,
)

# Section headings that mark the start of a trailing "more from this site"
# block (a list of unrelated story titles/links) tacked onto the end of the
# page — everything from this heading onward gets cut, not just the heading
# line itself.
_TRAILING_CUTOFF_RE = re.compile(
    r"^(more stories|related stories|recommended( articles?| for you)?|"
    r"you may (also )?like|top stories|trending( now)?|popular( now)?|"
    r"editor'?s picks|what others are reading|read next|"
    r"more from (mothership|us)|around the web)\s*$",
    re.IGNORECASE,
)


def _is_junk_ui_line(line: str) -> bool:
    return bool(
        _UI_CHROME_LINE_RE.match(line)
        or _BARE_URL_LINE_RE.match(line)
        or _SYMBOL_ONLY_LINE_RE.match(line)
        or _CAPTION_CREDIT_RE.match(line)
        or _JUNK_LINE_RE.match(line)
    )


def _cut_trailing_boilerplate(body: Optional[str]) -> Optional[str]:
    """Drop a trailing 'More Stories' (or similar) section heading and
    everything scraped in after it — that's a list of unrelated story
    links, not part of this article."""
    if not body:
        return body
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if _TRAILING_CUTOFF_RE.match(line.strip()):
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def _strip_duplicate_headline_lines(body: Optional[str], headline: Optional[str], max_lines: int = 6) -> Optional[str]:
    """ST/BT (and similar) sometimes repeat the headline verbatim somewhere
    near the top of the extracted body — not always as the very first
    characters, but after the author name or an 'AI generated' button too.
    Drop any line within the first few lines that's just the headline
    repeated, wherever exactly it lands."""
    if not body or not headline:
        return body
    target = _normalize_for_match(headline)
    if not target:
        return body
    lines = body.split("\n")
    out = [ln for i, ln in enumerate(lines) if not (i < max_lines and _normalize_for_match(ln) == target)]
    return "\n".join(out)


def _strip_leading_subheadline(body: Optional[str], description: Optional[str], dek: Optional[str] = None) -> Optional[str]:
    """Some sites (e.g. Mothership) show a short italic dek/subheadline
    right under the headline ('Gratitude and excitement.') that isn't part
    of the article body. That line is checked against both the page's meta
    description (publishers commonly reuse the same text for both) and the
    dek pulled directly off the page — the two don't always match
    word-for-word, so relying on only one of them missed real cases. If the
    first line of the body matches either, it's the dek — drop it."""
    if not body:
        return body
    targets = {t for t in (_normalize_for_match(description), _normalize_for_match(dek)) if t}
    if not targets:
        return body
    lines = body.split("\n")
    if lines and _normalize_for_match(lines[0]) in targets:
        return "\n".join(lines[1:])
    return body


def _strip_leading_duplicate_lines(
    body: Optional[str],
    headline: Optional[str] = None,
    author: Optional[str] = None,
    description: Optional[str] = None,
    dek: Optional[str] = None,
    max_checks: int = 8,
) -> Optional[str]:
    """Some sites (notably Straits Times, Business Times) scrape the
    reporter's byline in as an ordinary paragraph at the very top of the
    article body — on top of it already being available separately as the
    author field — and the headline or dek/subheadline sometimes gets
    pulled in right under it. That combination pushes the duplicate
    headline/subheadline past what a single fixed-position check catches,
    since it's no longer the very first line once the duplicated byline is
    sitting in front of it. There's also a case with no real byline at all,
    where a bare publication name (e.g. "The Business Times") is scraped in
    as if it were the first paragraph.

    This repeatedly drops leading body lines that are blank, match the
    headline/author/description/dek, or match a generic desk/wire byline
    pattern — in whatever order they show up — stopping as soon as a line
    that isn't one of those is reached."""
    if not body:
        return body
    targets = {t for t in (
        _normalize_for_match(headline),
        _normalize_for_match(author),
        _normalize_for_match(description),
        _normalize_for_match(dek),
    ) if t}
    lines = body.split("\n")
    checks = 0
    while lines and checks < max_checks:
        raw = lines[0].strip()
        if not raw:
            lines = lines[1:]
            continue
        norm = _normalize_for_match(raw)
        if norm and norm in targets:
            lines = lines[1:]
            checks += 1
            continue
        if _GENERIC_BYLINE_RE.match(raw) or _GENERIC_BYLINE_ZH_RE.match(raw):
            lines = lines[1:]
            checks += 1
            continue
        break
    return "\n".join(lines)


def scrape(url: str, delay: float = 0.0) -> ArticleResult:
    if delay:
        time.sleep(delay)
    try:
        html = fetch_html(url)
    except Exception as e:
        return ArticleResult(url, "unknown", None, None, None, None, 0, "none", error=str(e))

    method = "trafilatura"
    author = date = headline = body = None
    description = None
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
                author = _clean_author(meta.get("author"), soup)
                date = meta.get("date")
                body = meta.get("text")
                description = meta.get("description")
            except json.JSONDecodeError:
                pass

        # --- Fallback: BeautifulSoup heuristics for whatever trafilatura missed ---
        if not body or len(body) < 200:
            fb_body = extract_body_fallback(soup)
            if fb_body and (not body or len(fb_body) > len(body)):
                body = fb_body
                method = "fallback" if method != "trafilatura" else "trafilatura+fallback_body"
        if not author:
            author = _clean_author(extract_author_fallback(soup), soup)
            if author:
                method += "+fallback_author"
        if not headline:
            headline = extract_headline_fallback(soup)

    # --- Drop leading duplicate lines (byline/headline/dek scraped in as if
    # they were body text, in any order/combination), a duplicated headline
    # further down, a dek/subheadline line, known per-outlet boilerplate
    # (e.g. BT's newsletter promo), and a trailing "More Stories" block ---
    dek = extract_dek_fallback(soup)
    body = _strip_leading_duplicate_lines(body, headline, author, description, dek)
    body = _strip_duplicate_headline_lines(body, headline)
    body = _strip_leading_subheadline(body, description, dek)
    body = _strip_boilerplate(body)
    body = _strip_junk_lines(body)
    body = _cut_trailing_boilerplate(body)

    # --- Normalize into the paragraph shape the reader actually sees (no
    # per-sentence splitting — that used to misfire on abbreviations/dates) ---
    if body:
        body = clean_paragraphs(body)

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
