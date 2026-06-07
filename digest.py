#!/usr/bin/env python3
"""Metals & Mining news digest -> Telegram.
v5: real source extraction, blocked-sources list, word-boundary keyword matching,
no auto-tags, stricter DeepSeek SKIP filter.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# --- Config -----------------------------------------------------------------

ROOT = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.path.join(ROOT, "feeds.txt")
KEYWORDS_FILE = os.path.join(ROOT, "keywords.txt")
STATE_FILE = os.path.join(ROOT, "state.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_API_KEY"]

MAX_ITEMS_PER_RUN = 12
MAX_AGE_HOURS = 48
TG_BUDGET = 3900  # leave headroom under 4096

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Sources we never want to see. Match against extracted source domain or label.
BLOCKED_SOURCES = {
    "msn.com", "msn",
    "inkorr.com", "inkorr",
    "news.google.com",
    "yahoo.com", "yahoo finance", "yahoo",
    "seekingalpha.com", "seeking alpha",
    "investorplace.com", "investorplace",
    "marketbeat.com", "marketbeat",
    "zacks.com", "zacks",
    "the motley fool", "fool.com",
    "benzinga.com", "benzinga",
    "simplywall.st", "simply wall st",
    "tipranks.com", "tipranks",
    "marketwatch.com",
    "247wallst.com", "24/7 wall st.",
    "stockstotrade.com",
    "barchart.com", "barchart",
}

# Map common publisher labels (from title suffix) to clean domains
SOURCE_LABEL_TO_DOMAIN = {
    "reuters": "reuters.com",
    "bloomberg": "bloomberg.com",
    "financial times": "ft.com",
    "ft": "ft.com",
    "wall street journal": "wsj.com",
    "wsj": "wsj.com",
    "argus media": "argusmedia.com",
    "s&p global commodity insights": "spglobal.com",
    "s&p global": "spglobal.com",
    "fastmarkets": "fastmarkets.com",
    "aluminium insider": "aluminiuminsider.com",
    "light metal age": "lightmetalage.com",
    "mining.com": "mining.com",
    "mining weekly": "miningweekly.com",
    "mining journal": "miningjournal.com",
    "the northern miner": "northernminer.com",
    "northern miner": "northernminer.com",
    "international mining": "im-mining.com",
    "steel times international": "steeltimesint.com",
    "bnamericas": "bnamericas.com",
    "kitco": "kitco.com",
    "shanghai metals market": "metal.com",
    "smm": "metal.com",
    "metal.com": "metal.com",
}

# --- Load config files ------------------------------------------------------

def load_feeds():
    feeds = []
    with open(FEEDS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                feeds.append(line)
    return feeds


def load_keywords():
    pats = []
    with open(KEYWORDS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            esc = re.escape(line)
            pats.append(re.compile(r"(?i)(?<!\w)" + esc + r"(?!\w)"))
    return pats


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"seen": []}
    return {"seen": []}


def save_state(state):
    state["seen"] = state["seen"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --- Source extraction ------------------------------------------------------

def split_title_and_source(raw_title):
    if not raw_title:
        return "", ""
    for sep in (" - ", " \u2014 ", " \u2013 "):
        if sep in raw_title:
            idx = raw_title.rfind(sep)
            title = raw_title[:idx].strip()
            pub = raw_title[idx + len(sep):].strip()
            return title, pub
    return raw_title.strip(), ""


def source_to_domain(pub_label, fallback_url):
    if not pub_label:
        return urllib.parse.urlparse(fallback_url).netloc.replace("www.", "")
    key = pub_label.lower().strip()
    if key in SOURCE_LABEL_TO_DOMAIN:
        return SOURCE_LABEL_TO_DOMAIN[key]
    return pub_label.strip()


def is_blocked(pub_label, domain):
    candidates = {pub_label.lower().strip(), domain.lower().strip()}
    return bool(candidates & BLOCKED_SOURCES)


# --- Fetch & parse ----------------------------------------------------------

def fetch_feed(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  ! fetch error: {e}", file=sys.stderr)
        return None


def parse_pubdate(s):
    if not s:
        return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def parse_feed(xml_text):
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  ! parse error: {e}", file=sys.stderr)
        return items
    for item in root.iter("item"):
        raw_title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pubd = parse_pubdate(item.findtext("pubDate") or "")
        desc = re.sub(r"<[^>]+>", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        title, pub_label = split_title_and_source(raw_title)
        domain = source_to_domain(pub_label, link)
        items.append({
            "title": title,
            "raw_title": raw_title,
            "link": link,
            "desc": desc,
            "pub": pub_label,
            "domain": domain,
            "pubdate": pubd,
        })
    return items


# --- Filtering --------------------------------------------------------------

def is_recent(dt):
    if dt is None:
        return True
    age = datetime.now(timezone.utc) - dt
    return age <= timedelta(hours=MAX_AGE_HOURS)


def matches_keywords(text, patterns):
    return any(p.search(text) for p in patterns)


def url_hash(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


# --- DeepSeek enrichment ----------------------------------------------------

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

SYS_PROMPT = (
    "You are an analyst supporting a senior independent consultant in non-ferrous metals "
    "and mining (16 years across UC RUSAL, Norilsk Nickel, UMMC, ERG). For each news item, "
    "decide if it is relevant to his profile and, if relevant, produce ONE short Russian "
    "sentence (max 22 words) explaining why it matters for the industry. "
    "Reply ONLY with valid JSON: {\"skip\": bool, \"why\": str}. "
    "Set skip=true for: stock analyst ratings, ETF picks, EPS forecasts, financial blogs, "
    "macro-economy with no metals angle, political opinion without industry impact, "
    "celebrity/lifestyle, generic press releases without operational substance. "
    "Set skip=false for: production data, smelter/refinery operations, M&A deals, CapEx "
    "decisions, regulation (tariffs, sanctions, CBAM, Section 232), prices and premia movements "
    "with cause, technology shifts (inert anode, H2 DRI), named operators' strategic moves."
)


def deepseek_enrich(title, desc, source):
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": f"SOURCE: {source}\nTITLE: {title}\nDESC: {desc[:500]}"},
        ],
        "temperature": 0.2,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
        content = resp["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"  ! deepseek error: {e}", file=sys.stderr)
        return {"skip": False, "why": ""}


# --- Telegram ---------------------------------------------------------------

def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ! telegram error {e.code}: {body}", file=sys.stderr)
        raise


# --- Main -------------------------------------------------------------------

def main():
    feeds = load_feeds()
    keywords = load_keywords()
    state = load_state()
    seen = set(state.get("seen", []))

    print(f"Feeds: {len(feeds)}, keywords: {len(keywords)}, seen: {len(seen)}")

    candidates = []
    for url in feeds:
        print(f"- {url[:80]}...")
        xml = fetch_feed(url)
        if not xml:
            continue
        items = parse_feed(xml)
        print(f"  parsed: {len(items)}")
        for it in items:
            h = url_hash(it["link"])
            if h in seen:
                continue
            if not is_recent(it["pubdate"]):
                continue
            if is_blocked(it["pub"], it["domain"]):
                continue
            blob = f"{it['title']} {it['desc']}"
            if not matches_keywords(blob, keywords):
                continue
            it["hash"] = h
            candidates.append(it)

    by_hash = {}
    for c in candidates:
        by_hash.setdefault(c["hash"], c)
    candidates = list(by_hash.values())

    candidates.sort(key=lambda x: x["pubdate"] or datetime.now(timezone.utc), reverse=True)

    print(f"Candidates after filter: {len(candidates)}")

    enriched = []
    for c in candidates:
        if len(enriched) >= MAX_ITEMS_PER_RUN:
            break
        verdict = deepseek_enrich(c["title"], c["desc"], c["domain"])
        if verdict.get("skip"):
            print(f"  . skip: {c['title'][:80]}")
            seen.add(c["hash"])
            continue
        c["why"] = (verdict.get("why") or "").strip()
        enriched.append(c)
        seen.add(c["hash"])

    print(f"Enriched: {len(enriched)}")

    if not enriched:
        print("Nothing to send.")
        state["seen"] = list(seen)
        save_state(state)
        return 0

    msk = timezone(timedelta(hours=3))
    now = datetime.now(msk).strftime("%d %b, %H:%M MSK")
    header = f"<b>\U0001f9e0 Metals &amp; Mining</b> — {now}\n\n"

    blocks = []
    for i, c in enumerate(enriched, 1):
        title = esc(c["title"])
        link = esc(c["link"])
        domain = esc(c["domain"])
        why = esc(c["why"])
        block = f"<b>{i}. {title}</b>\n<i>{domain}</i>\n"
        if why:
            block += f"\U0001f4a1 {why}\n"
        block += f'<a href="{link}">\u2192 \u043e\u0442\u043a\u0440\u044b\u0442\u044c</a>\n\n'
        blocks.append(block)

    text = header
    sent_count = 0
    for b in blocks:
        if len(text) + len(b) > TG_BUDGET:
            break
        text += b
        sent_count += 1

    remaining = len(enriched) - sent_count
    if remaining > 0:
        text += f"<i>…\u0435\u0449\u0451 {remaining} (\u043e\u0442\u0440\u0435\u0437\u0430\u043d\u043e \u043f\u043e \u043b\u0438\u043c\u0438\u0442\u0443 4096)</i>"

    tg_send(text)
    print(f"Sent {sent_count} of {len(enriched)} items.")

    state["seen"] = list(seen)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
