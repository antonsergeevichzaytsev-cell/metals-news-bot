#!/usr/bin/env python3
"""Metals & Mining news digest -> Telegram.
v7: persist enriched items to history.json (rolling 7-day window) for F-block use.
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

ROOT = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.path.join(ROOT, "feeds.txt")
KEYWORDS_FILE = os.path.join(ROOT, "keywords.txt")
STATE_FILE = os.path.join(ROOT, "state.json")
HISTORY_FILE = os.path.join(ROOT, "history.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_API_KEY"]

MAX_ITEMS_PER_RUN = 12
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
PRIORITY_EMOJI = {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "\u26aa"}
MAX_AGE_HOURS = 48
TG_BUDGET = 3900
HISTORY_RETENTION_DAYS = 7

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

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
    "hrtoday.in",
    "mshale.com",
    "discovermoosejaw.com",
}

SOURCE_LABEL_TO_DOMAIN = {
    "reuters": "reuters.com", "bloomberg": "bloomberg.com",
    "financial times": "ft.com", "ft": "ft.com",
    "wall street journal": "wsj.com", "wsj": "wsj.com",
    "argus media": "argusmedia.com",
    "s&p global commodity insights": "spglobal.com", "s&p global": "spglobal.com",
    "fastmarkets": "fastmarkets.com",
    "aluminium insider": "aluminiuminsider.com",
    "light metal age": "lightmetalage.com",
    "mining.com": "mining.com",
    "mining weekly": "miningweekly.com",
    "mining journal": "miningjournal.com",
    "the northern miner": "northernminer.com", "northern miner": "northernminer.com",
    "international mining": "im-mining.com",
    "steel times international": "steeltimesint.com",
    "bnamericas": "bnamericas.com", "kitco": "kitco.com",
    "shanghai metals market": "metal.com", "smm": "metal.com", "metal.com": "metal.com",
}


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


def load_history():
    """Returns {'items': [...]} or empty."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"items": []}
    return {"items": []}


def save_history(history):
    """Prune items older than HISTORY_RETENTION_DAYS, then write."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    history["items"] = [it for it in history.get("items", []) if it.get("ts", "") >= cutoff]
    # Hard cap to prevent runaway file size
    history["items"] = history["items"][-300:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


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
    "sentence (max 22 words) explaining why it matters for industry strategy. "
    "Reply ONLY with valid JSON: {\"skip\": bool, \"why\": str, \"priority\": str}. "
    "Set skip=true for: stock analyst ratings, ETF picks, EPS forecasts, financial blogs, "
    "macro-economy with no metals angle, political/military opinion without direct supply impact, "
    "celebrity/lifestyle, generic press releases without operational substance, "
    "HR/personnel news (appointments, promotions, hires, departures), "
    "local protests without project-cancellation evidence, "
    "award announcements, conferences without substance, ESG marketing without numbers. "
    "Set skip=false for: production data and quarterly output, smelter/refinery operations, "
    "M&A deals with disclosed value, CapEx decisions, regulation (tariffs, sanctions, CBAM, "
    "Section 232), price/premia movements with cause, technology shifts (inert anode, H2 DRI, "
    "HPAL, autonomous haulage), named operators' strategic moves with operational substance. "
    "ALSO assign priority for ranking. "
    "\"high\" = actionable for him: a problem, deal, or regulation at a specific CIS / Central Asia / Mongolia asset or a junior/mid miner; "
    "and ALWAYS high for his orbit: Nornickel, RUSAL, Polyus, UMMC, ERG, Kazatomprom, KAZ Minerals, Nordgold, Steppe Gold, Erdene, and any CIS-exposed junior or mid miner. "
    "\"medium\" = know-but-not-urgent: trends, technology, or moves of global majors (BHP, Rio Tinto, Glencore, Vale, Anglo American, Freeport, Codelco, Newmont, Barrick, Zijin), notable price moves. "
    "\"low\" = general context."
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
        return {"skip": False, "why": "", "priority": "low"}


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


def tg_send_chunks(blocks, header):
    msgs = []
    cur = header
    for b in blocks:
        if len(cur) + len(b) > TG_BUDGET:
            msgs.append(cur.rstrip())
            cur = b
        else:
            cur += b
    if cur.strip():
        msgs.append(cur.rstrip())

    total = len(msgs)
    for i, m in enumerate(msgs, 1):
        if total > 1:
            m = m + f"\n\n<i>({i}/{total})</i>"
        tg_send(m)
        if i < total:
            time.sleep(1.2)
    return total


def main():
    feeds = load_feeds()
    keywords = load_keywords()
    state = load_state()
    history = load_history()
    seen = set(state.get("seen", []))

    print(f"Feeds: {len(feeds)}, keywords: {len(keywords)}, seen: {len(seen)}, history: {len(history.get('items', []))}")

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
    now_iso = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        if len(enriched) >= MAX_ITEMS_PER_RUN:
            break
        verdict = deepseek_enrich(c["title"], c["desc"], c["domain"])
        if verdict.get("skip"):
            print(f"  . skip: {c['title'][:80]}")
            seen.add(c["hash"])
            continue
        c["why"] = (verdict.get("why") or "").strip()
        c["priority"] = (verdict.get("priority") or "low").lower()
        enriched.append(c)
        seen.add(c["hash"])
        # Append to history for F-block (CEO quote of the week)
        history.setdefault("items", []).append({
            "ts": now_iso,
            "title": c["title"],
            "desc": c["desc"][:500],
            "domain": c["domain"],
            "link": c["link"],
            "why": c["why"],
        })

    print(f"Enriched: {len(enriched)}")

    if not enriched:
        print("Nothing to send.")
        state["seen"] = list(seen)
        save_state(state)
        save_history(history)
        return 0

    enriched.sort(key=lambda x: (
        PRIORITY_RANK.get(x.get("priority", "low"), 2),
        -((x["pubdate"] or datetime.now(timezone.utc)).timestamp()),
    ))

    msk = timezone(timedelta(hours=3))
    now = datetime.now(msk).strftime("%d %b, %H:%M MSK")
    header = f"<b>\U0001f9e0 Metals &amp; Mining</b> \u2014 {now}\n\n"

    blocks = []
    for i, c in enumerate(enriched, 1):
        title = esc(c["title"])
        link = esc(c["link"])
        domain = esc(c["domain"])
        why = esc(c["why"])
        dot = PRIORITY_EMOJI.get(c.get("priority", "low"), "\u26aa")
        block = f'{dot} <b>{i}.</b> <a href="{link}">{title}</a>\n'
        if why:
            block += f"<i>{domain}</i> \u00b7 \U0001f4a1 {why}\n\n"
        else:
            block += f"<i>{domain}</i>\n\n"
        blocks.append(block)

    sent = tg_send_chunks(blocks, header)
    print(f"Sent {len(enriched)} items in {sent} message(s).")

    state["seen"] = list(seen)
    save_state(state)
    save_history(history)
    return 0


if __name__ == "__main__":
    sys.exit(main())
