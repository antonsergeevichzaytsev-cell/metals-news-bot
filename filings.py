#!/usr/bin/env python3
"""Filings watcher -> Telegram.

Источник — первичные корпоративные релизы с вайра TMX Newsfile, не журналистика.
Сигнал операционный: CapEx, ramp-up, металлургия, EPC, рестарт — с весом на CIS.
Выход — не новость, а повод написать: что сломано + чем Антон закрывает.
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
SOURCES_FILE = os.path.join(ROOT, "filings_sources.txt")
STATE_FILE = os.path.join(ROOT, "state_filings.json")
HISTORY_FILE = os.path.join(ROOT, "filings_history.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_API_KEY"]

MAX_ITEMS_PER_RUN = 14
MAX_AGE_HOURS = 36
TG_BUDGET = 3900
HISTORY_RETENTION_DAYS = 30
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
PRIORITY_EMOJI = {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "\u26aa"}

QUIET_START_MSK = 23
QUIET_END_MSK = 8
MSK = timezone(timedelta(hours=3))

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 80% вайра юниоров — размещения, опционы, назначения, конференции.
# Режем регуляркой по ЗАГОЛОВКУ до того, как платить за DeepSeek.
# Порядок важен: SIGNAL проверяется первым и перебивает NOISE
# ("Positive PEA and Concurrent Private Placement" — оставляем).

SIGNAL_WORDS = [
    r"feasibility stud\w*", r"\bPEA\b", r"preliminary economic assessment",
    r"pre-?feasibility", r"\bDFS\b", r"\bPFS\b", r"bankable",
    r"NI 43-?101", r"technical report", r"mineral resource estimate",
    r"mineral reserve", r"resource update",
    r"capital cost", r"\bcapex\b", r"initial capital", r"sustaining capital",
    r"cost overrun", r"budget increase", r"cost estimate",
    r"final investment decision", r"\bFID\b", r"construction decision",
    r"commission\w*", r"ramp-?up", r"nameplate", r"throughput",
    r"recover\w+ rate", r"metallurgic\w*", r"flowsheet", r"pilot plant",
    r"\bEPC\b", r"\bEPCM\b", r"\bFEED\b", r"front-?end engineering",
    r"engineering contract", r"contractor award\w*",
    r"production guidance", r"restart", r"care and maintenance",
    r"suspend\w*", r"suspension", r"force majeure", r"shutdown",
    r"schedule delay", r"delay\w* to", r"behind schedule",
    r"mining licen[cs]e", r"permit\w*", r"offtake",
    r"smelter", r"refiner\w+", r"concentrator", r"tailings",
    r"heap leach", r"autoclave", r"\bPOX\b", r"\bHPAL\b",
]

NOISE_WORDS = [
    r"private placement", r"non-?brokered", r"brokered financing",
    r"\bLIFE offering\b", r"flow-?through", r"closes offering",
    r"upsized", r"oversubscribed", r"unit offering", r"bought deal",
    r"warrant\w*", r"stock option\w*", r"option grant", r"\bRSU\b", r"\bDSU\b",
    r"shares for debt", r"debt settlement",
    r"appoint\w*", r"resign\w*", r"joins? (the )?board", r"board of directors",
    r"advisory board", r"names? \w+ as", r"steps down",
    r"annual general meeting", r"\bAGM\b", r"voting results",
    r"name change", r"symbol change", r"share consolidation",
    r"consolidation of shares", r"opening bell",
    r"to present at", r"presents at", r"conference", r"webinar",
    r"investor awareness", r"marketing agreement", r"\bIR\b agreement",
    r"accounting firm", r"\bauditor\b",
    r"\bOTCQB\b", r"uplisting", r"\bDTC\b eligib\w*",
    r"reverse takeover", r"\bRTO\b", r"semi-?annual reporting",
]

# Экспозиция, ради которой всё затевалось.
ORBIT_WORDS = [
    r"mongolia\w*", r"kazakh\w*", r"uzbek\w*", r"kyrgyz\w*", r"tajik\w*",
    r"turkmen\w*", r"armenia\w*", r"georgia\w*", r"azerbaijan\w*",
    r"russia\w*", r"siberia\w*", r"\bural\w*", r"central asia\w*",
    r"caspian", r"\bgobi\b", r"altai", r"tien shan", r"tian shan",
    r"almaty", r"astana", r"tashkent", r"ulaanbaatar", r"bishkek",
    r"oyu tolgoi", r"steppe gold", r"erdene", r"kazatomprom",
    r"\bCIS\b", r"\bERG\b", r"nornickel", r"norilsk", r"rusal", r"polyus",
]


def _compile(words):
    return [re.compile(w, re.IGNORECASE) for w in words]


SIGNAL_RE = _compile(SIGNAL_WORDS)
NOISE_RE = _compile(NOISE_WORDS)
ORBIT_RE = _compile(ORBIT_WORDS)


def any_hit(text, patterns):
    return any(p.search(text or "") for p in patterns)


def load_sources():
    out = []
    with open(SOURCES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                s = json.load(f)
                s.setdefault("seen", [])
                s.setdefault("pending", [])
                return s
        except Exception:
            pass
    return {"seen": [], "pending": []}


def save_state(state):
    state["seen"] = state["seen"][-800:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"items": []}


def save_history(history):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    history["items"] = [it for it in history.get("items", []) if it.get("ts", "") >= cutoff]
    history["items"] = history["items"][-400:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def fetch(url, timeout=20):
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
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
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


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", s).strip()


def parse_feed(xml_text):
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  ! parse error: {e}", file=sys.stderr)
        return items
    for item in root.iter("item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "desc": strip_html(item.findtext("description") or ""),
            "pubdate": parse_pubdate(item.findtext("pubDate") or ""),
        })
    return items


def is_recent(dt):
    if dt is None:
        return True
    return datetime.now(timezone.utc) - dt <= timedelta(hours=MAX_AGE_HOURS)


def url_hash(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def prefilter(title, desc):
    """True = отдаём DeepSeek. False = шум, режем бесплатно."""
    blob = f"{title} {desc}"
    if any_hit(blob, SIGNAL_RE):
        return True
    if any_hit(title, NOISE_RE):
        return False
    return True


DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

SYS_PROMPT = (
    "You screen PRIMARY corporate news releases from junior and mid-tier mining companies "
    "for a senior independent consultant in mining and non-ferrous metals. His sellable skills: "
    "owner-side CapEx project direction (FEL 1-3, $550M project at UMMC), operational turnaround "
    "(Norilsk Nickel, foundry-forge shop, 305 people, 0 LTI), metallurgy and process technology "
    "(RUSAL, 7 patents in aluminium alloys), EPC/EPCM contractor control and cost estimate review. "
    "His target market: junior/mid miners with CIS, Central Asia, Mongolia, Caucasus exposure. "
    "Your job is NOT to summarise news. It is to decide whether this release is a REASON TO CONTACT "
    "the company, and if so, give him the angle. "
    "Reply ONLY with valid JSON: "
    "{\"skip\": bool, \"signal\": str, \"company\": str, \"project\": str, \"region\": str, "
    "\"why\": str, \"hook\": str, \"priority\": str}. "
    "Set skip=true for anything with no operational or engineering substance: equity financings, "
    "private placements, warrants, option grants, personnel and board changes, AGM results, "
    "conference appearances, investor-awareness deals, share consolidations, listing housekeeping, "
    "auditor changes, pure exploration drill assays with no development or engineering decision, "
    "and generic corporate updates. When in doubt and there is no number and no engineering event, skip. "
    "Set skip=false ONLY when the release contains a concrete operational, technical or capital event: "
    "feasibility study / PEA / PFS / DFS results, a technical report, a stated CapEx or cost estimate "
    "or a change to one, cost overrun, construction or investment decision, commissioning or ramp-up "
    "status, throughput or recovery vs nameplate, metallurgical or flowsheet results, EPC/EPCM award "
    "or scope change or dispute, schedule slip, restart, suspension, care and maintenance, "
    "force majeure, production guidance change, permit or licence milestone for a development project, "
    "or an offtake tied to a plant. "
    "\"signal\" = the event type in Russian, 2-4 words (e.g. \"CapEx вырос\", \"срыв ramp-up\", "
    "\"EPCM меняют scope\", \"FS опубликован\", \"рестарт актива\", \"металлургия не вышла\"). "
    "\"company\" = clean company name. \"project\" = named asset or project, empty string if none. "
    "\"region\" = country or region of the asset, empty string if unclear. "
    "\"why\" = ONE Russian sentence, max 20 words: what is actually broken or at stake here, with the "
    "number if there is one. No marketing language, no company adjectives. "
    "\"hook\" = ONE Russian sentence, max 18 words: what HE specifically sells into this situation. "
    "Be concrete about his skill, not generic. "
    "\"priority\": "
    "\"high\" = operational or CapEx signal AND the asset sits in CIS / Central Asia / Mongolia / Caucasus, "
    "OR the company is in his orbit (Nornickel, RUSAL, Polyus, UMMC, ERG, Kazatomprom, KAZ Minerals, "
    "Steppe Gold, Erdene). This is an outbound target. "
    "\"medium\" = a strong operational signal his skills fit (CapEx overrun, ramp-up failure, EPC dispute, "
    "FS with a CapEx number) but the asset is outside that geography. Sellable, weaker. "
    "\"low\" = substantive but not actionable for him. Use \"high\" when it genuinely fits."
)


def deepseek_screen(title, desc):
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": f"TITLE: {title}\nBODY: {desc[:900]}"},
        ],
        "temperature": 0.2,
        "max_tokens": 260,
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
        with urllib.request.urlopen(req, timeout=40) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return json.loads(resp["choices"][0]["message"]["content"])
    except Exception as e:
        print(f"  ! deepseek error: {e}", file=sys.stderr)
        # Сбой модели не должен молча съедать элемент: хэш не помечаем,
        # вернётся в следующий прогон.
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


def render(c, idx):
    dot = PRIORITY_EMOJI.get(c.get("priority", "low"), "\u26aa")
    head = " \u00b7 ".join(x for x in [c.get("company", ""), c.get("project", ""), c.get("region", "")] if x)
    block = f'{dot} <b>{idx}. {esc(head)}</b>\n'
    if c.get("signal"):
        block += f"<b>{esc(c['signal'])}</b>\n"
    if c.get("why"):
        block += f"{esc(c['why'])}\n"
    if c.get("hook"):
        block += f"\u2192 {esc(c['hook'])}\n"
    link = esc(c["link"])
    block += f'<a href="{link}">релиз</a>\n'
    if c.get("company"):
        block += f"<code>asset-to-hook: {esc(c['company'])}</code>\n\n"
    else:
        block += "\n"
    return block


def in_quiet_hours(now_msk):
    h = now_msk.hour
    if QUIET_START_MSK > QUIET_END_MSK:
        return h >= QUIET_START_MSK or h < QUIET_END_MSK
    return QUIET_START_MSK <= h < QUIET_END_MSK


def main():
    sources = load_sources()
    state = load_state()
    history = load_history()
    seen = set(state.get("seen", []))
    now_msk = datetime.now(MSK)

    print(f"Sources: {len(sources)}, seen: {len(seen)}, pending: {len(state.get('pending', []))}")

    raw = []
    for url in sources:
        print(f"- {url}")
        xml = fetch(url)
        if not xml:
            continue
        items = parse_feed(xml)
        print(f"  parsed: {len(items)}")
        raw.extend(items)

    # Один релиз лежит и в mining-metals, и в precious-metals — режем по ссылке.
    by_hash = {}
    for it in raw:
        h = url_hash(it["link"])
        if h in seen or h in by_hash:
            continue
        if not is_recent(it["pubdate"]):
            continue
        it["hash"] = h
        by_hash[h] = it
    fresh = list(by_hash.values())
    print(f"Fresh & unseen: {len(fresh)}")

    candidates = []
    for it in fresh:
        if prefilter(it["title"], it["desc"]):
            candidates.append(it)
        else:
            seen.add(it["hash"])
    print(f"After noise prefilter: {len(candidates)} (dropped {len(fresh) - len(candidates)} free)")

    # Орбита первой: если бюджет прогона кончится, он кончится на юниоре
    # из Аризоны, а не на монгольском активе.
    candidates.sort(key=lambda x: (
        not any_hit(f"{x['title']} {x['desc']}", ORBIT_RE),
        -((x["pubdate"] or datetime.now(timezone.utc)).timestamp()),
    ))

    kept = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for c in candidates[:MAX_ITEMS_PER_RUN]:
        v = deepseek_screen(c["title"], c["desc"])
        if v is None:
            continue
        seen.add(c["hash"])
        if v.get("skip"):
            print(f"  . skip: {c['title'][:70]}")
            continue
        rec = {
            "link": c["link"],
            "title": c["title"],
            "signal": (v.get("signal") or "").strip(),
            "company": (v.get("company") or "").strip(),
            "project": (v.get("project") or "").strip(),
            "region": (v.get("region") or "").strip(),
            "why": (v.get("why") or "").strip(),
            "hook": (v.get("hook") or "").strip(),
            "priority": (v.get("priority") or "low").lower(),
        }
        kept.append(rec)
        history.setdefault("items", []).append(dict(rec, ts=now_iso))

    print(f"Kept: {len(kept)}")

    # Ночной улов не пингует — ждёт утра.
    queue = list(state.get("pending", [])) + kept
    if in_quiet_hours(now_msk):
        print(f"Quiet hours ({now_msk:%H:%M} MSK) — queued {len(queue)}, not sending.")
        state["pending"] = queue
        state["seen"] = list(seen)
        save_state(state)
        save_history(history)
        return 0

    if not queue:
        print("Nothing to send.")
        state["pending"] = []
        state["seen"] = list(seen)
        save_state(state)
        save_history(history)
        return 0

    queue.sort(key=lambda x: PRIORITY_RANK.get(x.get("priority", "low"), 2))
    stamp = now_msk.strftime("%d %b, %H:%M MSK")
    header = f"<b>\U0001f4dc Filings \u2014 сигнал</b> \u2014 {stamp}\n\n"
    blocks = [render(c, i) for i, c in enumerate(queue, 1)]
    n = tg_send_chunks(blocks, header)
    highs = sum(1 for c in queue if c.get("priority") == "high")
    print(f"Sent {len(queue)} item(s) ({highs} high) in {n} message(s).")

    state["pending"] = []
    state["seen"] = list(seen)
    save_state(state)
    save_history(history)
    return 0


if __name__ == "__main__":
    sys.exit(main())
