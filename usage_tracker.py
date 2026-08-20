"""Учёт расходов на DeepSeek API — 20.08.2026.

До этого момента ни один из семи мест, вызывающих DeepSeek (digest.py x2,
bot_commands.py, daily_brief.py, filings.py, linkedin_ideas.py,
mission_control.py), не читал поле "usage" из ответа API — притом что
DeepSeek присылает его бесплатно в каждом ответе. Реальные деньги,
никакой видимости: если один прогон вдруг зациклится и сожжёт бюджет за
час, никто не узнает до месячного счёта.

Тарифы DeepSeek-V4-Flash (единственная модель, которую использует этот
бот — см. digest.py:deepseek_enrich) с 16.08.2026, per 1M tokens:
  cache-hit input:  $0.007  off-peak / $0.014 peak
  cache-miss input: $0.22   off-peak / $0.44  peak
  output:           $0.66   off-peak / $1.32  peak
Peak: 01:00-04:00 и 06:00-10:00 UTC. Все остальные часы — off-peak.
Источник: официальная DeepSeek Models & Pricing page, сверено 16.08.2026
(независимые трекеры цен, т.к. прямой доступ к api-docs.deepseek.com
недоступен из среды разработки — см. network allowlist). Тарифы меняются
без предупреждения (уже менялись минимум дважды в 2026: 01.07 плоская
ставка -> 16.08 peak/off-peak) — ЭТИ ЦИФРЫ УСТАРЕЮТ, сверяй раз в
несколько месяцев на https://api-docs.deepseek.com/quick_start/pricing.

Использование в любом модуле, вызывающем DeepSeek:
    import usage_tracker as ut
    ...
    resp = json.loads(r.read().decode("utf-8"))
    ut.record_usage("digest.deepseek_enrich", resp.get("usage", {}))
    content = resp["choices"][0]["message"]["content"]

Хранит только счётчики токенов и стоимость в state_usage.json (не
prompts/responses — те могут содержать чувствительные данные заголовков
новостей, но не секреты; хранить сами тексты для учёта трат не нужно).
"""
import json
import os
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
USAGE_PATH = os.path.join(ROOT, "state_usage.json")

# 1M tokens, USD. Обновить при сверке с официальной pricing page.
RATES_OFF_PEAK = {"cache_hit": 0.007, "cache_miss": 0.22, "output": 0.66}
RATES_PEAK = {"cache_hit": 0.014, "cache_miss": 0.44, "output": 1.32}

PEAK_HOURS_UTC = set(range(1, 4)) | set(range(6, 10))  # 01-04, 06-10

# Хранить дневные записи дольше этого — бессмысленно (месячный обзор
# явно достаточен), файл не должен расти неограниченно.
RETENTION_DAYS = 60


def is_peak_now(now=None):
    now = now or datetime.now(timezone.utc)
    return now.hour in PEAK_HOURS_UTC


def _load():
    if not os.path.exists(USAGE_PATH):
        return {"daily": {}}
    try:
        with open(USAGE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"daily": {}}


def _save(data):
    cutoff = (datetime.now(timezone.utc).date().toordinal() - RETENTION_DAYS)
    data["daily"] = {
        d: v for d, v in data.get("daily", {}).items()
        if datetime.fromisoformat(d).toordinal() >= cutoff
    }
    with open(USAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def compute_cost_usd(usage, peak=None):
    """usage — словарь из resp['usage'] (может быть частично пустым,
    например если поля cache_hit/cache_miss отсутствуют у старых
    ответов — тогда считаем весь prompt как cache_miss, консервативная
    оценка сверху, не занижает расход)."""
    if peak is None:
        peak = is_peak_now()
    rates = RATES_PEAK if peak else RATES_OFF_PEAK

    hit = usage.get("prompt_cache_hit_tokens", 0) or 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        # Ответ без разбивки по кэшу (не должно происходить у DeepSeek,
        # но не крашимся, если формат когда-то изменится) — весь prompt
        # трактуем как miss, это верхняя граница расхода, не нижняя.
        miss = max((usage.get("prompt_tokens", 0) or 0) - hit, 0)
    out = usage.get("completion_tokens", 0) or 0

    cost = (hit / 1e6) * rates["cache_hit"] + (miss / 1e6) * rates["cache_miss"] + (out / 1e6) * rates["output"]
    return cost


def record_usage(caller, usage):
    """Записывает одно обращение к DeepSeek. caller — строка вида
    'module.function' для последующей разбивки трат по источнику.
    usage — словарь resp.get('usage', {}); пустой словарь безопасен
    (даст нулевую стоимость, не упадёт)."""
    if not usage:
        return
    now = datetime.now(timezone.utc)
    peak = is_peak_now(now)
    cost = compute_cost_usd(usage, peak=peak)

    data = _load()
    day_key = now.date().isoformat()
    day = data["daily"].setdefault(day_key, {
        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
        "completion_tokens": 0, "calls": 0, "cost_usd": 0.0, "by_caller": {},
    })
    hit = usage.get("prompt_cache_hit_tokens", 0) or 0
    miss = usage.get("prompt_cache_miss_tokens", 0) or 0
    out = usage.get("completion_tokens", 0) or 0

    day["prompt_cache_hit_tokens"] += hit
    day["prompt_cache_miss_tokens"] += miss
    day["completion_tokens"] += out
    day["calls"] += 1
    day["cost_usd"] += cost

    by_caller = day["by_caller"].setdefault(caller, {"calls": 0, "cost_usd": 0.0})
    by_caller["calls"] += 1
    by_caller["cost_usd"] += cost

    _save(data)


def summary(days=7):
    """Возвращает (total_cost_usd, total_calls, by_caller_totals) за
    последние N дней включительно сегодня. by_caller_totals —
    {caller: cost_usd}, отсортировано по убыванию трат."""
    data = _load()
    cutoff = (datetime.now(timezone.utc).date().toordinal() - days + 1)
    total_cost = 0.0
    total_calls = 0
    by_caller = {}
    for day_key, day in data.get("daily", {}).items():
        try:
            if datetime.fromisoformat(day_key).toordinal() < cutoff:
                continue
        except ValueError:
            continue
        total_cost += day.get("cost_usd", 0.0)
        total_calls += day.get("calls", 0)
        for caller, v in day.get("by_caller", {}).items():
            by_caller[caller] = by_caller.get(caller, 0.0) + v.get("cost_usd", 0.0)
    ranked = sorted(by_caller.items(), key=lambda x: -x[1])
    return total_cost, total_calls, ranked
