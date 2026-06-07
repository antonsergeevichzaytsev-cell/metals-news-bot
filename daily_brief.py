#!/usr/bin/env python3
"""Anton Daily — morning brief at 08:00 MSK Mon-Fri (also manual run on weekends).
v2: weekend fallback for phrases, verbose Yahoo error logging, multi-endpoint fetch.
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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Map weekday (0=Mon) -> categories from phrases.json that suit that day's theme.
DAY_CATEGORIES = {
    0: ["anchor", "rate"],                                # Money
    1: ["calibrated", "hard_q", "slowdown"],              # Calibrated truth
    2: ["bridge", "mirroring"],                            # Off-topic -> bring back
    3: ["closing", "objection"],                           # Sales close
    4: ["pitch", "storytelling", "russia", "principle"],   # Identity & principle
    5: ["principle", "russia", "pitch"],                   # Sat manual run fallback
    6: ["principle", "russia", "pitch"],                   # Sun manual run fallback
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
        # Final fallback: any phrase
        pool = phrases
    return pool[week_index % len(pool)] if pool else None


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_yahoo(symbol, timeout=10):
    # Try multiple endpoints/variants. Yahoo sometimes requires interval+range.
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
    ]
    last_err = None
    for url in urls:
        try:
            body = http_get(url, timeout=timeout)
            data = json.loads(body)
            result = data.get("chart", {}).get("result")
            if not result:
                err = data.get("chart", {}).get("error")
                last_err = f"empty result, err={err}"
                continue
            meta = result[0]["meta"]
            cur = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            if cur is None:
                last_err = f"no regularMarketPrice in meta keys={list(meta.keys())[:8]}"
                continue
            chg = ((cur - prev) / prev * 100.0) if (prev and prev > 0) else None
            return cur, chg
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} on {url[:60]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e} on {url[:60]}"
    raise RuntimeError(last_err or "all endpoints failed")


def fetch_stooq(symbol_stooq, timeout=10):
    """Fallback: stooq.com CSV. Returns (close, chg_pct_or_None)."""
    url = f"https://stooq.com/q/l/?s={symbol_stooq}&i=d"
    body = http_get(url, timeout=timeout)
    # CSV header: Symbol,Date,Time,Open,High,Low,Close,Volume
    lines = body.strip().split("\n")
    if len(lines) < 2:
        raise RuntimeError(f"stooq empty for {symbol_stooq}")
    parts = lines[1].split(",")
    if len(parts) < 7 or parts[6] in ("", "N/D"):
        raise RuntimeError(f"stooq no close for {symbol_stooq}: {lines[1][:80]}")
    close = float(parts[6])
    return close, None


def fetch_prices():
    """Returns dict {symbol: (price_per_tonne_usd, chg_pct_or_None, source_label)}.
    Tries Yahoo first (Al/Cu via CME proxy), falls back to stooq.
    """
    prices = {}

    # --- Aluminum ---
    try:
        p, c = fetch_yahoo("ALI=F")
        prices["Al"] = (p, c, "CME")
    except Exception as e:
        print(f"  ! yahoo ALI=F: {e}", file=sys.stderr)
        try:
            # Stooq symbol for CME aluminum futures
            p, c = fetch_stooq("ali.f")
            prices["Al"] = (p, c, "stooq")
        except Exception as e2:
            print(f"  ! stooq ali.f: {e2}", file=sys.stderr)

    # --- Copper (COMEX HG=F is $/lb -> convert to $/t, 2204.62 lb/t) ---
    try:
        p, c = fetch_yahoo("HG=F")
        prices["Cu"] = (p * 2204.62, c, "CME")
    except Exception as e:
        print(f"  ! yahoo HG=F: {e}", file=sys.stderr)
        try:
            p, c = fetch_stooq("hg.f")
            prices["Cu"] = (p * 2204.62, c, "stooq")
        except Exception as e2:
            print(f"  ! stooq hg.f: {e2}", file=sys.stderr)

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
        out += f"<i>\u041d\u0435\u0442 \u0444\u0440\u0430\u0437 \u0432 \u0431\u0430\u0437\u0435.</i>\n\n"

    prices = fetch_prices()
    if prices:
        out += f"<b>\U0001f4ca Markets</b>\n"
        parts = []
        for sym, (price, chg, src) in prices.items():
            if chg is None:
                parts.append(f"{sym} ${price:,.0f}/t <i>({src})</i>")
            else:
                arrow = "\u25b2" if chg > 0 else ("\u25bc" if chg < 0 else "\u00b7")
                parts.append(f"{sym} ${price:,.0f}/t {arrow}{abs(chg):.1f}% <i>({src})</i>")
        out += " \u00b7 ".join(parts) + "\n"
    else:
        out += "<i>\U0001f4ca Markets: \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d</i>\n"

    tg_send(out)
    cat_label = p["cat"] if p else "none"
    print(f"Sent: weekday={weekday}, cat={cat_label}, week_idx={iso_week}, prices={len(prices)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
