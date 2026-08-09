#!/usr/bin/env python3
"""Metals & Mining news digest -> Telegram.
v8: persist priority + company to history.json — чтобы F-блок не ранжировал заново.
v9 (09.08.2026): (1) native publisher RSS feeds добавлены в feeds.txt —
    охват шире, не только Google News reroute. (2) Deep-analysis второй
    проход DeepSeek для priority=high — структурный разбор что/кого/делать
    вместо одной строки. (3) UZCOPPER-тег (🏭) — узкий сигнал copper +
    Uzbekistan/Central Asia, отдельно от общего orbit, под текущую
    инженерную работу.
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

import net
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
# Окно возраста 48ч при ~50 публикациях в день + отказы модели: 500 хэшей
# покрывали окно впритык. 1500 — запас, файл всё равно копеечный.
SEEN_KEEP = 1500
# Сигнатуры заголовков для дедупа МЕЖДУ прогонами. 400 ~ трое суток выдачи
# при окне возраста 48ч — с запасом.
TITLE_SIGS_KEEP = 400
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
    "moomoo", "scanx.trade", "scanx",
    "openpr.com", "openpr",
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


def feed_name(url):
    """Короткое имя ленты для отчёта о здоровье: сам поисковый запрос."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]
        q = re.sub(r"\s*when:\d+[dhm]\s*", "", q).strip()
        return q[:44] or url[:44]
    except Exception:
        return url[:44]


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"seen": []}
    return {"seen": []}


