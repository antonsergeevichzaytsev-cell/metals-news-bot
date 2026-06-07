#!/usr/bin/env python3
"""Metals & Mining digest bot v2 — HTML escape + Telegram fallback."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEEDS_FILE = "feeds.txt"
KEYWORDS_FILE = "keywords.txt"
STATE_FILE = "state.json"
MAX_AGE_HOURS = 24
MAX_ITEMS_PER_RUN = 10
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 metals-news-bot"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()

if not BOT_TOKEN or not CHAT_ID:
    print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
    sys.exit(1)


def load_list(path):
    p = Path(path)
    if not p.exists():
        print(f"WARN: {path} missing", file=sys.stderr)
        return []
    return [s.strip() for s in p.read_text(encoding="utf-8").splitlines()
            if s.strip() and not s.strip().startswith("#")]


def load_state():
    try:
        return set(json.loads(Path(STATE_FILE).read_text(encoding="utf-8")).get("seen", []))
    except Exception:
        return set()


def save_state(seen):
    keep = list(seen)[-1000:]
    Path(STATE_FILE).write_text(json.dumps({"seen": keep}, ensure_ascii=False), encoding="utf-8")


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def parse_pubdate(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def parse_feed(xml_bytes):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  parse error: {e}", file=sys.stderr)
        return items
    for it in root.iter("item"):
        items.append(dict(
            title=(it.findtext("title") or "").strip(),
            link=(it.findtext("link") or "").strip(),
            desc=strip_html(it.findtext("description") or ""),
            guid=(it.findtext("guid") or it.findtext("link") or "").strip(),
            pubdate=(it.findtext("pubDate") or "").strip(),
        ))
    atom_ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(atom_ns + "entry"):
        title_el = entry.find(atom_ns + "title")
        summary_el = entry.find(atom_ns + "summary") or entry.find(atom_ns + "content")
        id_el = entry.find(atom_ns + "id")
        upd_el = entry.find(atom_ns + "updated") or entry.find(atom_ns + "published")
        link_el = entry.find(atom_ns + "link")
        link = link_el.get("href") if link_el is not None else ""
        items.append(dict(
            title=(title_el.text if title_el is not None else "").strip(),
            link=link,
            desc=strip_html(summary_el.text if summary_el is not None else ""),
            guid=(id_el.text if id_el is not None else link).strip(),
            pubdate=(upd_el.text if upd_el is not None else "").strip(),
        ))
    return items


def match_keywords(text, keywords):
    t = text.lower()
    return [kw for kw in keywords if kw.lower() in t]


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


DEEPSEEK_SYSTEM = (
    "Ты — аналитик горно-металлургической отрасли. Твой собеседник — "
    "независимый консультант по mining & non-ferrous metals с 16-летним опытом "
    "(RUSAL, Nornickel, UMMC, ERG). Ему важны: aluminium/copper/nickel производство, "
    "CapEx-проекты, M&A в ГМК, Россия/СНГ, тарифы и санкции, LME-цены."
)

DEEPSEEK_USER_TMPL = (
    "Прочитай заголовок и описание новости. Дай одну короткую строку (15-30 слов, "
    "на русском) — почему это важно для эксперта по mining & metals. Если "
    "новость нерелевантна (политика без связи с металлами, крипта, общий бизнес) — "
    "ответь ровно одним словом: SKIP.\nНе используй markdown, эмодзи, кавычки.\n\n"
    "Заголовок: {title}\nОписание: {desc}"
)


def deepseek_comment(title, desc):
    if not DEEPSEEK_KEY:
        return None
    payload = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": DEEPSEEK_SYSTEM},
            {"role": "user", "content": DEEPSEEK_USER_TMPL.format(title=title[:300], desc=desc[:600])},
        ],
        "max_tokens": 120, "temperature": 0.3, "stream": False,
    }).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            resp = json.loads(r.read())
        text = resp["choices"][0]["message"]["content"].strip()
        text = text.strip(" \n\r\t.;,:\"'")
        if not text:
            return None
        if text.upper().startswith("SKIP"):
            return "SKIP"
        if len(text) > 250:
            text = text[:240].rsplit(" ", 1)[0] + "…"
        return text
    except Exception as e:
        print(f"  DeepSeek error: {e}", file=sys.stderr)
        return None


def telegram_send_raw(text, parse_mode="HTML"):
    url = TELEGRAM_API.format(token=BOT_TOKEN)
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "true"}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, None, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def send_telegram(text):
    ok, resp, err = telegram_send_raw(text, parse_mode="HTML")
    if ok:
        return resp
    print(f"  Telegram HTML send failed: {err}", file=sys.stderr)
    plain = re.sub(r"<[^>]+>", "", text)
    plain = plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    ok2, resp2, err2 = telegram_send_raw(plain[:4000], parse_mode=None)
    if ok2:
        print("  Sent as plain text fallback")
        return resp2
    print(f"  Plain text also failed: {err2}", file=sys.stderr)
    return {"ok": False}


def main():
    feeds = load_list(FEEDS_FILE)
    keywords = load_list(KEYWORDS_FILE)
    seen = load_state()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    print(f"Feeds: {len(feeds)} | Keywords: {len(keywords)} | Seen: {len(seen)} | DeepSeek: {'ON' if DEEPSEEK_KEY else 'OFF'}")

    relevant = []
    for feed_url in feeds:
        try:
            xml = fetch(feed_url)
            items = parse_feed(xml)
        except Exception as e:
            print(f"  FETCH FAIL {feed_url[:80]}: {e}", file=sys.stderr)
            continue
        kept = 0
        for it in items:
            if not it["guid"] or it["guid"] in seen:
                continue
            dt = parse_pubdate(it["pubdate"])
            if dt and dt < cutoff:
                continue
            haystack = f"{it['title']} {it['desc']}"
            matches = match_keywords(haystack, keywords)
            if not matches:
                continue
            it["matches"] = matches[:3]
            it["source"] = urllib.parse.urlparse(feed_url).netloc.replace("www.", "")
            it["dt"] = dt or datetime.now(timezone.utc)
            relevant.append(it)
            kept += 1
        print(f"  {feed_url[:80]}: parsed {len(items)}, kept {kept}")

    relevant.sort(key=lambda x: x["dt"], reverse=True)
    seen_titles = set()
    deduped = []
    for it in relevant:
        prefix = re.sub(r"\s+", " ", it["title"][:60].lower()).strip()
        if prefix in seen_titles:
            continue
        seen_titles.add(prefix)
        deduped.append(it)

    candidates = deduped[: MAX_ITEMS_PER_RUN * 2]
    enriched = []
    for it in candidates:
        comment = deepseek_comment(it["title"], it["desc"]) if DEEPSEEK_KEY else None
        if comment == "SKIP":
            seen.add(it["guid"])
            print(f"  AI dropped: {it['title'][:70]}")
            continue
        it["comment"] = comment
        enriched.append(it)
        if len(enriched) >= MAX_ITEMS_PER_RUN:
            break

    if not enriched:
        print("No new relevant items. Skipping send.")
        save_state(seen)
        return 0

    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    header_tag = "📰" if not DEEPSEEK_KEY else "🧠"
    lines = [f"<b>{header_tag} Metals &amp; Mining — {now_msk.strftime('%d %b, %H:%M')} MSK</b>"]
    for i, it in enumerate(enriched, 1):
        title = esc(it["title"][:220])
        link = esc(it["link"])
        src = esc(it["source"])
        tags = " ".join("#" + re.sub(r"[^A-Za-z0-9]+", "", m) for m in it["matches"][:2] if m)
        block = f'\n<b>{i}.</b> <a href="{link}">{title}</a>'
        if it.get("comment"):
            block += f'\n💡 <i>{esc(it["comment"])}</i>'
        block += f'\n<i>{src}</i>  {tags}'
        lines.append(block)

    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:3900] + "\n\n…"

    result = send_telegram(body)
    ok = result.get("ok")
    mid = result.get("result", {}).get("message_id") if ok else None
    print(f"Sent: ok={ok}, message_id={mid}")

    if ok:
        for it in enriched:
            seen.add(it["guid"])
        save_state(seen)
        print(f"State saved, {len(seen)} GUIDs tracked.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
