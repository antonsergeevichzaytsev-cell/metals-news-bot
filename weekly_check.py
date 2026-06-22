#!/usr/bin/env python3
"""Weekly Check — Sunday 19:00 MSK.
One ping with Strategy v3.1 metrics + reset-point countdown.
Static reminder only (cron-based, no data reads). Real review = Sunday Weekly Review
against live files (decisions_log, client_pipeline, outreach_tracker).
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MSK = timezone(timedelta(hours=3))
Y1_START = datetime(2026, 5, 29, tzinfo=MSK)
Y1_END = datetime(2027, 5, 29, tzinfo=MSK)
Y1_TOTAL_WEEKS = 52

# Strategy v3.1 reset points (decisions_log 2026-06-09)
RESET_POINTS = [
    (datetime(2026, 7, 9, tzinfo=MSK),  "проверка dispatch frequency (не вырос → диагностика payout/profile, НЕ понижать ставку)"),
    (datetime(2026, 8, 9, tzinfo=MSK),  "cash flow не восстановился → fulltime/vahta переходит в primary"),
    (datetime(2026, 9, 9, tzinfo=MSK),  "dispatch стабильно 5+/мес → план возврата к $250 на 2 платформах"),
]


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

    out += "<b>Метрики недели (v3.1):</b>\n"
    out += "\u2610 Диспатчей платформ за неделю? (главная)\n"
    out += "\u2610 Outreach отправлено (cold + follow-up + регистрации)?\n"
    out += "\u2610 Партнёрские firms — ответы / follow-up сделан?\n"
    out += "\u2610 Прямой DD-диалог хотя бы с одним?\n"
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
