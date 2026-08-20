"""Живые цены на медь и алюминий через COMEX-фьючерсы (Yahoo Finance,
Stooq как fallback).

19.08.2026: вынесено из mission_control.py в отдельный модуль — до
этого была ТРЕТЬЯ независимая копия той же логики в bot_commands.py
(cmd_prices, без Stooq-fallback и без sanity-проверки), и она была
кандидатом для четвёртой копии в linkedin_ideas.py. Единственный
источник правды устраняет риск расхождения между копиями при будущих
правках (например если Yahoo сменит формат ответа — раньше пришлось
бы чинить в двух-трёх местах порознь, легко забыть одно).

Не LME напрямую — LME не даёт бесплатный JSON без ключа/подписки
(проверено 16.08.2026: Metals-API и официальный lme.com оба платные
или без API). COMEX HG=F/ALI=F торгуются с LME в жёсткой корреляции,
достаточно для оперативного контекста, но это не то же самое число,
что увидит трейдер на LME терминале — цена в центах/фунт, не $/тонна.
Никель и цинк намеренно НЕ показаны: ликвидного COMEX-эквивалента нет.
"""
import json
import sys
import urllib.request

import net

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Диапазоны за последние годы с запасом на волатильность, не биржевой лимит.
# Смысл не «поймать реальный ценовой шок», а «отличить реальную цену от мусора»:
# смена единиц (за фунт вместо за тонну), битый парсинг, зависший кэш источника —
# всё это выглядит как нормальный float, просто не в этом диапазоне.
PRICE_SANITY = {"Al": (1500, 5000), "Cu": (5000, 15000)}


def _http_get(url, timeout=10):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    with net.urlopen_retry(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def fetch_yahoo(symbol, timeout=10):
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
    ]
    last_err = None
    for url in urls:
        try:
            result = json.loads(_http_get(url, timeout)).get("chart", {}).get("result")
            if not result:
                last_err = "empty result"
                continue
            meta = result[0]["meta"]
            cur = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            if cur is None:
                last_err = "no regularMarketPrice"
                continue
            chg = ((cur - prev) / prev * 100.0) if (prev and prev > 0) else None
            return cur, chg
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    raise RuntimeError(last_err or "all failed")


def fetch_stooq(symbol, timeout=10):
    lines = _http_get(f"https://stooq.com/q/l/?s={symbol}&i=d", timeout).strip().split("\n")
    if len(lines) < 2:
        raise RuntimeError("stooq empty")
    parts = lines[1].split(",")
    if len(parts) < 7 or parts[6] in ("", "N/D"):
        raise RuntimeError("stooq no close")
    return float(parts[6]), None


def is_plausible_price(sym, price):
    lo, hi = PRICE_SANITY.get(sym, (0, float("inf")))
    return lo <= price <= hi


def fetch_prices():
    """Возвращает {sym: (price_usd_per_tonne, change_pct_or_None, source)}.
    source — 'CME' (Yahoo) или 'stooq'. Символ отсутствует в результате,
    если оба источника не дали правдоподобной цены."""
    prices = {}
    for sym, yf, sq, mult in (("Al", "ALI=F", "ali.f", 1.0), ("Cu", "HG=F", "hg.f", 2204.62)):
        for name, fn, arg in (("yahoo", fetch_yahoo, yf), ("stooq", fetch_stooq, sq)):
            try:
                p, c = fn(arg)
                val = p * mult
                if not is_plausible_price(sym, val):
                    print(f"  ! {name} {arg}: {val:.0f} outside sanity range for {sym} - treating as bad data", file=sys.stderr)
                    continue
                prices[sym] = (val, c, "CME" if name == "yahoo" else "stooq")
                break
            except Exception as e:
                print(f"  ! {name} {arg}: {e}", file=sys.stderr)
    return prices


def format_prices(prices):
    if not prices:
        return ""
    parts = []
    for sym, (price, chg, src) in prices.items():
        if chg is None:
            parts.append(f"{sym} ${price:,.0f}/t ({src})")
        else:
            arrow = "\u25b2" if chg > 0 else ("\u25bc" if chg < 0 else "\u00b7")
            parts.append(f"{sym} ${price:,.0f}/t {arrow}{abs(chg):.1f}% ({src})")
    return " \u00b7 ".join(parts)
