#!/usr/bin/env python3
"""Account watch -> Telegram.

filings.py ловит НОВЫХ кандидатов из первичных релизов. digest.py — общий
рыночный фон. Ни один не следит за компаниями, которые УЖЕ в работе
(pipeline.json, канал direct_outreach) — Seligdar, District Metals и т.д.
Узнать, что там шевельнулось, можно было только случайно через общий digest.

Этот бот — персональный, не тематический: для каждого живого лида с доменом
строит именной Google News запрос и следит только за ним. Не подменяет
pipeline_sync (тот читает почту, этот читает новости) — они про разные каналы
сигнала об одной и той же компании.

Имя компании выводится из домена эвристикой (см. company_name_guess).
Если эвристика ошиблась — правь account_overrides.json, не код.
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
PIPELINE_PATH = os.path.join(ROOT, "pipeline.json")
OVERRIDES_PATH = os.path.join(ROOT, "account_overrides.json")
STATE_PATH = os.path.join(ROOT, "state_account_watch.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

DEAD_STATUSES = {"dead", "closed", "declined", "done", "channel_failed"}
MAX_AGE_HOURS = 30  # окно поиска чуть шире периода прогона — не терять на стыке

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Домен -> шум. Публичные новостные площадки на этих доменах почти всегда
# не про саму компанию, а про инфраструктуру/платформу — фильтруем на выходе,
# не в запросе (запрос всё равно должен быть широким).
GENERIC_NOISE_DOMAINS = {
    "wikipedia.org", "linkedin.com", "crunchbase.com", "glassdoor.com",
    "indeed.com", "zoominfo.com", "opencorporates.com",
}


def load_pipeline():
    with open(PIPELINE_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_overrides():
    if os.path.exists(OVERRIDES_PATH):
        try:
            with open(OVERRIDES_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception:
            pass
    return {}


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                s = json.load(f)
                s.setdefault("seen", [])
                s.setdefault("hits", [])
                return s
        except Exception:
            pass
    return {"seen": [], "hits": []}


def save_state(state):
    state["seen"] = state["seen"][-1500:]
    # hits — не только хэши, а компания+заголовок+дата: другие боты
    # (evening_digest, mission_control) читают это, чтобы показать ПО КАКОЙ
    # компании было движение, не только "account_watch жив". Retention
    # короче, чем seen (7 дней хватает для дневного/недельного свода).
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    state["hits"] = [h for h in state.get("hits", []) if h.get("ts", "") >= cutoff][-200:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def company_name_guess(domain):
    """Грубая эвристика: TALCO-s.tj -> Talco, districtmetals.com -> Districtmetals.
    Специально грубая — точность важнее для override-файла, чем для кода.
    Составные домены (solidcore-resources) лучше искать целиком, не резать."""
    root = domain.split(".")[0]
    root = re.sub(r"[-_]", " ", root)
    return root.strip().title()


def watch_targets(pipeline, overrides):
    """Живые direct_outreach лиды с доменом. Мёртвые не следим -
    решение закрыть канал уже принято, новостной шум по нему не нужен.

    Приоритет имени: company_name из лида (DeepSeek, проставлено при заведении
    pipeline_sync'ом) -> ручной override -> грубая эвристика из домена.
    Первый источник закрывает проблему на входе — override нужен только
    для старых лидов, заведённых до этого поля, или если DeepSeek ошибся."""
    targets = []
    for lead in pipeline.get("leads", []):
        if lead.get("status") in DEAD_STATUSES:
            continue
        if lead.get("channel") != "direct_outreach":
            continue
        domain = lead.get("to_domain")
        if not domain:
            continue
        name = lead.get("company_name") or overrides.get(domain) or company_name_guess(domain)
        targets.append({"lead_id": lead["id"], "domain": domain,
                         "name": name, "topic": lead.get("topic", "")})
    return targets


def build_query_url(name):
    q = urllib.parse.quote(f'"{name}"')
    is_cyrillic = bool(re.search(r"[\u0400-\u04FF]", name))
    if is_cyrillic:
        return f"https://news.google.com/rss/search?q={q}+when:2d&hl=ru&gl=RU&ceid=RU:ru"
    return f"https://news.google.com/rss/search?q={q}+when:2d&hl=en-US&gl=US&ceid=US:en"


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace"), "ok"
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def parse_pubdate(s):
    if not s:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
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
    return re.sub(r"\s+", " ", s).strip()


def parse_feed(xml_text):
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        src_domain = urllib.parse.urlparse(link).netloc.replace("www.", "")
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": link,
            "desc": strip_html(item.findtext("description") or ""),
            "pubdate": parse_pubdate(item.findtext("pubDate") or ""),
            "src_domain": src_domain,
        })
    return items


def is_recent(dt):
    if dt is None:
        return True
    return datetime.now(timezone.utc) - dt <= timedelta(hours=MAX_AGE_HOURS)


def url_hash(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return (resp.get("result") or {}).get("message_id")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ! telegram error {e.code}: {body}", file=sys.stderr)
        return None


def render(target, item):
    title = esc(item["title"])
    link = esc(item["link"])
    src = esc(item["src_domain"])
    block = f"\U0001f440 <b>Движение по счёту</b>\n"
    block += f"<b>{esc(target['name'])}</b>"
    if target["topic"]:
        block += f" \u00b7 <i>{esc(target['topic'][:60])}</i>"
    block += "\n"
    block += f'<a href="{link}">{title}</a>\n'
    block += f"<i>{src}</i>\n"
    block += f"<code>lead: {esc(target['lead_id'])}</code>"
    return block


def main():
    pipeline = load_pipeline()
    overrides = load_overrides()
    state = load_state()
    seen = set(state.get("seen", []))
    first_run = "last_run" not in state
    # Первый прогон: seen пуст -> без защиты улетело бы разом всё за 2 дня
    # по всем компаниям. Молча засеваем seen и ничего не шлём - дальше бот
    # видит только НОВОЕ, как и задумано.
    if first_run:
        print("First run detected - seeding seen-set silently, no messages sent this run.")

    targets = watch_targets(pipeline, overrides)
    print(f"Watching {len(targets)} account(s): {[t['name'] for t in targets]}")

    if not targets:
        save_state(state)
        print("No live direct_outreach leads with a domain - nothing to watch.")
        return 0

    n_sent = 0
    n_errors = 0
    hits = state.get("hits", [])
    for target in targets:
        url = build_query_url(target["name"])
        xml, status = fetch(url)
        if xml is None:
            n_errors += 1
            print(f"  ! {target['name']}: {status}", file=sys.stderr)
            continue
        items = parse_feed(xml)
        for it in items:
            if it["src_domain"] in GENERIC_NOISE_DOMAINS:
                continue
            if not is_recent(it["pubdate"]):
                continue
            h = url_hash(it["link"])
            if h in seen:
                continue
            seen.add(h)
            if first_run:
                continue
            mid = tg_send(render(target, it))
            if mid:
                n_sent += 1
                hits.append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "lead_id": target["lead_id"],
                    "company": target["name"],
                    "title": it["title"][:150],
                    "link": it["link"],
                    "src_domain": it["src_domain"],
                })
            time.sleep(1.0)
        time.sleep(0.5)  # вежливая пауза между запросами к Google News

    state["seen"] = list(seen)
    state["hits"] = hits
    state["last_run_targets"] = len(targets)
    state["last_run_sent"] = n_sent
    save_state(state)
    print(f"Done. Targets: {len(targets)}, sent: {n_sent}, feed errors: {n_errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
