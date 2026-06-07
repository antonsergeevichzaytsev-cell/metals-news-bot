#!/usr/bin/env python3
"""Weekly Check — Sunday 19:00 MSK.
One ping with Strategy v3 metrics checklist + pivot-triggers.
No two-way flow (GitHub Actions cron-based, no polling) — Anton reviews mentally
and can reply to himself in Saved Messages if he wants persistent log.
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

# Strategy v3 Y1 dates (per decisions_log.md 2026-05-29 activation)
MSK = timezone(timedelta(hours=3))
Y1_START = datetime(2026, 5, 29, tzinfo=MSK)
Y1_END = datetime(2027, 5, 29, tzinfo=MSK)
Y1_TOTAL_WEEKS = 52


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

    # Week number within Y1 (1-based)
    days_since_start = (now.date() - Y1_START.date()).days
    if days_since_start < 0:
        week_in_y1 = 0  # pre-launch
    else:
        week_in_y1 = (days_since_start // 7) + 1

    days_to_end = (Y1_END.date() - now.date()).days

    # Current week range (Mon-Sun)
    weekday = now.weekday()  # 0=Mon, 6=Sun
    monday = now - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)
    week_range = f"{monday.strftime('%d %b')} \u2014 {sunday.strftime('%d %b')}"

    # Six-month checkpoint flag
    months_in = days_since_start // 30 if days_since_start > 0 else 0

    out = f"<b>\U0001f4ca Weekly Check</b> \u2014 \u043d\u0435\u0434\u0435\u043b\u044f {week_in_y1} / {Y1_TOTAL_WEEKS}, Y1\n"
    out += f"<i>{week_range}</i>\n\n"

    out += "<b>\u041c\u0435\u0442\u0440\u0438\u043a\u0438 Strategy v3:</b>\n"
    out += "\u2610 LinkedIn-\u043f\u043e\u0441\u0442\u043e\u0432 \u043e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e?\n"
    out += "\u2610 Expert calls dispatched \u0437\u0430 \u043d\u0435\u0434\u0435\u043b\u044e?\n"
    out += "\u2610 DD-inquiries \u0432 \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u043c \u0434\u0438\u0430\u043b\u043e\u0433\u0435?\n"
    out += "\u2610 \u0427\u0430\u0441\u043e\u0432 \u0440\u0430\u0431\u043e\u0442\u044b (cap = 55)?\n"
    out += "\u2610 Retainer-\u0440\u0430\u0437\u0433\u043e\u0432\u043e\u0440 \u0445\u043e\u0442\u044f \u0431\u044b \u0441 \u043e\u0434\u043d\u0438\u043c \u043a\u043b\u0438\u0435\u043d\u0442\u043e\u043c?\n\n"

    out += "<b>\U0001f6a8 Pivot-\u0442\u0440\u0438\u0433\u0433\u0435\u0440\u044b (Strategy v3):</b>\n"
    out += "\u2022 <b>90 \u0434\u043d\u0435\u0439 \u0431\u0435\u0437 DD inquiry</b> \u2192 \u043f\u0435\u0440\u0435\u0441\u043c\u043e\u0442\u0440 \u043f\u043e\u0437\u0438\u0446\u0438\u043e\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u044f (\u0427\u0430\u0441\u0442\u044c I)\n"
    out += "\u2022 <b>\u0421\u0442\u0430\u0432\u043a\u0430 \u043f\u043b\u0430\u0442\u0444\u043e\u0440\u043c \u043d\u0435 \u0440\u0430\u0441\u0442\u0451\u0442 \u043a \u043c\u0435\u0441.6</b> \u2192 \u0440\u0430\u0437\u0433\u043e\u0432\u043e\u0440 \u0441 researcher\n"
    out += "\u2022 <b>Year 1 \u0431\u0435\u0437 retainer</b> \u2192 Y2 \u043d\u0435 \u0432\u044b\u0441\u0442\u0440\u0435\u043b\u0438\u0442 (\u043a\u0440\u0438\u0442\u0438\u0447\u0435\u0441\u043a\u0430\u044f \u0442\u043e\u0447\u043a\u0430)\n\n"

    # Monthly milestone reminder
    if months_in > 0 and now.day <= 7:
        out += f"<b>\U0001f4c5 \u041c\u0435\u0441\u044f\u0446 {months_in} Y1 \u2014 \u0432\u0440\u0435\u043c\u044f checkpoint:</b>\n"
        if months_in == 1:
            out += "<i>Target: 5-10 expert calls, 1 DD-inquiry in active dialog</i>\n\n"
        elif months_in == 3:
            out += "<i>Target Q1 close: rate UP, 1\u0430\u044f DD-\u0441\u0434\u0435\u043b\u043a\u0430</i>\n\n"
        elif months_in == 6:
            out += "<i>Pivot point: \u0441\u0442\u0430\u0432\u043a\u0430 \u0434\u0432\u0438\u0433\u0430\u0435\u0442\u0441\u044f? \u041d\u0435\u0442 \u2192 \u0440\u0430\u0437\u0433\u043e\u0432\u043e\u0440 \u0441 researcher</i>\n\n"
        elif months_in == 12:
            out += "<b>\u2620\ufe0f Year 1 \u0444\u0438\u043d\u0438\u0448. Retainer \u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043d? \u041d\u0435\u0442 \u2192 pivot.</b>\n\n"

    out += f"<i>\U0001f4c5 \u0414\u043e \u043a\u043e\u043d\u0446\u0430 Y1 (29 \u043c\u0430\u044f 2027): {days_to_end} \u0434\u043d\u0435\u0439</i>"

    tg_send(out)
    print(f"Sent weekly check: week {week_in_y1}/{Y1_TOTAL_WEEKS}, months_in={months_in}, days_to_end={days_to_end}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
