#!/usr/bin/env python3
"""
app.py — local web UI for news_scraper.py

Run:
    python app.py
Then open http://127.0.0.1:5000 in your browser.
"""

from flask import Flask, request, render_template_string
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from news_scraper import scrape
from print_scraper import (
    parse_table_paste, fetch_table_row, prefetch_json_cache,
    SOURCE_LABELS, SUPPORTED_SOURCES, EXCLUDED_SOURCES,
)

app = Flask(__name__)

# How many articles/JSON pages to fetch at once. Fetching is I/O-bound
# (waiting on the network), so running several in parallel — instead of one
# at a time with a sleep() between each — is what lets a batch of many
# links finish inside a hosting platform's request timeout (e.g. Render's
# gunicorn worker defaults to killing a request after 30s). Override with
# the SCRAPE_CONCURRENCY env var if needed.
SCRAPE_CONCURRENCY = int(os.environ.get("SCRAPE_CONCURRENCY", "4"))

PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>News Scraper</title>
<style>
  :root {
    --blue-50:  #eef5ff;
    --blue-100: #dceaff;
    --blue-400: #5b9dfa;
    --blue-500: #3b82f6;
    --blue-600: #2563eb;
    --blue-700: #1d4ed8;
    --ink:      #111827;
    --muted:    #64748b;
    --glass:    rgba(255, 255, 255, 0.55);
    --glass-border: rgba(255, 255, 255, 0.75);
  }

  * { box-sizing: border-box; }

  body {
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    max-width: 900px;
    margin: 0 auto;
    padding: 48px 20px 80px;
    color: var(--ink);
    min-height: 100vh;
    background:
      radial-gradient(circle at 15% 10%, var(--blue-100), transparent 45%),
      radial-gradient(circle at 85% 0%, #dbeafe, transparent 40%),
      linear-gradient(180deg, #f4f8ff 0%, #eef3fb 100%);
    background-attachment: fixed;
  }

  h1 {
    font-size: 26px;
    font-weight: 700;
    margin-bottom: 4px;
    background: linear-gradient(90deg, var(--blue-700), var(--blue-400));
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    display: inline-block;
  }

  .subtitle {
    color: var(--muted);
    font-size: 14px;
    margin-bottom: 28px;
  }

  .tabs {
    display: flex;
    gap: 6px;
    margin-bottom: 22px;
  }
  .tabs a {
    text-decoration: none;
    font-size: 14px;
    font-weight: 600;
    color: var(--blue-700);
    padding: 8px 16px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.55);
    border: 1px solid var(--glass-border);
    transition: background 0.15s ease, box-shadow 0.15s ease;
  }
  .tabs a:hover {
    background: white;
    box-shadow: 0 2px 10px rgba(37, 99, 235, 0.12);
  }
  .tabs a.active {
    color: white;
    background: linear-gradient(135deg, var(--blue-500), var(--blue-700));
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.3);
  }

  .glass {
    background: var(--glass);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    border: 1px solid var(--glass-border);
    border-radius: 18px;
    box-shadow: 0 8px 30px rgba(37, 99, 235, 0.08);
  }

  form.glass {
    padding: 24px;
    margin-bottom: 8px;
    transition: box-shadow 0.25s ease;
  }
  form.glass:focus-within {
    box-shadow: 0 10px 40px rgba(37, 99, 235, 0.16);
  }

  textarea {
    width: 100%;
    height: 120px;
    font-family: "SF Mono", Consolas, monospace;
    font-size: 14px;
    padding: 14px;
    border-radius: 12px;
    border: 1px solid #d6e4ff;
    background: rgba(255, 255, 255, 0.7);
    resize: vertical;
    outline: none;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }
  textarea:focus {
    border-color: var(--blue-500);
    box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.15);
  }

  label {
    font-size: 14px;
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 14px 0 4px;
    cursor: pointer;
    color: var(--ink);
    user-select: none;
  }
  input[type="checkbox"] {
    width: 17px;
    height: 17px;
    accent-color: var(--blue-500);
    cursor: pointer;
  }

  button {
    background: linear-gradient(135deg, var(--blue-500), var(--blue-700));
    color: white;
    border: none;
    padding: 11px 24px;
    border-radius: 10px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.3);
    transition: transform 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
  }
  button:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 20px rgba(37, 99, 235, 0.4);
  }
  button:active { transform: translateY(0); }
  button:disabled {
    opacity: 0.7;
    cursor: not-allowed;
    transform: none;
  }

  button.small {
    padding: 7px 16px;
    font-size: 13px;
    box-shadow: 0 2px 8px rgba(37, 99, 235, 0.25);
  }
  button.secondary {
    background: rgba(255, 255, 255, 0.7);
    color: var(--blue-700);
    border: 1px solid var(--blue-100);
    box-shadow: none;
  }
  button.secondary:hover {
    background: white;
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.15);
  }

  .article {
    padding: 22px;
    margin: 20px 0;
    animation: fadeUp 0.4s ease both;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }
  .article:hover {
    transform: translateY(-3px);
    box-shadow: 0 14px 34px rgba(37, 99, 235, 0.14);
  }
  .article:nth-of-type(1) { animation-delay: 0.02s; }
  .article:nth-of-type(2) { animation-delay: 0.06s; }
  .article:nth-of-type(3) { animation-delay: 0.1s; }
  .article:nth-of-type(4) { animation-delay: 0.14s; }
  .article:nth-of-type(5) { animation-delay: 0.18s; }

  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(14px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .meta {
    color: var(--muted);
    font-size: 13px;
    margin-bottom: 10px;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 16px;
  }
  .meta span {
    background: var(--blue-50);
    padding: 2px 9px;
    border-radius: 999px;
  }

  .headline {
    font-size: 18px;
    font-weight: 700;
    margin-bottom: 8px;
    color: var(--ink);
  }

  .body {
    white-space: pre-wrap;
    line-height: 1.65;
    font-size: 15px;
    max-height: 400px;
    overflow-y: auto;
    border-top: 1px solid rgba(37, 99, 235, 0.12);
    padding-top: 14px;
    margin-top: 10px;
  }
  .body::-webkit-scrollbar { width: 8px; }
  .body::-webkit-scrollbar-thumb {
    background: var(--blue-100);
    border-radius: 8px;
  }

  .error {
    color: #b91c1c;
    font-weight: 500;
  }

  .toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 28px 0 0;
  }
  .results-count {
    font-size: 13px;
    color: var(--muted);
  }
  .copied-msg {
    font-size: 13px;
    color: #16794f;
    margin-left: 10px;
    display: none;
    animation: fadeUp 0.2s ease both;
  }

  /* Loading overlay shown while the form submits */
  #loading-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(238, 243, 251, 0.75);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    z-index: 999;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 16px;
  }
  #loading-overlay.active { display: flex; }

  .spinner {
    width: 46px;
    height: 46px;
    border-radius: 50%;
    border: 4px solid var(--blue-100);
    border-top-color: var(--blue-600);
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .loading-text {
    font-size: 14px;
    color: var(--blue-700);
    font-weight: 600;
  }

  .btn-spinner {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 2px solid rgba(255,255,255,0.5);
    border-top-color: white;
    animation: spin 0.7s linear infinite;
    display: none;
  }
  button.loading .btn-spinner { display: inline-block; }
  button.loading .btn-label { opacity: 0.85; }
</style>
</head>
<body>

  <div id="loading-overlay">
    <div class="spinner"></div>
    <div class="loading-text">Scraping articles…</div>
  </div>

  <div class="tabs">
    <a href="/" class="active">Web scrape</a>
    <a href="/print">Print (ST / BT / BH)</a>
  </div>

  <h1>News Article Scraper</h1>
  <div class="subtitle">Paste article links below and pull clean, structured text out of them.</div>

  <form method="POST" class="glass" id="scrape-form">
    <textarea name="urls" placeholder="Paste one or more article links, one per line...">{{ raw_urls or '' }}</textarea>
    <div style="margin-top: 16px;">
      <button type="submit" id="submit-btn">
        <span class="btn-spinner"></span>
        <span class="btn-label">Scrape</span>
      </button>
    </div>
  </form>

  {% if results %}
  <div class="toolbar">
    <span class="results-count">{{ results|length }} result{{ 's' if results|length != 1 else '' }}</span>
    <div>
      <button class="secondary" onclick="copyAll()">Copy All (for Sheets)</button>
      <span id="copied-all" class="copied-msg">Copied!</span>
    </div>
  </div>
  {% endif %}

  {% for r in results %}
  <div class="article glass">
    {% if r.error %}
      <div class="meta"><span>{{ r.url }}</span></div>
      <div class="error">Error: {{ r.error }}</div>
    {% else %}
      <div class="headline">{{ r.headline or '(no headline found)' }}</div>
      <div class="meta">
        <span>{{ r.url }}</span>
        <span>Language: {{ r.language }}</span>
        <span>Author: {{ r.author or 'unknown' }}</span>
        <span>{{ r.word_count }} words</span>
        <span>method: {{ r.extraction_method }}</span>
      </div>
      <div class="body">{{ r.body or '(no body extracted)' }}</div>
      <div style="margin-top: 12px;">
        <button class="small secondary" onclick="copyRow({{ loop.index0 }})">Copy row</button>
        <span id="copied-{{ loop.index0 }}" class="copied-msg">Copied!</span>
      </div>
    {% endif %}
  </div>
  {% endfor %}

<script>
  const articles = {{ articles_json|safe }};

  function tsvEscape(field) {
    field = (field === null || field === undefined) ? '' : String(field);
    if (/[\\t\\n"]/.test(field)) {
      field = '"' + field.replace(/"/g, '""') + '"';
    }
    return field;
  }

  function formattedText(a) {
    if (!a) return '';
    // Error rows keep their slot (with the error message) instead of being
    // dropped, so every other row's position still lines up with the
    // pasted URLs when copied back into the Sheet.
    if (a.error) {
      return 'ERROR: ' + a.error;
    }
    const parts = [];
    if (a.language !== 'English' && a.headline) {
      parts.push(a.headline.trim());
    }
    if (a.author) {
      parts.push(a.author.trim());
    }
    if (a.body) {
      parts.push(a.body.trim());
    }
    return parts.join('\\n\\n');
  }

  function flash(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = 'inline';
    setTimeout(() => { el.style.display = 'none'; }, 1500);
  }

  // navigator.clipboard.writeText only works on "secure contexts" (https,
  // or localhost). If you open this page via a LAN IP over plain http (e.g.
  // from your phone), navigator.clipboard is undefined and the old code
  // failed completely silently. This falls back to the older
  // execCommand('copy') trick so the buttons work either way.
  function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      try {
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        ok ? resolve() : reject(new Error('execCommand copy failed'));
      } catch (err) {
        document.body.removeChild(ta);
        reject(err);
      }
    });
  }

  function copyRow(idx) {
    // Wrap in quotes so newlines inside the article stay in ONE cell when
    // pasted into Sheets/Excel, instead of spilling into extra rows.
    copyToClipboard(tsvEscape(formattedText(articles[idx])))
      .then(() => flash('copied-' + idx))
      .catch(err => alert('Copy failed: ' + err.message));
  }

  function copyAll() {
    // One escaped article per line -> one row per article when pasted.
    // Error rows keep their position (with an ERROR: message) instead of
    // being dropped, so the pasted rows don't shift out of alignment with
    // the original list of URLs.
    const rows = articles.map(a => tsvEscape(formattedText(a)));
    copyToClipboard(rows.join('\\n'))
      .then(() => flash('copied-all'))
      .catch(err => alert('Copy failed: ' + err.message));
  }

  // Loading state while the page round-trips to the server
  const form = document.getElementById('scrape-form');
  const overlay = document.getElementById('loading-overlay');
  const submitBtn = document.getElementById('submit-btn');
  form.addEventListener('submit', () => {
    overlay.classList.add('active');
    submitBtn.classList.add('loading');
    submitBtn.disabled = true;
  });
