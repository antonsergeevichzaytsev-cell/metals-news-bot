import re

# --- Извлечение фактов: детерминированно, без модели -------------------------
# Смысл: цифры сейчас лежат внутри прозы поля "why" и сравнить их между
# прогонами нельзя. Регуляркой они вынимаются в поля, и появляется то, чего
# в системе нет нигде: сравнение одного проекта во времени.
#
# Почему НЕ через модель, хотя она и так вызывается: любая правка системного
# промпта меняет поведение стадийного гейта, а проверить это можно только
# живыми прогонами. Регулярка проверяется офлайн на всей истории.

_MULT = {"k": 1e-3, "thousand": 1e-3, "m": 1.0, "mm": 1.0, "million": 1.0,
         "b": 1e3, "bn": 1e3, "billion": 1e3}

# $2.7 billion | US$150 million | C$45M | A$1.2bn | 500,000 dollars
_MONEY = re.compile(
    r"(?:(?P<cur>US|C|A|CA|AU|NZ|HK)?\$|\bUSD\s*|\bCAD\s*|\bAUD\s*)"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<mult>k|thousand|mm|m|million|bn|b|billion)?\b",
    re.IGNORECASE)

# 900,000 tonnes | 60 million tonnes per year | 2.4 Mtpa | 5,000 t/d
_TONNES = re.compile(
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<mult>million|billion|m|bn|b)?\s*"
    r"(?P<unit>tonnes?|tons?|tpa|mtpa|t/d|tpd|oz|ounces?|lbs?|pounds?)\b",
    re.IGNORECASE)

# 92% recovery | recovery of 88.5% | grade of 1.4 g/t
_PCT = re.compile(r"(?P<num>\d{1,3}(?:\.\d+)?)\s*%", re.IGNORECASE)
_RECOV_CTX = re.compile(r"recover|extraction|yield", re.IGNORECASE)

# Q3 2027 | H1 2028 | in 2029 | by mid-2027
_HORIZON = re.compile(
    r"\b(?:(?P<qh>Q[1-4]|H[12])\s*)?(?P<year>20[2-4]\d)\b", re.IGNORECASE)

# Масштаб теста — критично для recovery: лабораторная цифра не равна заводской.
_SCALE = re.compile(
    r"\b(bench[- ]?scale|laboratory|lab[- ]?scale|pilot[- ]?(?:plant|scale)|"
    r"demonstration plant|commercial scale|full[- ]?scale)\b", re.IGNORECASE)


def _to_musd(num, mult):
    try:
        v = float(num.replace(",", ""))
    except ValueError:
        return None
    m = _MULT.get((mult or "").lower())
    if m is None:
        # голая сумма в долларах без множителя
        return round(v / 1e6, 4) if v >= 1e5 else None
    return round(v * m, 4)


def extract_facts(text):
    """Достаёт из текста релиза сравнимые числа. Пустой dict — норма."""
    t = text or ""
    out = {}

    money = []
    for mt in _MONEY.finditer(t):
        v = _to_musd(mt.group("num"), mt.group("mult"))
        if v is not None and 0.01 <= v <= 500000:
            money.append(v)
    if money:
        out["money_musd"] = sorted(set(money), reverse=True)[:3]

    tons = []
    for mt in _TONNES.finditer(t):
        try:
            v = float(mt.group("num").replace(",", ""))
        except ValueError:
            continue
        mult = (mt.group("mult") or "").lower()
        unit = mt.group("unit").lower()
        if mult in ("million", "m") or unit == "mtpa":
            v *= 1e6
        elif mult in ("billion", "bn", "b"):
            v *= 1e9
        tons.append({"value": v, "unit": unit})
    if tons:
        out["tonnage"] = tons[:3]

    # Процент берём ТОЛЬКО при контексте извлечения. Без него это доля
    # в сделке, рост выручки или процент выполнения — несравнимый мусор.
    # Проверено на истории: "NOVAGOLD ... Acquire 100%" давал percent=100.
    if _RECOV_CTX.search(t):
        pcts = [float(m.group("num")) for m in _PCT.finditer(t)
                if float(m.group("num")) <= 100]
        if pcts:
            out["percent"] = sorted(set(pcts), reverse=True)[:3]
            out["percent_is_recovery"] = True
            # Масштаб обязателен рядом с recovery: bench-scale 90% и
            # заводские 90% — разные факты под одним словом.
            sc = _SCALE.search(t)
            out["test_scale"] = sc.group(1).lower() if sc else "НЕ УКАЗАН"

    yrs = sorted({(m.group("qh") or "").upper() + (" " if m.group("qh") else "") + m.group("year")
                  for m in _HORIZON.finditer(t)})
    if yrs:
        out["horizon"] = yrs[:3]

    return out


def compare_facts(company, project, facts, history_items, limit=40):
    """Ищет предыдущие упоминания того же проекта и возвращает список сдвигов."""
    if not facts:
        return []
    key_c = (company or "").strip().lower()
    key_p = (project or "").strip().lower()
    if not key_c:
        return []
    prior = [i for i in history_items[-limit:]
             if (i.get("company") or "").strip().lower() == key_c
             and (not key_p or (i.get("project") or "").strip().lower() == key_p)
             and i.get("facts")]
    if not prior:
        return []
    last = prior[-1]
    pf, notes = last.get("facts", {}), []
    when = (last.get("ts") or "")[5:10]

    a = (facts.get("money_musd") or [None])[0]
    b = (pf.get("money_musd") or [None])[0]
    if a and b and abs(a - b) / max(b, 1e-9) >= 0.05:
        notes.append(f"сумма {b:,.0f} → {a:,.0f} млн $ (с {when})")

    ha, hb = facts.get("horizon"), pf.get("horizon")
    if ha and hb and ha[0] != hb[0]:
        notes.append(f"срок {hb[0]} → {ha[0]} (с {when})")

    ra, rb = facts.get("percent"), pf.get("percent")
    if facts.get("percent_is_recovery") and ra and rb and ra[0] != rb[0]:
        notes.append(f"извлечение {rb[0]}% → {ra[0]}% (с {when})")

    return notes
