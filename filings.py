#!/usr/bin/env python3
"""Filings watcher -> Telegram.

Источник — первичные корпоративные релизы (TMX Newsfile + GlobeNewswire), не журналистика.
Сигнал операционный: CapEx, ramp-up, металлургия, EPC, рестарт — с весом на CIS.
Выход — не новость, а повод написать: что сломано + чем Антон закрывает.

Главное правило отбора — СТАДИЯ проекта. Навык продаётся только на своей стадии:
началась стройка — FEL и оценка затрат уже позади, предлагать их — ошибка.

Второе правило — слепота хуже поломки, на всех этажах:
- лента, которая тихо умерла, выглядит как лента без новостей -> fetch называет причину,
  смена статуса уезжает в Telegram один раз, по фронту;
- модель, которая режет лишнее, выглядит как чистая выдача -> отказы пишутся
  в history["skipped"] с причиной;
- лог Actions без токена не читается -> цифры прогона ложатся в state["last_run"].
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

MAX_ITEMS_PER_RUN = 40
MAX_AGE_HOURS = 36
TG_BUDGET = 3900
HISTORY_RETENTION_DAYS = 30
SKIPPED_RETENTION_DAYS = 7
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
PRIORITY_EMOJI = {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "\u26aa"}

# Стадия решает, какой из навыков вообще продаётся. На стройке FEL уже кончился.
STAGE_RU = {
    "exploration": "\u0440\u0430\u0437\u0432\u0435\u0434\u043a\u0430",
    "study": "\u0438\u0437\u0443\u0447\u0435\u043d\u0438\u0435",
    "financing": "\u0444\u0438\u043d\u0430\u043d\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435",
    "construction": "\u0441\u0442\u0440\u043e\u0439\u043a\u0430",
    "commissioning": "\u043f\u0443\u0441\u043a\u043e\u043d\u0430\u043b\u0430\u0434\u043a\u0430",
    "operating": "\u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442",
    "care_maintenance": "\u043a\u043e\u043d\u0441\u0435\u0440\u0432\u0430\u0446\u0438\u044f",
}

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
    r"CEO Clips", r"video -", r"interview",
]

# Экспозиция, ради которой всё затевалось.
ORBIT_WORDS = [
    r"mongolia\w*", r"kazakh\w*", r"uzbek\w*", r"kyrgyz\w*", r"tajik\w*",
    r"turkmen\w*", r"armenia\w*", r"georgia\w*", r"azerbaijan\w*",
    r"russia\w*", r"siberia\w*", r"\bural\w*", r"central asia\w*",
    r"caspian", r"\bgobi\b", r"altai", r"tien shan", r"tian shan",
    r"almaty", r"astana", r"tashkent", r"ulaanbaatar", r"bishkek",
    r"oyu tolgoi", r"steppe gold", r"erdene", r"bayan khundii", r"zuun mod",
    r"kazatomprom",
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
                s.setdefault("feed_health", {})
                return s
        except Exception:
            pass
    return {"seen": [], "pending": [], "feed_health": {}}


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
    sk_cutoff = (datetime.now(timezone.utc) - timedelta(days=SKIPPED_RETENTION_DAYS)).isoformat()
    history["skipped"] = [it for it in history.get("skipped", []) if it.get("ts", "") >= sk_cutoff][-500:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def feed_name(url):
    """Короткое имя ленты для отчёта."""
    if "newsfilecorp" in url:
        return "NF:" + url.rstrip("/").rsplit("/", 1)[-1]
    m = re.search(r"/industry/[\w]+-([^/]+)/", url)
    if m and "globenewswire" in url:
        return "GNW:" + urllib.parse.unquote(m.group(1))
    return url[:38]


def fetch(url, timeout=20):
    """Возвращает (text, status). status == "ok" либо причина отказа.

    Молчаливый None здесь недопустим. Лента, которая тихо умерла, выглядит
    ровно как лента без новостей: тишина и зелёный прогон. Причину надо знать.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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
    "to decide whether each one is a REASON FOR A SPECIFIC CONSULTANT TO CONTACT the company. "
    "You are NOT summarising news. "
    "\n\n"
    "THE CONSULTANT: 16 years owner-side in mining and non-ferrous metals. "
    "Each of his skills is sellable ONLY at a specific project stage: "
    "(a) cost estimate review, CapEx benchmarking, FEL 1-3 owner-side project direction ($550M project at UMMC) "
    "-> ONLY at study or financing stage, i.e. BEFORE the investment decision. "
    "(b) EPC/EPCM contractor control, owner-side cost and schedule control -> ONLY at construction stage. "
    "(c) operational turnaround, throughput vs nameplate, shop-floor productivity "
    "(Norilsk Nickel foundry-forge shop, 305 people, 0 LTI) -> ONLY at commissioning, operating or restart. "
    "(d) metallurgy, flowsheet, alloys, process technology (RUSAL, 7 patents) -> at study, commissioning or operating. "
    "Target market: junior/mid miners with CIS, Central Asia, Mongolia, Caucasus exposure. "
    "\n\n"
    "STEP 1 - read the release and fix the project \"stage\". Exactly one of: "
    "exploration, study, financing, construction, commissioning, operating, care_maintenance. "
    "STEP 2 - choose the hook ONLY from the skills valid at that stage. "
    "A hook that contradicts the stage is a WRONG ANSWER even if it sounds plausible. "
    "Once construction has started, FEL and cost estimation are OVER - never offer them. "
    "Once the plant is operating, CapEx benchmarking is OVER. "
    "If no skill is valid at that stage, set skip=true. "
    "\n\n"
    "CONTRASTIVE EXAMPLE. Release: Sixty North Gold begins road construction and site preparation "
    "at the Mon Gold Mine, targeting production this year. "
    "WRONG -> stage: construction, hook: \"Может потребоваться контроль строительства и оценка затрат на этапе FEL.\" "
    "Wrong twice: FEL ended when construction began, and \"может потребоваться\" is a guess, not an offer. "
    "RIGHT -> stage: construction, hook: \"Owner-side контроль подрядчика и графика: не дать стройке съесть бюджет до первого золота.\" "
    "Matches the stage, names one concrete thing he does, states it as a fact. "
    "Every hook must read like the RIGHT one: no \"может\", no \"возможно\", no \"предложить экспертизу\", "
    "no restating his CV back at the reader. "
    "\n\n"
    "Reply ONLY with valid JSON, keys in exactly this order: "
    "{\"skip\": bool, \"stage\": str, \"signal\": str, \"company\": str, \"project\": str, \"region\": str, "
    "\"why\": str, \"hook\": str, \"priority\": str}. "
    "\n\n"
    "Set skip=true for anything with no operational or engineering substance: equity financings, "
    "private placements, warrants, option grants, personnel and board changes, AGM results, "
    "conference appearances, investor-awareness deals, share consolidations, listing housekeeping, "
    "auditor changes, third-party items (insider buying, interviews, video clips) where the company is only "
    "the subject, pure exploration drill assays with no development or engineering decision, "
    "and generic corporate updates. When in doubt and there is no number and no engineering event, skip. "
    "WHEN skip=true you MUST still fill \"signal\" with a 2-4 word Russian reason for skipping "
    "(\"размещение\", \"назначение\", \"буровые пробы\", \"конференция\", \"третьи лица\") "
    "and leave the other fields empty. Without a reason the decision cannot be audited. "
    "\n\n"
    "Set skip=false ONLY for a concrete operational, technical or capital event: feasibility study / PEA / PFS / DFS "
    "results, technical report, a stated CapEx or cost estimate or a change to one, cost overrun, construction or "
    "investment decision, commissioning or ramp-up status, throughput or recovery vs nameplate, metallurgical or "
    "flowsheet results, EPC/EPCM award or scope change or dispute, schedule slip, restart, suspension, "
    "care and maintenance, force majeure, production guidance change, permit or licence milestone for a "
    "development project, or an offtake tied to a plant. "
    "\n\n"
    "\"signal\" = event type in Russian, 2-4 words (\"CapEx вырос\", \"срыв ramp-up\", \"EPCM меняют scope\", "
    "\"FS опубликован\", \"рестарт актива\", \"металлургия не вышла\"). "
    "\"company\" = clean name. \"project\" = named asset, empty string if none. \"region\" = country of the asset. "
    "\"why\" = ONE Russian sentence, max 20 words: what is broken or at stake, with the number if there is one. "
    "No marketing language. "
    "\"hook\" = ONE Russian sentence, max 18 words, per the CONTRASTIVE EXAMPLE above. "
    "\n\n"
    "\"priority\": \"high\" = valid stage-matched hook AND the asset is in CIS / Central Asia / Mongolia / Caucasus, "
    "OR the company is in his orbit (Nornickel, RUSAL, Polyus, UMMC, ERG, Kazatomprom, KAZ Minerals, Steppe Gold, "
    "Erdene). Outbound target. "
    "\"medium\" = strong stage-matched signal (CapEx overrun, ramp-up failure, EPC dispute, FS with a CapEx number) "
    "but outside that geography. "
    "\"low\" = substantive but not actionable for him."
)


