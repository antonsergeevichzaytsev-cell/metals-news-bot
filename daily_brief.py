#!/usr/bin/env python3
"""Anton Daily — morning brief at 08:00 MSK Mon-Fri.
Sends ONE phrase-of-the-day + LME proxy prices (CME futures via Yahoo Finance).
Separate from digest.py (news at 08:30 / 20:00 MSK).
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

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Map weekday (0=Mon) -> categories from phrases.json that suit that day's theme.
DAY_CATEGORIES = {
    0: ["anchor", "rate"],                                # Money
    1: ["calibrated", "hard_q", "slowdown"],              # Calibrated truth
    2: ["bridge", "mirroring"],                            # Off-topic -> bring back
    3: ["closing", "objection"],                           # Sales close
    4: ["pitch", "storytelling", "russia", "principle"],   # Identity & principle
}

DAY_THEMES = {
    0: "\U0001f4b0 \u0414\u0435\u043d\u044c\u0433\u0438: anchor &amp; rate",  # Money: anchor & rate
    1: "\U0001f3af Calibrated truth",
    2: "\U0001f309 Bridge: off-topic \u2192 \u0432\u043e\u0437\u0432\u0440\u0430\u0442",  # Bridge -> возврат
    3: "\U0001f91d Close &amp; \u0432\u043e\u0437\u0440\u0430\u0436\u0435\u043d\u0438\u044f",  # Close & возражения
    4: "\U0001f194 Identity &amp; principle",
}

WEEKDAY_RU = {0: "\u041f\u043d", 1: "\u0412\u0442", 2: "\u0421\u0440", 3: "\u0427\u0442", 4: "\u041f\u0442", 5: "\u0421\u0431", 6: "\u0412\u0441"}


def load_phrases():
    with open(PHRASES_FILE, encoding="utf-8") as f:
        return json.load(f)


def pick_phrase(phrases, weekday, week_index):
    cats = DAY_CATEGORIES.get(weekday, [])
    pool = [p for p in phrases if p["cat"] in cats]
    if not pool:
        return None
    return pool[week_index % len(pool)]


def fetch_yahoo(symbol, timeout=10):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    meta = data["chart"]["result"][0]["meta"]
    cur = meta["regularMarketPrice"]
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    chg = ((cur - prev) / prev * 100.0) if (prev and prev > 0) else None
    return cur, chg


def fetch_prices():
    """Returns dict {symbol: (price_per_tonne_usd, chg_pct_or_None)}.
    LME data is paywalled; we use CME futures as proxy. Correlation ~95% for Al/Cu.
    """
    prices = {}
    # Aluminum — CME ALI=F is already $/t
    try:
        p, c = fetch_yahoo("ALI=F")
        prices["Al"] = (p, c)
    except Exception as e:
        print(f"  ! yahoo ALI=F: {e}", file=sys.stderr)
    # Copper — COMEX HG=F is $/lb, convert to $/t (1 t = 2204.62 lb)
    try:
        p, c = fetch_yahoo("HG=F")
        prices["Cu"] = (p * 2204.62, c)
    except Exception as e:
        print(f"  ! yahoo HG=F: {e}", file=sys.stderr)
    return prices


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
    weekday = now.weekday()  # 0=Mon
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
    else:
        out += f"<i>\u041d\u0435\u0442 \u0444\u0440\u0430\u0437 \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0434\u043d\u044f.</i>\n\n"

    prices = fetch_prices()
    if prices:
        out += f"<b>\U0001f4ca Markets</b> <i>(CME proxy, \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 LME 3M API)</i>\n"
        parts = []
        for sym, (price, chg) in prices.items():
            if chg is None:
                parts.append(f"{sym} ${price:,.0f}/t")
            else:
                arrow = "\u25b2" if chg > 0 else ("\u25bc" if chg < 0 else "\u00b7")
                parts.append(f"{sym} ${price:,.0f}/t {arrow}{abs(chg):.1f}%")
        out += " \u00b7 ".join(parts) + "\n"
    else:
        out += "<i>\U0001f4ca Markets: \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d</i>\n"

    tg_send(out)
    cat_label = p["cat"] if p else "none"
    print(f"Sent: weekday={weekday}, cat={cat_label}, week_idx={iso_week}, prices={len(prices)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
