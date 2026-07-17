#!/usr/bin/env python3
"""Weekly Check — Sunday 19:00 MSK.
Сторож (проверка самих ботов) + диспатчи за неделю + метрики v3.1 + reset-точки.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

ROOT = os.path.dirname(os.path.abspath(__file__))
PIPELINE_PATH = os.path.join(ROOT, "pipeline.json")
HISTORY_PATH = os.path.join(ROOT, "history.json")
SYNC_STATE_PATH = os.path.join(ROOT, "state_pipeline_sync.json")
INBOX_STATE_PATH = os.path.join(ROOT, "state_inbox.json")

MSK = timezone(timedelta(hours=3))
Y1_START = datetime(2026, 5, 29, tzinfo=MSK)
Y1_END = datetime(2027, 5, 29, tzinfo=MSK)
Y1_TOTAL_WEEKS = 52

# --- Сторож: пороги ---------------------------------------------------------
# Проверка идёт в воскресенье, а боты бегают Пн-Пт. Значит в норме данные
# уже двое суток как не обновлялись — пороги это учитывают.
STALE_HOURS = 72          # pipeline.json / history.json не трогали дольше -> бот не бежит
FOSSIL_DAYS = 14          # ни одного нового лида дольше -> пайплайн окаменел
CADENCE_MAX_SILENCE = 21  # синхронно с mission_control.is_dead()
STALE_REPLY_DAYS = 7      # ответ лежит дольше -> гниющие деньги
DEAD_STATUSES = {"dead", "closed", "declined", "done", "channel_failed"}

# Strategy v3.1 reset points (decisions_log 2026-06-09)
RESET_POINTS = [
    (datetime(2026, 7, 9, tzinfo=MSK),  "проверка dispatch frequency (не вырос → диагностика payout/profile, НЕ понижать ставку)"),
    (datetime(2026, 8, 9, tzinfo=MSK),  "cash flow не восстановился → fulltime/vahta переходит в primary"),
    (datetime(2026, 9, 9, tzinfo=MSK),  "dispatch стабильно 5+/мес → план возврата к $250 на 2 платформах"),
]


def load_json(path, default):
    if not os.path.exists(path):
        return None  # None = файла нет вообще, это отдельная тревога
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def parse_date(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def dispatch_stats(now, days=7):
    """Главную метрику считает pipeline_sync (у него Gmail), пишет в state.
    Здесь только читаем: у weekly_check в воркфлоу нет доступа к почте вообще."""
    st = load_json(SYNC_STATE_PATH, None)
    if st is None:
        return None
    log = st.get("dispatches")
    if log is None:
        return None  # счётчик ещё не отработал ни разу
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [r for r in log.values() if r.get("date", "") >= cutoff]
    by_kind = {"dispatch": [], "unknown": [], "other": []}
    for r in recent:
        by_kind.setdefault(r.get("kind", "unknown"), []).append(r)
    plats = {}
    for r in by_kind["dispatch"]:
        plats[r.get("platform", "?")] = plats.get(r.get("platform", "?"), 0) + 1
    return {"dispatch": len(by_kind["dispatch"]), "unknown": len(by_kind["unknown"]),
            "other": len(by_kind["other"]), "by_platform": plats}


def watchdog(now):
    """Проверяет БОТОВ, а не бизнес.

    Смысл один: поймать момент, когда бот бодро рапортует, а данные под ним
    окаменели. Именно так пайплайн простоял с 8 июня, а Mission Control
    каждое утро уверенно докладывал по трупам.

    Всё считается из полей самих файлов — состояния сторож не хранит
    (у воркфлоу права contents: read).
    """
    alarms, facts = [], []

    pipeline = load_json(PIPELINE_PATH, None)
    if pipeline is None:
        alarms.append("pipeline.json не читается или отсутствует — pipeline_sync мёртв")
        return alarms, facts

    leads = pipeline.get("leads", [])
    today = now.date()

    # 1. Файл вообще обновляется?
    upd = parse_dt(pipeline.get("last_updated"))
    if upd:
        h = (now - upd.astimezone(MSK)).total_seconds() / 3600
        if h > STALE_HOURS:
            alarms.append(f"pipeline.json не обновлялся {h/24:.1f} дн — pipeline_sync не бежит")
    else:
        alarms.append("в pipeline.json нет last_updated — не могу проверить свежесть")

    # 2. ГЛАВНАЯ ПРОВЕРКА: пайплайн растёт?
    #    Именно её отсутствие стоило шести недель молчаливой лжи.
    firsts = [d for d in (parse_date(l.get("first_contact")) for l in leads) if d]
    if firsts:
        age = (today - max(firsts)).days
        if age > FOSSIL_DAYS:
            alarms.append(
                f"ни одного нового лида {age} дн. Либо ты не пишешь, либо SENT не читается. "
                f"Пайплайн окаменел — всё, что он рапортует, недостоверно"
            )
        facts.append(f"новых лидов за 7 дн: {sum(1 for d in firsts if (today - d).days <= 7)}")
    else:
        alarms.append("в лидах нет ни одной даты first_contact — считать нечем")

    # 3. Живые/мёртвые
    live = [l for l in leads if l.get("status") not in DEAD_STATUSES]
    facts.append(f"живых лидов: {len(live)} из {len(leads)}")
    if leads and not live:
        alarms.append("живых лидов ноль — воронка пуста")

    # 4. Каденция реально работает или лиды бессмертны?
    zombies = [l for l in live
               if l.get("status") in ("sent_no_reply", "follow_up_overdue")
               and l.get("silence_days", 0) > CADENCE_MAX_SILENCE]
    if zombies:
        alarms.append(f"каденция исчерпана у {len(zombies)}, а статус живой — не закрыты в файле")

    # 5. Ответы, которые гниют
    stale = [l for l in live if l.get("status") == "reply_received"
             and l.get("silence_days", 0) >= STALE_REPLY_DAYS]
    if stale:
        worst = max(stale, key=lambda x: x.get("silence_days", 0))
        alarms.append(
            f"ответов лежит без действия: {len(stale)}, худший {worst.get('silence_days')} дн "
            f"— {worst.get('topic', '')[:40]}"
        )

    # 6. Активность за неделю — отвечает на вопрос, который раньше задавали тебе
    acts = [d for d in (parse_date(l.get("last_activity")) for l in leads) if d]
    facts.append(f"лидов с активностью за 7 дн: {sum(1 for d in acts if (today - d).days <= 7)}")
    facts.append(f"касаний всего в работе: {sum(l.get('touches', 0) for l in live)}")

    # 7. Inbox молчит по делу или сдох?
    #    С 17.07 inbox.py не шлёт пустые сводки — раньше его живость была видна
    #    по пяти сообщениям в день. Теперь тишина штатна, и отличить её от смерти
    #    можно только по last_run. Без этой проверки молчание стало бы новой ложью.
    inbox_state = load_json(INBOX_STATE_PATH, None)
    if inbox_state is None:
        alarms.append("state_inbox.json не читается — inbox.py мёртв, платформы не слушает никто")
    else:
        lr = parse_dt(inbox_state.get("last_run"))
        if not lr:
            alarms.append("в state_inbox.json нет last_run — inbox.py ни разу не отработал после правки 17.07")
        else:
            h = (now - lr.astimezone(MSK)).total_seconds() / 3600
            if h > STALE_HOURS:
                alarms.append(f"inbox.py не бежал {h/24:.1f} дн — тишина на платформах может быть его смертью, а не рынком")
        # Счётчик-улика: до 17.07 тут было 2 письма за месяцы, потому что
        # platforms.json не знал glgroup.com. Если снова замрёт — фильтр опять слеп.
        facts.append(f"писем с платформ в памяти inbox: {len(inbox_state.get('seen', []))}")

    # 8. Новостной бот жив?
    history = load_json(HISTORY_PATH, None)
    if history is None:
        alarms.append("history.json не читается — digest мёртв")
    else:
        items = history.get("items", []) if isinstance(history, dict) else history
        ts = [t for t in (parse_dt(i.get("ts")) for i in items) if t]
        if not ts:
            alarms.append("в history.json нет свежих меток времени — digest не пишет")
        else:
            h = (now - max(ts).astimezone(MSK)).total_seconds() / 3600
            if h > STALE_HOURS:
                alarms.append(f"history.json не пополнялся {h/24:.1f} дн — digest не бежит")

    return alarms, facts


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
    now = datetime.now(MSK)

    days_since_start = (now.date() - Y1_START.date()).days
    week_in_y1 = 0 if days_since_start < 0 else (days_since_start // 7) + 1
    days_to_end = (Y1_END.date() - now.date()).days

    weekday = now.weekday()
    monday = now - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)
    week_range = f"{monday.strftime('%d %b')} \u2014 {sunday.strftime('%d %b')}"

    out = f"<b>\U0001f4ca Weekly Check</b> \u2014 неделя {week_in_y1} / {Y1_TOTAL_WEEKS}, Y1\n"
    out += f"<i>{week_range}</i>\n\n"

    # --- Сторож: первым, потому что он решает, верить ли остальному ---
    alarms, facts = watchdog(now)
    out += "<b>\U0001f415 Сторож</b>\n"
    if alarms:
        for a in alarms:
            out += f"\u26a0\ufe0f {esc(a)}\n"
        out += "<i>Пока это не закрыто — цифрам ботов не верь.</i>\n"
    else:
        out += "\u2705 Боты честны: файлы свежие, пайплайн растёт\n"
    if facts:
        out += "<i>" + esc(" \u00b7 ".join(facts)) + "</i>\n"
    out += "\n"

    # --- ГЛАВНАЯ метрика: больше не вопрос, а число ---
    ds = dispatch_stats(now, 7)
    out += "<b>\U0001f3af Диспатчи платформ за неделю</b>\n"
    if ds is None:
        out += "<i>Счётчик ещё не отработал — pipeline_sync не писал dispatches в state</i>\n\n"
    else:
        out += f"<b>{ds['dispatch']}</b>"
        if ds["by_platform"]:
            out += " \u2014 " + esc(", ".join(f"{k}: {v}" for k, v in sorted(ds["by_platform"].items())))
        out += "\n"
        if ds["unknown"]:
            out += f"<i>+{ds['unknown']} писем с платформ не классифицированы \u2014 глянь, если цифра кажется низкой</i>\n"
        if ds["dispatch"] == 0 and ds["other"] == 0 and ds["unknown"] == 0:
            out += "<i>С платформ вообще ничего не пришло. Это либо тишина, либо фильтр.</i>\n"
        out += "\n"

    out += "<b>Метрики недели (v3.1):</b>\n"
    out += "<i>Что видно из данных — выше. Ниже только то, чего в файлах нет:</i>\n"
    out += "\u2610 Часов работы (cap = 50)?\n\n"

    out += "<b>\U0001f6a8 Gating item:</b>\n"
    out += "\u2610 Tipalti payout (РФ) resolved? Без него диспатчи \u2260 деньги\n\n"

    out += "<b>\u23f3 Reset-точки (v3.1):</b>\n"
    for dt, label in RESET_POINTS:
        d = (dt.date() - now.date()).days
        if d < -3:
            continue
        tag = f"через {d} дн." if d > 0 else ("СЕГОДНЯ" if d == 0 else f"{-d} дн. назад — сделал?")
        out += f"\u2022 <b>{dt.strftime('%d.%m')}</b> ({tag}) — {label}\n"
    out += "\n"

    out += "<b>Настоящий разбор:</b> воскресный Weekly Review по живым файлам (decisions_log / client_pipeline / outreach_tracker). Этот пинг — только напоминание.\n\n"

    out += f"<i>\U0001f4c5 До конца Y1 (29 мая 2027): {days_to_end} дней</i>"

    tg_send(out)
    print(f"Sent weekly check: week {week_in_y1}/{Y1_TOTAL_WEEKS}, days_to_end={days_to_end}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