def save_state(state):
    # Обрезаем ХВОСТ по порядку добавления. Раньше здесь было то же
    # выражение, но state["seen"] приходил из list(set(...)) — порядок
    # у множества произвольный, и обрезка выбрасывала случайные записи,
    # а не самые старые. Следствие 28.07-03.08: 5 ссылок ушли в Telegram
    # по второму разу. Порядок теперь держит dict (см. main).
    state["seen"] = state["seen"][-SEEN_KEEP:]
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
    # 7 дней x ~50 элементов = ~350. Кап 300 обрезал раньше заявленного
    # срока хранения — retention и cap противоречили друг другу.
    history["items"] = history["items"][-400:]
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
    """Возвращает (text, status). status == "ok" либо причина отказа.

    Молчаливый None недопустим: лента, которая тихо умерла, выглядит ровно
    как лента без новостей — тишина и зелёный прогон. Тот же принцип, что
    в filings.py, до 04.08.2026 здесь не был применён.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with net.urlopen_retry(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace"), "ok"
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}"
    except TimeoutError:
        return None, "timeout"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


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


# Кап MAX_ITEMS_PER_RUN упирается КАЖДЫЙ прогон (21 из 21 за 28.07-03.08).
# До 04.08 кандидаты сортировались только по дате — значит модель всегда
# платила за 12 самых свежих, а всё остальное не просматривалось вообще.
# Орбита идёт первой по тому же принципу, что в filings.py: если бюджет
# прогона кончится, он должен кончиться на общей макро-заметке, а не на
# активе, который Антона реально касается.
ORBIT_WORDS = [
    r"nornickel", r"norilsk", r"rusal", r"polyus", r"\bUMMC\b", r"\bERG\b",
    r"kazatomprom", r"kaz minerals", r"nordgold", r"polymetal",
    r"almalyk", r"uzcopper", r"navoi",
    r"uzbek\w*", r"kazakh\w*", r"kyrgyz\w*", r"tajik\w*", r"turkmen\w*",
    r"mongolia\w*", r"armenia\w*", r"azerbaijan\w*", r"georgia\w*",
    r"russia\w*", r"siberia\w*", r"\bural\w*", r"central asia\w*",
    r"\bCIS\b", r"caspian", r"oyu tolgoi", r"tashkent", r"almaty", r"astana",
]
ORBIT_RE = [re.compile(w, re.IGNORECASE) for w in ORBIT_WORDS]


def in_orbit(text):
    return any(p.search(text or "") for p in ORBIT_RE)


# UZCOPPER-специфичный сигнал, добавлено 09.08.2026. Уже, чем ORBIT: тот
# ловит весь СНГ/Центральную Азию для очереди приоритизации, этот — именно
# медь/переработка + Узбекистан/регион, под текущую работу техдиректором.
# Отдельный тег в digest'е (значок 🏭), не подмешивается в приоритет —
# фильтр под задачу, а не замена orbit-логике.
UZCOPPER_WORDS = [
    r"uzcopper", r"almalyk", r"navoi", r"uzbek\w*",
    r"\bcopper\b.*\b(concentrat\w*|smelt\w*|refin\w*|flotation)\b",
    r"\b(concentrat\w*|smelt\w*|refin\w*)\b.*\bcopper\b",
    r"copper cathode", r"copper concentrate", r"sx-ew", r"\bSX-EW\b",
]
UZCOPPER_RE = [re.compile(w, re.IGNORECASE) for w in UZCOPPER_WORDS]


def is_uzcopper_relevant(text):
    return any(p.search(text or "") for p in UZCOPPER_RE)


TITLE_STOPWORDS = {
    "the", "and", "for", "with", "after", "says", "amid", "from", "over",
    "into", "its", "new", "will", "set", "market", "chatter", "faces",
}


def title_tokens(title):
    toks = re.findall(r"[a-z0-9]+", (title or "").lower())
    return {t for t in toks if len(t) >= 3 and t not in TITLE_STOPWORDS}


def is_near_duplicate(sig, kept_sigs, threshold=0.5):
    for other in kept_sigs:
        if not sig or not other:
            continue
        inter = len(sig & other)
        union = len(sig | other)
        if union and inter / union >= threshold:
            return True
    return False


# --- DeepSeek enrichment ----------------------------------------------------

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

SYS_PROMPT = (
    "You are an analyst supporting a senior independent consultant in non-ferrous metals "
    "and mining (16 years across UC RUSAL, Norilsk Nickel, UMMC, ERG). For each news item, "
    "decide if it is relevant to his profile and, if relevant, produce ONE short Russian "
    "sentence (max 22 words) explaining why it matters for industry strategy. "
    "Reply ONLY with valid JSON: {\"skip\": bool, \"why\": str, \"priority\": str, \"company\": str}. "
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
    "ALSO assign priority for ranking. Be selective, but DO use \"high\" when it genuinely fits — do not leave high empty by default. "
    "\"high\" = directly actionable or strategically important FOR HIM: any concrete event, deal, project, or regulation involving his orbit (Nornickel, RUSAL, Polyus, UMMC, ERG, Kazatomprom, KAZ Minerals, Nordgold, Steppe Gold, Erdene), or a CIS / Central Asia / Mongolia asset, or a named junior/mid miner; AND ALSO a major development that is a direct competitor, threat, or opportunity to his players — e.g., a large new nickel/copper/aluminium/gold project or capacity expansion that competes with them, or a deal he could realistically plug into. "
    "\"medium\" is NOT the default bucket - it is the exception. Assign \"medium\" ONLY when the item has BOTH a named company or asset AND a concrete event carrying a number or a named cause (stated production change, capacity expansion, strike that stops output, M&A with disclosed value, price or premia move with a stated reason), and no direct line to his orbit. No number and no named cause means \"low\", not \"medium\". "
    "\"low\" = everything else, and this is the NORMAL verdict for most items: macro, market outlooks, price commentary without a named cause, carbon/CBAM/tax policy discussion, analyst themes, industry trends, and any piece with no specific operational event. If you hesitate between \"medium\" and \"low\", choose \"low\". "
    "FINALLY, set \"company\": the single named company or asset this item is about, as a short clean name usable as a research entry point (e.g. \"Kazzinc\", \"Almalyk MMC\", \"Nornickel / Talnakh\"). If the item is about a country, market, or policy with no single named company, set \"company\" to an empty string."
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
        with net.urlopen_retry(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
        content = resp["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"  ! deepseek error: {e}", file=sys.stderr)
        # Раньше здесь возвращалась заглушка skip=False — сбой сети превращался
        # в опубликованную новость без "почему важно" и с приоритетом low,
        # причём хэш помечался seen и элемент больше не возвращался. Тихая
        # потеря качества. Теперь None: элемент не публикуем, seen не трогаем,
        # он вернётся следующим прогоном. Как в filings.py.
        return None


DEEP_SYS_PROMPT = (
    "You are an analyst supporting a senior independent consultant in non-ferrous metals and "
    "mining (16 years across UC RUSAL, Norilsk Nickel, UMMC, ERG; FEL 1-3 CapEx programs; "
    "DD and turnaround experience). This news item was already flagged HIGH priority — his "
    "direct orbit or a strategic threat/opportunity to it. Go one level deeper than a one-line "
    "summary. In Russian, produce a short structured breakdown, 3 bullets max, each under 18 "
    "words, no fluff: "
    "(1) что произошло — the concrete fact, numbers if present; "
    "(2) кого касается — which named player(s) or asset(s) are directly affected, and how; "
    "(3) что делать — one concrete next action or question worth raising (e.g. what to verify, "
    "who to ask, what this changes for a live engagement), or an explicit empty string if there "
    "genuinely is no action beyond awareness — do not invent one. "
    "Reply ONLY with valid JSON: {\"what\": str, \"who\": str, \"action\": str}."
)


def deepseek_deep_analysis(title, desc, source, why, company):
    """Second-pass enrichment for priority=high items only.

    Не на весь поток — цена вызова x2 на 12 заметок за прогон незаметна,
    на весь candidates-список (до сотни) была бы ощутима. High уже
    отобран первым проходом, здесь просто разворачиваем его подробнее.
    """
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": DEEP_SYS_PROMPT},
            {"role": "user", "content": (
                f"SOURCE: {source}\nTITLE: {title}\nDESC: {desc[:800]}\n"
                f"ALREADY NOTED WHY (first pass): {why}\nCOMPANY: {company}"
            )},
        ],
        "temperature": 0.2,
        "max_tokens": 220,
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
        with net.urlopen_retry(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
        content = resp["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"  ! deepseek deep_analysis error: {e}", file=sys.stderr)
        # Сбой второго прохода не должен ронять итоговую заметку — она уже
        # прошла первый проход и заслужила публикацию. Просто не будет
        # расширенного блока, останется short "why" как раньше.
        return None


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
        with net.urlopen_retry(req, timeout=20) as r:
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
    # dict, а не set: нужен порядок добавления, иначе обрезка в save_state
    # режет произвольные хэши. Мембершип-тест "h in seen" работает так же.
    seen = dict.fromkeys(state.get("seen", []))

    print(f"Feeds: {len(feeds)}, keywords: {len(keywords)}, seen: {len(seen)}, history: {len(history.get('items', []))}")

    candidates = []
    health = {}
    n_raw = 0
    for url in feeds:
        name = feed_name(url)
        xml, status = fetch_feed(url)
        if xml is None:
            health[name] = status
            print(f"- {name}: {status}")
            continue
        items = parse_feed(xml)
        # Ответила 200, но 0 элементов — тоже поломка (сменился формат),
        # а не "новостей нет".
        health[name] = "ok" if items else "0 items"
        n_raw += len(items)
        print(f"- {name}: {status}, parsed {len(items)}")
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

    # Орбита первой, внутри группы — по свежести. Кап бьёт по хвосту,
    # а хвост теперь — не "всё, что старше", а "всё, что дальше от Антона".
    candidates.sort(key=lambda x: (
        not in_orbit(f"{x['title']} {x['desc']}"),
        -((x["pubdate"] or datetime.now(timezone.utc)).timestamp()),
    ))

    # Near-dup работал только ВНУТРИ прогона: одна и та же история от двух
    # источников в разные прогоны имеет разные ссылки -> разные хэши ->
    # дедуп по хэшу её не видит. Факт за 28.07-03.08: 9 таких пар
    # (First Quantum, Vale Q2, Vedanta, India gold tariff и др.).
    # Сигнатуры заголовков теперь переживают прогон.
    deduped = []
    kept_sigs = [set(x) for x in state.get("title_sigs", [])]
    n_cross_dup = 0
    for c in candidates:
        sig = title_tokens(c["title"])
        if is_near_duplicate(sig, kept_sigs):
            n_cross_dup += 1
            seen[c["hash"]] = None  # чтобы не пересчитывать её каждый прогон
            continue
        deduped.append(c)
        kept_sigs.append(sig)
    candidates = deduped
    # Держим только последние TITLE_SIGS_KEEP — окно шире возраста заметки,
    # но не растёт бесконечно.
    state["title_sigs"] = [sorted(x) for x in kept_sigs[-TITLE_SIGS_KEEP:]]
    if n_cross_dup:
        print(f"Near-duplicates dropped: {n_cross_dup}")

    print(f"Candidates after filter: {len(candidates)}")

    enriched = []
    n_skipped = 0
    n_model_errors = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        if len(enriched) >= MAX_ITEMS_PER_RUN:
            break
        verdict = deepseek_enrich(c["title"], c["desc"], c["domain"])
        if verdict is None:
            n_model_errors += 1
            continue
        if verdict.get("skip"):
            print(f"  . skip: {c['title'][:80]}")
            seen[c["hash"]] = None
            n_skipped += 1
            continue
        c["why"] = (verdict.get("why") or "").strip()
        c["priority"] = (verdict.get("priority") or "low").lower()
        c["company"] = (verdict.get("company") or "").strip()
        c["uzcopper"] = is_uzcopper_relevant(f"{c['title']} {c['desc']}")
        # Deep-analysis: только high, второй проход. На low/medium не тратим —
        # это был бы кап MAX_ITEMS_PER_RUN x2 вызовов на каждый прогон,
        # тогда как high обычно 1-3 заметки из 12.
        if c["priority"] == "high":
            deep = deepseek_deep_analysis(c["title"], c["desc"], c["domain"], c["why"], c["company"])
            c["deep"] = deep  # None если сбой — рендер ниже это обрабатывает
        else:
            c["deep"] = None
        enriched.append(c)
        seen[c["hash"]] = None
        # Append to history for F-block (CEO quote of the week)
        # priority и company уже посчитаны DeepSeek выше. Не сохранить их —
        # значит выбросить оплаченную работу: пятничный F-блок гонял бы
        # ранжирование заново по тем же самым новостям.
        history.setdefault("items", []).append({
            "ts": now_iso,
            "title": c["title"],
            "desc": c["desc"][:500],
            "domain": c["domain"],
            "link": c["link"],
            "why": c["why"],
            "priority": c["priority"],
            "company": c.get("company", ""),
            "uzcopper": c.get("uzcopper", False),
            "deep": c.get("deep"),
        })

    print(f"Enriched: {len(enriched)}")

    broken = {k: v for k, v in health.items() if v != "ok"}
    state["last_run"] = {
        "ts": now_iso,
        "raw": n_raw,
        "candidates": len(candidates),
        "screened": len(enriched) + n_skipped,
        "skipped_by_model": n_skipped,
        "model_errors": n_model_errors,
        "enriched": len(enriched),
        "cap": MAX_ITEMS_PER_RUN,
        "cap_hit": len(candidates) > MAX_ITEMS_PER_RUN,
        "feeds_total": len(health),
        "feeds_broken": len(broken),
        "broken": broken,
        "seen_size": len(seen),
        "cross_dups": n_cross_dup,
    }
    state["feed_health"] = health
    if broken:
        print(f"  ! broken feeds: {broken}")
    print(f"last_run: {state['last_run']}")

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

    # Ташкент, UTC+5. Переведено 05.08.2026 вместе с переездом: раньше
    # штамп стоял в MSK (UTC+3), и время в шапке дайджеста расходилось
    # с местным на два часа. Узбекистан переход на летнее время не
    # применяет, поэтому фиксированный сдвиг корректен круглый год.
    tst = timezone(timedelta(hours=5))
    now = datetime.now(tst).strftime("%d %b, %H:%M TST")

    targets = [c for c in enriched if c.get("priority") == "high"]
    rest = [c for c in enriched if c.get("priority") != "high"]

    sent_total = 0

    # --- Лента целей: только \U0001f534, отдельным сообщением, с входом для агента ---
    if targets:
        t_header = f"<b>\U0001f3af Цели \u2014 в разработку</b> \u2014 {now}\n\n"
        t_blocks = []
        for i, c in enumerate(targets, 1):
            title = esc(c["title"])
            link = esc(c["link"])
            domain = esc(c["domain"])
            why = esc(c["why"])
            company = esc(c.get("company", ""))
            uzc = "\U0001f3ed " if c.get("uzcopper") else ""
            block = f'\U0001f534 {uzc}<b>{i}.</b> <a href="{link}">{title}</a>\n'
            if why:
                block += f"<i>{domain}</i> \u00b7 \U0001f4a1 {why}\n"
            else:
                block += f"<i>{domain}</i>\n"
            # Deep-анализ: только если второй проход отработал (None на сбой
            # DeepSeek или на priority != high — по построению targets всегда high,
            # но deep может быть None при сетевой ошибке второго вызова).
            deep = c.get("deep")
            if deep:
                what = esc((deep.get("what") or "").strip())
                who = esc((deep.get("who") or "").strip())
                action = esc((deep.get("action") or "").strip())
                if what:
                    block += f"   \u2022 <b>что:</b> {what}\n"
                if who:
                    block += f"   \u2022 <b>кого:</b> {who}\n"
                if action:
                    block += f"   \u2022 <b>делать:</b> {action}\n"
            if company:
                block += f"\u2192 <code>asset-to-hook: {company}</code>\n\n"
            else:
                block += "\n"
            t_blocks.append(block)
        sent_total += tg_send_chunks(t_blocks, t_header)
        time.sleep(1.2)

    # --- Остальное: контекст, ниже ---
    if rest:
        r_header = f"<b>\U0001f9e0 Metals &amp; Mining \u2014 контекст</b> \u2014 {now}\n\n"
        r_blocks = []
        for i, c in enumerate(rest, 1):
            title = esc(c["title"])
            link = esc(c["link"])
            domain = esc(c["domain"])
            why = esc(c["why"])
            dot = PRIORITY_EMOJI.get(c.get("priority", "low"), "\u26aa")
            uzc = "\U0001f3ed " if c.get("uzcopper") else ""
            block = f'{dot} {uzc}<b>{i}.</b> <a href="{link}">{title}</a>\n'
            if why:
                block += f"<i>{domain}</i> \u00b7 \U0001f4a1 {why}\n\n"
            else:
                block += f"<i>{domain}</i>\n\n"
            r_blocks.append(block)
        sent_total += tg_send_chunks(r_blocks, r_header)

    print(f"Sent {len(targets)} target(s) + {len(rest)} context item(s) in {sent_total} message(s).")

    state["seen"] = list(seen)
    save_state(state)
    save_history(history)
    return 0


if __name__ == "__main__":
    sys.exit(main())