</script>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    raw_urls = ""

    if request.method == "POST":
        raw_urls = request.form.get("urls", "")
        urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]

        if urls:
            # executor.map keeps results in the same order as `urls` even
            # though the fetches themselves run concurrently.
            with ThreadPoolExecutor(max_workers=SCRAPE_CONCURRENCY) as executor:
                results = list(executor.map(scrape, urls))

    articles_json = json.dumps(
        [{"error": r.error, "url": r.url} if r.error else asdict(r) for r in results],
        ensure_ascii=False,
    ).replace("</", "<\\/")

    return render_template_string(
        PAGE,
        results=results,
        raw_urls=raw_urls,
        articles_json=articles_json,
    )


PAGE_PRINT = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Print Article Fetcher</title>
<style>
  :root {
    --blue-50:  #eef5ff;
    --blue-100: #dceaff;
    --blue-400: #5b9dfa;
    --blue-500: #3b82f6;
    --blue-600: #2563eb;
    --blue-700: #1d4ed8;
    --ink:      #111827;
    --muted:    #64748b;
    --glass:    rgba(255, 255, 255, 0.55);
    --glass-border: rgba(255, 255, 255, 0.75);
    --err-bg:   #fde8e6;
    --err-text: #b91c1c;
    --skip-bg:  #fef3c7;
    --skip-text:#92400e;
    --ok-bg:    #dcfce7;
    --ok-text:  #166534;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    max-width: 1400px;
    margin: 0 auto;
    padding: 48px 24px 80px;
    color: var(--ink);
    min-height: 100vh;
    background:
      radial-gradient(circle at 15% 10%, var(--blue-100), transparent 45%),
      radial-gradient(circle at 85% 0%, #dbeafe, transparent 40%),
      linear-gradient(180deg, #f4f8ff 0%, #eef3fb 100%);
    background-attachment: fixed;
  }
  h1 {
    font-size: 26px;
    font-weight: 700;
    margin-bottom: 4px;
    background: linear-gradient(90deg, var(--blue-700), var(--blue-400));
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    display: inline-block;
  }
  .subtitle { color: var(--muted); font-size: 14px; margin-bottom: 18px; }
  .hint {
    color: var(--muted);
    font-size: 13px;
    margin: 0 0 22px;
    line-height: 1.6;
    max-width: 900px;
  }
  .hint code {
    background: var(--blue-50);
    padding: 1px 6px;
    border-radius: 6px;
    font-family: "SF Mono", Consolas, monospace;
  }
  .hint .warn {
    display: inline-block;
    margin-top: 6px;
    color: var(--skip-text);
    background: var(--skip-bg);
    padding: 2px 9px;
    border-radius: 999px;
    font-weight: 600;
  }
  .tabs { display: flex; gap: 6px; margin-bottom: 22px; }
  .tabs a {
    text-decoration: none;
    font-size: 14px;
    font-weight: 600;
    color: var(--blue-700);
    padding: 8px 16px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.55);
    border: 1px solid var(--glass-border);
    transition: background 0.15s ease, box-shadow 0.15s ease;
  }
  .tabs a:hover { background: white; box-shadow: 0 2px 10px rgba(37, 99, 235, 0.12); }
  .tabs a.active {
    color: white;
    background: linear-gradient(135deg, var(--blue-500), var(--blue-700));
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.3);
  }
  .glass {
    background: var(--glass);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    border: 1px solid var(--glass-border);
    border-radius: 18px;
    box-shadow: 0 8px 30px rgba(37, 99, 235, 0.08);
  }
  form.glass { padding: 24px; margin-bottom: 8px; transition: box-shadow 0.25s ease; }
  form.glass:focus-within { box-shadow: 0 10px 40px rgba(37, 99, 235, 0.16); }
  textarea {
    width: 100%;
    height: 160px;
    font-family: "SF Mono", Consolas, monospace;
    font-size: 13px;
    padding: 14px;
    border-radius: 12px;
    border: 1px solid #d6e4ff;
    background: rgba(255, 255, 255, 0.7);
    resize: vertical;
    outline: none;
    white-space: pre;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }
  textarea:focus { border-color: var(--blue-500); box-shadow: 0 0 0 4px rgba(59, 130, 246, 0.15); }
  button {
    background: linear-gradient(135deg, var(--blue-500), var(--blue-700));
    color: white;
    border: none;
    padding: 11px 24px;
    border-radius: 10px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    margin-top: 16px;
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.3);
    transition: transform 0.15s ease, box-shadow 0.15s ease;
  }
  button:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(37, 99, 235, 0.4); }
  button:active { transform: translateY(0); }
  button.small { padding: 6px 14px; font-size: 12px; margin-top: 0; box-shadow: 0 2px 8px rgba(37, 99, 235, 0.25); }
  button.secondary {
    background: rgba(255, 255, 255, 0.85);
    color: var(--blue-700);
    border: 1px solid var(--blue-100);
    box-shadow: none;
  }
  button.secondary:hover { background: white; box-shadow: 0 4px 14px rgba(37, 99, 235, 0.15); }

  .toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
    margin: 26px 0 14px;
  }
  .summary { font-size: 13px; color: var(--muted); display: flex; gap: 10px; flex-wrap: wrap; }
  .summary .pill { padding: 2px 10px; border-radius: 999px; font-weight: 600; }
  .pill.ok   { background: var(--ok-bg);   color: var(--ok-text); }
  .pill.err  { background: var(--err-bg);  color: var(--err-text); }
  .pill.skip { background: var(--skip-bg); color: var(--skip-text); }
  .copied-msg { font-size: 13px; color: #16794f; margin-left: 8px; display: none; }

  .table-wrap {
    overflow-x: auto;
    border-radius: 16px;
    border: 1px solid var(--glass-border);
    background: var(--glass);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    box-shadow: 0 8px 30px rgba(37, 99, 235, 0.08);
  }
  table.results-table { border-collapse: collapse; width: 100%; font-size: 13px; }
  table.results-table th {
    position: sticky;
    top: 0;
    text-align: left;
    background: rgba(37, 99, 235, 0.9);
    color: white;
    padding: 10px 12px;
    font-weight: 600;
    white-space: nowrap;
    z-index: 1;
  }
  table.results-table td {
    padding: 10px 12px;
    border-top: 1px solid rgba(37, 99, 235, 0.1);
    vertical-align: top;
  }
  table.results-table tr:nth-child(even) td { background: rgba(255, 255, 255, 0.35); }
  table.results-table tr.row-error td   { background: rgba(253, 232, 230, 0.55) !important; }
  table.results-table tr.row-skipped td { background: rgba(254, 243, 199, 0.5) !important; }
  td.col-date, td.col-source, td.col-type { white-space: nowrap; }
  td.col-title { max-width: 260px; }
  td.col-url a { color: var(--blue-600); }
  td.fulltext-cell { min-width: 340px; max-width: 480px; }
  .fulltext-body {
    white-space: pre-wrap;
    line-height: 1.55;
    max-height: 220px;
    overflow-y: auto;
    margin-bottom: 6px;
  }
  .status-tag {
    display: inline-block;
    font-size: 11px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 999px;
    margin-right: 6px;
  }
  .status-tag.err  { background: var(--err-bg);  color: var(--err-text); }
  .status-tag.skip { background: var(--skip-bg); color: var(--skip-text); }
  .status-text { font-size: 13px; }
  .status-text.err  { color: var(--err-text); }
  .status-text.skip { color: var(--skip-text); }

  #loading-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(238, 243, 251, 0.75);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    z-index: 999;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 16px;
  }
  #loading-overlay.active { display: flex; }
  .spinner {
    width: 46px;
    height: 46px;
    border-radius: 50%;
    border: 4px solid var(--blue-100);
    border-top-color: var(--blue-600);
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading-text { font-size: 14px; color: var(--blue-700); font-weight: 600; }
</style>
</head>
<body>

  <div id="loading-overlay">
    <div class="spinner"></div>
    <div class="loading-text">Fetching print articles…</div>
  </div>

  <div class="tabs">
    <a href="/">Web scrape</a>
    <a href="/print" class="active">Print (ST / BT / BH / LHZB / Shin Min)</a>
  </div>

  <h1>Print Article Fetcher</h1>
  <div class="subtitle">Paste rows straight from your Sheet — same column order, columns A to G.</div>
  <div class="hint">
    Select <code>Publish Date</code> through <code>URL</code> (columns A–G) in your sheet, copy, and paste
    directly into the box below — tabs between columns are kept automatically.<br>
    Order expected: <code>Publish Date&nbsp;&nbsp;Article Date&nbsp;&nbsp;Newspaper&nbsp;&nbsp;Article Type&nbsp;&nbsp;Section&nbsp;&nbsp;Title&nbsp;&nbsp;URL</code><br>
    Supported: {% for code, label in source_labels.items() %}<code>{{ code }}</code> ({{ label }}){% if not loop.last %}, {% endif %}{% endfor %}.
    Only rows with Article Type = <code>Print</code> (or blank) are fetched; other sources are skipped.
    <span class="warn">Shin Min code is a guess ("SM") — tell Claude your sheet's actual code if different.</span>
  </div>

  <form method="POST" class="glass" id="print-form">
    <textarea name="rows" placeholder="26/05/2026	26/05/2026	BH	Print	Frontpage	S'pura kekal ramalan pertumbuhan ekon 2-4%, tapi diancam konflik Timur Tengah	https://hosted-content.meltwater.com/files/sph/BH/20260526/bhte_20260526_article_1-3.pdf
26/05/2026	26/05/2026	ST	Print	Frontpage	S'pore keeps 2026 growth forecast at 2%-4%, as downside risks grow	https://hosted-content.meltwater.com/files/sph/ST/20260526/stte_20260526_article_1-6.pdf">{{ raw_rows or '' }}</textarea>
    <div>
      <button type="submit" id="submit-btn">Fetch articles</button>
    </div>
  </form>

  {% if results %}
  <div class="toolbar">
    <div class="summary">
      <span class="pill ok">{{ ok_count }} fetched</span>
      <span class="pill err">{{ error_count }} errors</span>
      <span class="pill skip">{{ skipped_count }} skipped</span>
    </div>
    <div>
      <button class="secondary" onclick="copyAll()">Copy Full Text column (paste into Sheet Col L)</button>
      <span id="copied-all" class="copied-msg">Copied!</span>
    </div>
  </div>

  <div class="table-wrap">
    <table class="results-table">
      <thead>
        <tr>
          <th>Publish Date</th>
          <th>Article Date</th>
          <th>Newspaper</th>
          <th>Type</th>
          <th>Section</th>
          <th>Title</th>
          <th>URL</th>
          <th>Full Text</th>
        </tr>
      </thead>
      <tbody>
        {% for r in results %}
        <tr class="{% if r.status == 'error' %}row-error{% elif r.status == 'skipped' %}row-skipped{% endif %}">
          <td class="col-date">{{ r.publish_date }}</td>
          <td class="col-date">{{ r.article_date }}</td>
          <td class="col-source">{{ r.source }}</td>
          <td class="col-type">{{ r.article_type }}</td>
          <td>{{ r.section }}</td>
          <td class="col-title">{{ r.title }}</td>
          <td class="col-url">{% if r.url %}<a href="{{ r.url }}" target="_blank" rel="noopener">link</a>{% endif %}</td>
          <td class="fulltext-cell">
            {% if r.status == 'ok' %}
              <div class="fulltext-body">{{ r.body }}</div>
              <button class="small secondary" onclick="copyRow({{ loop.index0 }})">Copy</button>
              <span id="copied-{{ loop.index0 }}" class="copied-msg">Copied!</span>
            {% elif r.status == 'skipped' %}
              <span class="status-tag skip">SKIPPED</span><span class="status-text skip">{{ r.error }}</span>
            {% else %}
              <span class="status-tag err">{{ r.error_type }}</span><span class="status-text err">{{ r.error }}</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}

