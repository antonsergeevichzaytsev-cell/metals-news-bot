#!/usr/bin/env python3
"""Mission Control — daily morning briefing aggregator."""
import imaplib, email, json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
import urllib.request, urllib.parse

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

PIPELINE_PATH = "pipeline.json"
ANTON_STATE_PATH = "anton_state.json"
HISTORY_PATH = "history.json"
OVERNIGHT_HOURS = 16
TG_BUDGET = 3900


def load_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def decode_subject(raw):
    if not raw:
        return ""
    parts = decode_header(raw)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                text = text.decode(enc or "utf-8", errors="replace")
            except (LookupError, TypeError):
                text = text.decode("utf-8", errors="replace")
        out += text
    return " ".join(out.split()).strip()


def extract_email_addr(addr):
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", addr or "")
    return m.group(0).lower() if m else ""


def domain_of(addr):
    return addr.split("@", 1)[1].lower() if "@" in addr else ""


def fetch_overnight_emails(hours):
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    M.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    M.select("INBOX", readonly=True)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'(SINCE "{since}")')
    out = []
    if typ == "OK" and data and data[0]:
        ids = data[0].split()
        for num in ids[-200:]:
            typ, msg_data = M.fetch(num, "(BODY.PEEK[HEADER])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            try:
                msg = email.message_from_bytes(msg_data[0][1])
                sender = extract_email_addr(msg.get("From", ""))
                out.append({"sender": sender, "domain": domain_of(sender), "subject": decode_subject(msg.get("Subject", ""))})
            except Exception:
                continue
    try: M.close()
    except Exception: pass
    try: M.logout()
    except Exception: pass
    return out


def compute_strategy_metrics(anton_state):
    sv3 = anton_state.get("strategy_v3", {})
    today = datetime.now(timezone.utc).date()
    try:
        y1_start = datetime.strptime(sv3.get("y1_start_date", "2026-05-29"), "%Y-%m-%d").date()
        y1_end = datetime.strptime(sv3.get("y1_end_date", "2027-05-29"), "%Y-%m-%d").date()
        week_n = (today - y1_start).days // 7 + 1
        days_remaining = (y1_end - today).days
    except ValueError:
        week_n, days_remaining = 0, 365
    return {"week_n": max(1, week_n), "y1_days_remaining": days_remaining, "y1_critical_success": sv3.get("y1_critical_success", "")}


DEAD_STATUSES = {"dead", "closed", "declined", "done", "channel_failed"}
CADENCE_MAX_SILENCE = 21  # каденция 4-7 дн x макс 3 касания -> дольше жить лид не может
CADENCE_EXPIRY_WINDOW = 7  # столько дней напоминаем закрыть лид в файле, потом молчим
# ^ у mission_control права contents:read, состояние "уже сказал" хранить негде.
#   Поэтому окно, а не флаг: иначе лид долбит каждое утро вечно и сам становится зомби.

PIPELINE_STALE_HOURS = 20
# ^ pipeline_sync бежит каждые 30 мин Пн-Пт 7-15 UTC. 20ч перекрывает ночь и
#   утро без ложных срабатываний, но ловит "коммит встал и молчит" за один день —
#   ровно то, что произошло 17-20.07 и не было видно, пока не проверили руками.


def pipeline_staleness_hours(pipeline):
    """None если last_updated нет или битый — тогда staleness не проверяем,
    а не выдаём произвольное число. Отсутствие метки — не то же самое, что 0ч."""
    raw = pipeline.get("last_updated")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def is_dead(l):
    """Мёртв, если помечен мёртвым ИЛИ каденция исчерпана.
    Полученный ответ не умирает никогда — он и есть деньги."""
    if l.get("status") in DEAD_STATUSES:
        return True
    if l.get("status") == "reply_received":
        return False
    if l.get("status") in ("sent_no_reply", "follow_up_overdue"):
        return l.get("silence_days", 0) > CADENCE_MAX_SILENCE
    return False


def analyze_pipeline(pipeline):
    leads = pipeline.get("leads", [])
    live = [l for l in leads if not is_dead(l)]
    # каденция убила, но в файле ещё числится живым. Напоминаем ТОЛЬКО пока свежее:
    # умер давно и не закрыт — значит решение принято молчанием, не долбим.
    just_expired = [l for l in leads
                    if l.get("status") not in DEAD_STATUSES and is_dead(l)
                    and l.get("silence_days", 0) <= CADENCE_MAX_SILENCE + CADENCE_EXPIRY_WINDOW]
    active_calls = [l for l in live if l.get("type") == "expert_call"]
    outreach_active = [l for l in live if l.get("type") == "partnership"]
    # лид с ответом уже поднят как stale_reply — второй раз в overdue не дублируем
    overdue_followup = [l for l in live if l.get("type") == "partnership"
                        and l.get("silence_days", 0) >= 7 and l.get("status") != "reply_received"]
    silent_speakings = [l for l in live if l.get("type") == "speaking_opportunity" and l.get("silence_days", 0) >= 14]
    new_replies = [l for l in live if l.get("status") == "reply_received"]
    # ответ, который лежит неделю — это не "новый ответ", это гниющие деньги
    stale_replies = sorted([l for l in new_replies if l.get("silence_days", 0) >= 7],
                           key=lambda x: -x.get("silence_days", 0))
    return {"total_leads": len(leads), "live_leads": len(live), "dead_leads": len(leads) - len(live),
            "active_calls": active_calls, "outreach_active_count": len(outreach_active),
            "overdue_followup": overdue_followup, "silent_speakings": silent_speakings,
            "new_replies": new_replies, "stale_replies": stale_replies, "just_expired": just_expired}


def top_news_overnight(history, since_hours=24):
    items = history.get("items", []) if isinstance(history, dict) else history
    if not items: return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    fresh = []
    for it in items:
        try:
            ts = datetime.fromisoformat((it.get("ts", "")).replace("Z", "+00:00"))
            if ts >= cutoff: fresh.append(it)
        except (ValueError, TypeError):
            continue
    fresh.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return fresh[:3]


def get_phrase_of_day(anton_state):
    wd = datetime.now(timezone.utc).weekday()
    phrases = anton_state.get("ammunition_phrases", {})
    if wd == 0: return ("anchor", phrases.get("anchor", ""))
    elif wd == 4: return ("pitch_60s", phrases.get("pitch_60s", ""))
    else: return ("rate", phrases.get("rate", ""))


def esc(s):
    if not isinstance(s, str): s = str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- цены: перенесено из daily_brief.py, чтобы утреннее сообщение было одно -----

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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


# Диапазоны за последние годы с запасом на волатильность, не биржевой лимит.
# Смысл не «поймать реальный ценовой шок», а «отличить реальную цену от мусора»:
# смена единиц (за фунт вместо за тонну), битый парсинг, зависший кэш источника —
# всё это выглядит как нормальный float, просто не в этом диапазоне.
PRICE_SANITY = {"Al": (1500, 5000), "Cu": (5000, 15000)}


def is_plausible_price(sym, price):
    lo, hi = PRICE_SANITY.get(sym, (0, float("inf")))
    return lo <= price <= hi


def fetch_prices():
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


def deepseek_synthesize(context, max_tokens=900):
    if not DEEPSEEK_KEY: return None
    url = "https://api.deepseek.com/chat/completions"
    prompt = (
        "Ты — Chief of Staff Антона Зайцева. Утренний briefing на русском.\n\n"
        "Правила:\n- Никакой воды, никаких 'отличного утра' и преамбул\n- Только факты из переданного контекста, ничего не выдумывай\n- HTML теги Telegram: <b>, <i>, <code>, без других\n- Эмодзи только в заголовках секций\n- Максимум 2500 символов всего\n"
        "- Мёртвые лиды в контекст не попадают. Не додумывай их и не проси по ним follow-up.\n"
        "- Если pipeline_stale=true — ЭТО ПЕРВЫЙ пункт URGENT, перед всем остальным: "
        "'⚠️ Pipeline не обновлялся {pipeline_stale_hours}ч — цифры ниже могут быть устаревшими. Проверь Actions.' "
        "PIPELINE и остальные секции всё равно показывай как есть, но с этим предупреждением наверху.\n"
        "- Полученный ответ, который лежит >7 дней (stale_replies) — ВСЕГДА первый пункт URGENT (после pipeline_stale, если он есть). "
        "Это не 'новый ответ', это деньги, которые гниют. Пиши, сколько дней лежит.\n"
        "- Каждый пункт URGENT — бинарное действие с глаголом. Не 'проработать', а 'ответить/отправить/закрыть'.\n\n"
        "Секции (в этом порядке):\n"
        "🎯 <b>MISSION CONTROL — {weekday} {date}</b>\n\n"
        "⚡ <b>URGENT</b>\n[2-4 пункта что требует действия СЕГОДНЯ. Сначала stale_replies с числом дней. Если нет — 'ничего критичного']\n\n"
        "💼 <b>PIPELINE</b>\n[1-3 строки: только живые. Формат 'живых N (мёртвых M отброшено)'. Если just_expired непусто — одна строка: что каденция убила и надо закрыть в файле]\n\n"
        "📊 <b>STRATEGY v3</b>\n[Week N/52, days remaining, ключевая метрика]\n\n"
        "📈 <b>MARKETS</b>\n[строка prices_line дословно. Если пусто — секцию пропустить]\n\n"
        "🗞️ <b>TOP NEWS</b>\n[1-2 ключевых события overnight с угла Anton'а]\n\n"
        "🗣️ <b>ФРАЗА ДНЯ</b>\n[фраза дословно из контекста]\n\n"
        "⚠️ <b>HARD TRUTH</b>\n[ОДНА честная строка если есть проблема. Иначе пропустить.]\n\n"
        f"=== КОНТЕКСТ ===\n{json.dumps(context, ensure_ascii=False)}"
    )
    body = json.dumps({"model": "deepseek-chat", "messages": [{"role": "system", "content": "Ты sharp Chief of Staff. Direct, no fluff."}, {"role": "user", "content": prompt}], "max_tokens": max_tokens, "temperature": 0.3}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
            return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"DeepSeek error: {e}", file=sys.stderr)
        return None


def format_fallback(context):
    p = context["pipeline_analysis"]
    s = context["strategy_metrics"]
    lines = [f"🎯 <b>MISSION CONTROL — {context['weekday']} {context['date_str']}</b>", ""]
    lines.append("⚡ <b>URGENT</b>")
    n = 0
    if context.get("pipeline_stale"):
        lines.append(f"• ⚠️ Pipeline не обновлялся {context.get('pipeline_stale_hours')}ч — цифры ниже могут быть устаревшими. Проверь Actions."); n += 1
    for l in p["stale_replies"][:3]:
        lines.append(f"• ОТВЕТ ЛЕЖИТ {l.get('silence_days', 0)} ДН — прочитать и ответить: {esc(l.get('topic', ''))}"); n += 1
    for l in p["new_replies"][:3]:
        if l in p["stale_replies"]: continue
        lines.append(f"• REPLY: {esc(l.get('topic', ''))}"); n += 1
    for l in p["overdue_followup"][:3]:
        lines.append(f"• Follow-up overdue ({l.get('silence_days', 0)}d): {esc(l.get('topic', ''))}"); n += 1
    if n == 0: lines.append("• Ничего критичного")
    lines.append("")
    lines.append("💼 <b>PIPELINE</b>")
    lines.append(f"• Живых: {p['live_leads']} (мёртвых отброшено: {p['dead_leads']})")
    lines.append(f"• Active expert calls: {len(p['active_calls'])}")
    lines.append(f"• Outreach in flight: {p['outreach_active_count']}")
    if p["overdue_followup"]:
        lines.append(f"• Overdue follow-ups: {len(p['overdue_followup'])}")
    for l in p["just_expired"][:3]:
        lines.append(f"• Каденция исчерпана ({l.get('silence_days', 0)}d) — закрыть в файле: {esc(l.get('topic', ''))}")
    lines.append("")
    if context.get("prices_line"):
        lines.append("📈 <b>MARKETS</b>")
        lines.append(esc(context["prices_line"]))
        lines.append("")
    lines.append("📊 <b>STRATEGY v3</b>")
    lines.append(f"• Week {s['week_n']}/52 Y1, {s['y1_days_remaining']} дней до close")
    lines.append(f"• Critical: {esc(s['y1_critical_success'])}")
    lines.append("")
    if context["top_news"]:
        lines.append("🗞️ <b>TOP NEWS</b>")
        for n_item in context["top_news"][:2]:
            lines.append(f"• {esc(n_item.get('title', '')[:80])}")
            if n_item.get("why"): lines.append(f"  <i>{esc(n_item['why'][:80])}</i>")
        lines.append("")
    pk, pt = context["phrase"]
    if pt:
        lines.append("🗣️ <b>ФРАЗА ДНЯ</b>")
        lines.append(f"<i>{esc(pt)}</i>")
    return "\n".join(lines)


def tg_send(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text[:TG_BUDGET], "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print(f"TG error: {e}", file=sys.stderr)
        return False


def main():
    now = datetime.now(timezone.utc) + timedelta(hours=3)
    weekdays_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    months_ru = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
    weekday = weekdays_ru[now.weekday()]
    date_str = f"{now.day} {months_ru[now.month-1]}"
    pipeline = load_json(PIPELINE_PATH, {"leads": []})
    anton_state = load_json(ANTON_STATE_PATH, {})
    history = load_json(HISTORY_PATH, {"items": []})
    pa = analyze_pipeline(pipeline)
    sm = compute_strategy_metrics(anton_state)
    news = top_news_overnight(history)
    phrase = get_phrase_of_day(anton_state)
    stale_h = pipeline_staleness_hours(pipeline)
    pipeline_stale = stale_h is not None and stale_h > PIPELINE_STALE_HOURS
    try: overnight = fetch_overnight_emails(OVERNIGHT_HOURS)
    except Exception as e:
        print(f"Gmail error: {e}", file=sys.stderr); overnight = []
    try: prices_line = format_prices(fetch_prices())
    except Exception as e:
        print(f"Prices error: {e}", file=sys.stderr); prices_line = ""
    context = {
        "date_str": date_str, "weekday": weekday,
        "strategy_metrics": sm,
        "pipeline_analysis": {
            "live_leads": pa["live_leads"],
            "dead_leads_excluded": pa["dead_leads"],
            "active_calls_count": len(pa["active_calls"]),
            "outreach_active_count": pa["outreach_active_count"],
            "stale_replies": [{"topic": l.get("topic", "")[:80], "silence_days": l.get("silence_days", 0), "next_action": l.get("next_action", "")[:100]} for l in pa["stale_replies"][:3]],
            "overdue_followup": [{"topic": l.get("topic", "")[:80], "silence_days": l.get("silence_days", 0)} for l in pa["overdue_followup"][:5]],
            "silent_speakings": [{"topic": l.get("topic", "")[:80], "silence_days": l.get("silence_days", 0)} for l in pa["silent_speakings"][:3]],
            "new_replies": [{"topic": l.get("topic", "")[:80], "status": l.get("status", "")} for l in pa["new_replies"][:5]],
            "active_calls_status": [{"topic": l.get("topic", "")[:60], "silence_days": l.get("silence_days", 0)} for l in pa["active_calls"][:3]],
            "just_expired": [{"topic": l.get("topic", "")[:80], "silence_days": l.get("silence_days", 0)} for l in pa["just_expired"][:3]],
        },
        "prices_line": prices_line,
        "overnight_emails_count": len(overnight),
        "top_news": [{"title": n.get("title", "")[:120], "why": n.get("why", "")[:120]} for n in news],
        "phrase": phrase, "phrase_text": phrase[1],
        "signature_cases": anton_state.get("signature_cases", [])[:3],
        "icp_tier_1": anton_state.get("icp", {}).get("tier_1", ""),
        "constraints_note": "Russia-resident. Compliance filter at platforms. B2 English.",
        "pipeline_stale": pipeline_stale,
        "pipeline_stale_hours": round(stale_h) if stale_h is not None else None,
    }
    text = deepseek_synthesize(context)
    if not text:
        print("DeepSeek failed, using fallback", file=sys.stderr)
        context["pipeline_analysis"] = pa
        context["top_news"] = news
        text = format_fallback(context)
    tg_send(text)
    print(f"Mission Control sent. Week {sm['week_n']}, overnight emails {len(overnight)}, news {len(news)}")


if __name__ == "__main__":
    main()
