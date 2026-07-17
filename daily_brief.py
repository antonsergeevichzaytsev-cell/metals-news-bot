#!/usr/bin/env python3
"""Anton Daily — вооружение на день, 08:00 MSK Пн-Пт.
Фраза дня из phrases.json (48 шт, 13 категорий, ротация по дням) + пятничный F-блок.

Разделение труда с Mission Control (07:45):
  MC    — деньги и действия: urgent, pipeline, strategy, цены Al/Cu
  Daily — вооружение: фраза дня, топ-новость недели
Два сообщения — ноль пересечения.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
PHRASES_FILE = os.path.join(ROOT, "phrases.json")
HISTORY_FILE = os.path.join(ROOT, "history.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")  # optional

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DAY_CATEGORIES = {
    0: ["anchor", "rate"],
    1: ["calibrated", "hard_q", "slowdown"],
    2: ["bridge", "mirroring"],
    3: ["closing", "objection"],
    4: ["pitch", "storytelling", "russia", "principle"],
    5: ["principle", "russia", "pitch"],
    6: ["principle", "russia", "pitch"],
}

DAY_THEMES = {
    0: "\U0001f4b0 \u0414\u0435\u043d\u044c\u0433\u0438: anchor &amp; rate",
    1: "\U0001f3af Calibrated truth",
    2: "\U0001f309 Bridge: off-topic \u2192 \u0432\u043e\u0437\u0432\u0440\u0430\u0442",
    3: "\U0001f91d Close &amp; \u0432\u043e\u0437\u0440\u0430\u0436\u0435\u043d\u0438\u044f",
    4: "\U0001f194 Identity &amp; principle",
    5: "\U0001f4da \u0412\u044b\u0445\u043e\u0434\u043d\u043e\u0439: principles",
    6: "\U0001f4da \u0412\u044b\u0445\u043e\u0434\u043d\u043e\u0439: principles",
}

WEEKDAY_RU = {0: "\u041f\u043d", 1: "\u0412\u0442", 2: "\u0421\u0440", 3: "\u0427\u0442", 4: "\u041f\u0442", 5: "\u0421\u0431", 6: "\u0412\u0441"}


def load_phrases():
    with open(PHRASES_FILE, encoding="utf-8") as f:
        return json.load(f)


def pick_phrase(phrases, weekday, week_index):
    cats = DAY_CATEGORIES.get(weekday, [])
    pool = [p for p in phrases if p["cat"] in cats]
    if not pool:
        pool = phrases
    return pool[week_index % len(pool)] if pool else None


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


# --- F-block: top story of the week ----------------------------------------

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

F_BLOCK_PROMPT = (
    "You receive a list of metals and mining news from the past week, indexed by NUMBER. "
    "Each item has a title, source domain, a priority tag already assigned at ingest "
    "(high/medium, or '?' for older entries), and a Russian 'why it matters' note. "
    "Treat the priority tag as a strong prior - it was assigned by the same analysis. "
    "Pick THE SINGLE most strategically significant item for a senior independent consultant "
    "in non-ferrous metals and mining (16y at UC RUSAL, Norilsk Nickel, UMMC, ERG). "
    "Strong picks: (1) executive quote from named CEO/COO of a major operator, "
    "(2) M&A deal with disclosed value, (3) named regulatory shift (sanctions, tariffs, CBAM), "
    "(4) production data with concrete numbers, (5) named CapEx decision. "
    "Weak picks (avoid): generic market summaries, analyst commentary, no-name press releases. "
    "Return ONLY valid JSON: {\"pick\": int, \"for_call\": str}. "
    "'pick' = 0-based index of chosen item, or -1 if week was thin and nothing qualifies. "
    "'for_call' = ONE Russian sentence (max 25 words) on how Anton can reference this on "
    "an expert call or LinkedIn post. Start with 'На звонке:' or 'В посте:'."
)


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {"items": []}
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"items": []}


def pick_top_of_week(history):
    """Returns (item_dict, for_call_str) or (None, '') if nothing qualifies."""
    if not DEEPSEEK_KEY:
        return None, ""
    items = history.get("items", [])
    if not items:
        return None, ""
    # Trim to last 30 items max (most recent first)
    recent = items[-30:]

    # Утренний digest уже разметил приоритет через DeepSeek. Гонять ранжирование
    # заново — платить дважды за одну работу. Отсекаем явный low.
    # Записи без priority (до 17.07) ОСТАВЛЯЕМ: иначе первую неделю после
    # правки F-блок останется без материала. Ретеншн 7 дней вымоет их сам.
    pool = [it for it in recent if (it.get("priority") or "") != "low"]
    if not pool:
        pool = recent  # неделя вся low — пусть модель сама решит, есть ли там что-то

    # Build user message: numbered list
    lines = []
    for i, it in enumerate(pool):
        title = (it.get("title") or "")[:200]
        src = (it.get("domain") or "")[:40]
        why = (it.get("why") or "")[:200]
        pr = (it.get("priority") or "?")
        co = (it.get("company") or "")[:60]
        line = f"[{i}] {title} | src={src} | priority={pr} | why={why}"
        if co:
            line += f" | company={co}"
        lines.append(line)
    user_msg = "\n".join(lines)

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": F_BLOCK_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.3,
        "max_tokens": 200,
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
        verdict = json.loads(content)
        pick = verdict.get("pick", -1)
        for_call = (verdict.get("for_call") or "").strip()
        if pick is None or pick < 0 or pick >= len(pool):
            return None, ""
        return pool[pick], for_call
    except Exception as e:
        print(f"  ! deepseek F-block error: {e}", file=sys.stderr)
        return None, ""


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
        print(f"  ! telegram {e.code}: {body}", file=sys.stderr)
        raise


def main():
    msk = timezone(timedelta(hours=3))
    now = datetime.now(msk)
    weekday = now.weekday()
    iso_year, iso_week, _ = now.isocalendar()

    phrases = load_phrases()
    p = pick_phrase(phrases, weekday, iso_week)

    date_str = f"{now.strftime('%d %b')}, {WEEKDAY_RU.get(weekday, '?')}"
    out = f"<b>\u2600\ufe0f Anton Daily</b> \u2014 {date_str}\n\n"

    if p:
        theme = DAY_THEMES.get(weekday, "")
        out += f"<b>{theme}</b>\n"
        out += f"<i>[{esc(p['cat'])} \u00b7 {esc(p['lang'])}]</i>\n\n"
        out += f"\U0001f5e3\ufe0f {esc(p['text'])}\n\n"
        if p.get("use"):
            out += f"\U0001f4aa <i>{esc(p['use'])}</i>\n\n"

    # Цены Al/Cu живут в Mission Control (07:45, блок MARKETS).
    # Здесь они были бы дублем через 15 минут — убраны осознанно.

    # --- F-block on Fridays ---
    if weekday == 4:
        history = load_history()
        top, for_call = pick_top_of_week(history)
        if top:
            out += "\n<b>\U0001f3c6 \u0422\u043e\u043f-\u043d\u043e\u0432\u043e\u0441\u0442\u044c \u043d\u0435\u0434\u0435\u043b\u0438</b>\n"
            title = esc(top.get("title", ""))
            link = esc(top.get("link", ""))
            domain = esc(top.get("domain", ""))
            if link:
                out += f'<a href="{link}">{title}</a>\n'
            else:
                out += f"{title}\n"
            out += f"<i>{domain}</i>\n"
            if for_call:
                out += f"\U0001f4cc {esc(for_call)}\n"
        elif DEEPSEEK_KEY:
            out += "\n<i>\U0001f3c6 \u0422\u043e\u043f-\u043d\u043e\u0432\u043e\u0441\u0442\u044c \u043d\u0435\u0434\u0435\u043b\u0438: \u043d\u0435\u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e \u0434\u0430\u043d\u043d\u044b\u0445 \u0432 history</i>\n"

    tg_send(out)
    cat_label = p["cat"] if p else "none"
    print(f"Sent: weekday={weekday}, cat={cat_label}, week_idx={iso_week}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