<script>
  // One entry per pasted row, in the same order — 'ok' rows have the full
  // text string, error/skipped rows are null so column alignment with the
  // pasted rows is preserved when copying back into the Sheet.
  const bodies = {{ bodies_json|safe }};

  function tsvEscape(field) {
    field = (field === null || field === undefined) ? '' : String(field);
    if (/[\\t\\n"]/.test(field)) {
      field = '"' + field.replace(/"/g, '""') + '"';
    }
    return field;
  }

  function flash(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = 'inline';
    setTimeout(() => { el.style.display = 'none'; }, 1500);
  }

  // navigator.clipboard.writeText only works on "secure contexts" (https,
  // or localhost). If you open this page via a LAN IP over plain http (e.g.
  // from your phone), navigator.clipboard is undefined and the old code
  // failed completely silently. This falls back to the older
  // execCommand('copy') trick so the buttons work either way.
  function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      try {
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        ok ? resolve() : reject(new Error('execCommand copy failed'));
      } catch (err) {
        document.body.removeChild(ta);
        reject(err);
      }
    });
  }

  function copyRow(idx) {
    copyToClipboard(bodies[idx] || '')
      .then(() => flash('copied-' + idx))
      .catch(err => alert('Copy failed: ' + err.message));
  }

  function copyAll() {
    const rows = bodies.map(b => tsvEscape(b));
    copyToClipboard(rows.join('\\n'))
      .then(() => flash('copied-all'))
      .catch(err => alert('Copy failed: ' + err.message));
  }

  const form = document.getElementById('print-form');
  const overlay = document.getElementById('loading-overlay');
  form.addEventListener('submit', () => { overlay.classList.add('active'); });