def deepseek_screen(title, desc):
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": f"TITLE: {title}\nBODY: {desc[:900]}"},
        ],
        "temperature": 0.2,
        "max_tokens": 300,
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
        stage = c.get("stage", "")
        tail = f" \u00b7 <i>{esc(STAGE_RU.get(stage, stage))}</i>" if stage else ""
        block += f"<b>{esc(c['signal'])}</b>{tail}\n"
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


def render_health(changes):
    lines = []
    for name, was, now in changes:
        if now == "ok":
            lines.append(f"\u2705 <b>{esc(name)}</b> \u2014 \u043e\u0436\u0438\u043b\u0430 (\u0431\u044b\u043b\u043e: {esc(was or '?')})")
        else:
            tail = " \u2014 \u0440\u0430\u043d\u044c\u0448\u0435 \u0440\u0430\u0431\u043e\u0442\u0430\u043b\u0430" if was == "ok" else ""
            lines.append(f"\u26a0\ufe0f <b>{esc(name)}</b>: {esc(now)}{tail}")
    return "<b>\U0001f6e0 \u041b\u0435\u043d\u0442\u044b \u2014 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0435 \u0441\u0442\u0430\u0442\u0443\u0441\u0430</b>\n\n" + "\n".join(lines)


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
    health = {}
    for url in sources:
        name = feed_name(url)
        xml, status = fetch(url)
        if xml is None:
            health[name] = status
            print(f"- {name}: {status}")
            continue
        items = parse_feed(xml)
        # Ответила 200, но 0 элементов — это тоже поломка (сменился формат),
        # а не «новостей нет». Помечаем отдельно.
        health[name] = "ok" if items else "0 items"
        print(f"- {name}: {status}, parsed {len(items)}")
        raw.extend(items)

    broken = {k: v for k, v in health.items() if v != "ok"}
    print(f"Feeds: {len(health)} ok={len(health) - len(broken)} broken={len(broken)} {broken if broken else ''}")

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
    n_skipped = 0
    n_screened = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for c in candidates[:MAX_ITEMS_PER_RUN]:
        v = deepseek_screen(c["title"], c["desc"])
        if v is None:
            continue
        n_screened += 1
        seen.add(c["hash"])
        if v.get("skip"):
            reason = (v.get("signal") or "").strip() or "?"
            # Отказ модели — тоже решение. Не залогируешь — не узнаешь,
            # режет она мусор или твою орбиту.
            history.setdefault("skipped", []).append({
                "ts": now_iso,
                "title": c["title"][:130],
                "link": c["link"],
                "reason": reason,
                "orbit": bool(any_hit(f"{c['title']} {c['desc']}", ORBIT_RE)),
            })
            n_skipped += 1
            print(f"  . skip [{reason}]: {c['title'][:60]}")
            continue
        rec = {
            "link": c["link"],
            "title": c["title"],
            "stage": (v.get("stage") or "").strip(),
            "signal": (v.get("signal") or "").strip(),
            "company": (v.get("company") or "").strip(),
            "project": (v.get("project") or "").strip(),
            "region": (v.get("region") or "").strip(),
            "why": (v.get("why") or "").strip(),
            "hook": (v.get("hook") or "").strip(),
            "priority": (v.get("priority") or "low").lower(),
        }
        history.setdefault("items", []).append(dict(rec, ts=now_iso))
        # low не шлём: это инструмент для исходящих, а не лента для чтения.
        # В history оно остаётся — будет на чём калибровать порог.
        if rec["priority"] not in ("high", "medium"):
            print(f"  . low -> history only: {c['title'][:60]}")
            continue
        kept.append(rec)

    print(f"Kept: {len(kept)}")

    # Счётчики в state: лог Actions без токена не читается, а это — читается.
    # Заодно видно, упирается ли прогон в потолок.
    state["last_run"] = {
        "ts": now_iso,
        "raw": len(raw),
        "fresh": len(fresh),
        "candidates": len(candidates),
        "prefiltered_out": len(fresh) - len(candidates),
        "screened": n_screened,
        "skipped_by_model": n_skipped,
        "kept": len(kept),
        "cap": MAX_ITEMS_PER_RUN,
        "cap_hit": len(candidates) > MAX_ITEMS_PER_RUN,
        "feeds_broken": len(broken),
    }
    print(f"last_run: {state['last_run']}")

    # Ночной улов не пингует — ждёт утра.
    queue = list(state.get("pending", [])) + kept

    if in_quiet_hours(now_msk):
        # feed_health НЕ трогаем: не отчитались — значит переход не съеден,
        # утренний прогон обнаружит его заново и доложит.
        print(f"Quiet hours ({now_msk:%H:%M} MSK) - queued {len(queue)}, not sending.")
        state["pending"] = queue
        state["seen"] = list(seen)
        save_state(state)
        save_history(history)
        return 0

    # Докладываем по ФРОНТУ, а не по уровню: сломанная лента должна крикнуть
    # один раз, а не ныть пять раз в день.
    prev = state.get("feed_health", {})
    changes = [(n, prev.get(n), s) for n, s in health.items() if prev.get(n) != s]
    if changes:
        print(f"Feed health changed: {changes}")
        tg_send(render_health(changes))
        time.sleep(1.0)
    state["feed_health"] = health

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