</script>
</body>
</html>
"""


@app.route("/print", methods=["GET", "POST"])
def print_index():
    results = []
    raw_rows = ""

    if request.method == "POST":
        raw_rows = request.form.get("rows", "")
        parsed_rows = parse_table_paste(raw_rows)

        # Fetch every distinct (source, date) consolidated.json page up
        # front, in parallel — a batch of rows often shares just a handful
        # of dates, so this turns N sequential downloads into a few
        # concurrent ones. Rows are then formatted from that cache, which
        # is fast since it's no longer waiting on the network.
        json_cache = prefetch_json_cache(parsed_rows, max_workers=SCRAPE_CONCURRENCY)
        for row in parsed_rows:
            results.append(fetch_table_row(row, json_cache=json_cache))

    def _cell_text(r):
        if r.status == "ok":
            return r.body
        if r.status == "skipped":
            return f"SKIPPED: {r.error}"
        return f"ERROR: {r.error}"

    bodies_json = json.dumps(
        [_cell_text(r) for r in results],
        ensure_ascii=False,
    ).replace("</", "<\\/")

    ok_count = sum(1 for r in results if r.status == "ok")
    error_count = sum(1 for r in results if r.status == "error")
    skipped_count = sum(1 for r in results if r.status == "skipped")

    return render_template_string(
        PAGE_PRINT,
        results=results,
        raw_rows=raw_rows,
        bodies_json=bodies_json,
        ok_count=ok_count,
        error_count=error_count,
        skipped_count=skipped_count,
        source_labels=SOURCE_LABELS,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=True, host="0.0.0.0", port=port)
