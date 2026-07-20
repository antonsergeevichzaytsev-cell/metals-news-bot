#!/usr/bin/env python3
"""Evening digest -> Telegram, 18:30 MSK будни.

Восемь ботов пишут в Telegram разрозненно весь день: filings хук отдельным
сообщением, pipeline reply отдельным, account_watch отдельным. Ничего не
собирает это в одну картину «что вообще случилось сегодня». Mission Control
(07:45) смотрит на живые лиды и говорит, что делать; этот бот смотрит назад
на прошедший день и говорит, что произошло — те же данные, другой срез.

Механика: держит свой снэпшот pipeline.json на начало дня (day_start_snapshot
в собственном state) и на каждом прогоне сравнивает с текущим состоянием -
разница и есть «что изменилось сегодня». Не трогает файлы других ботов,
только читает last_run из их state, чтобы подтвердить, что все живы.
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
PIPELINE_PATH = os.path.join(ROOT, "pipeline.json")
STATE_PATH = os.path.join(ROOT, "state_evening_digest.json")

BOT_STATE_FILES = {
    "filings": "state_filings.json",
    "pipeline_sync": "state_pipeline_sync.json",
    "inbox": "state_inbox.json",
    "account_watch": "state_account_watch.json",
}

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MSK = timezone(timedelta(hours=3))
DEAD_STATUSES = {"dead", "closed", "declined", "done", "channel_failed"}
STALE_HOURS = 30  # бот считается «не отчитался сегодня», если last_run старше этого


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_pipeline():
    return load_json(PIPELINE_PATH, {"leads": []})


def load_state():
    return load_json(STATE_PATH, {})


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def snapshot_leads(pipeline):
    """Минимальный слепок: id -> (status, touches, last_activity).
    Не весь лид целиком — держим state этого бота маленьким."""
    out = {}
    for lead in pipeline.get("leads", []):
        out[lead["id"]] = {
            "status": lead.get("status", ""),
            "touches": lead.get("touches", 0),
            "last_activity": lead.get("last_activity", ""),
        }
    return out


def diff_leads(before, after, pipeline):
    """Сравнивает два слепка, возвращает то, что реально изменилось за день."""
    by_id = {l["id"]: l for l in pipeline.get("leads", [])}
    new_leads, new_replies, newly_dead, touched_again = [], [], [], []

    for lid, cur in after.items():
        prev = before.get(lid)
        lead = by_id.get(lid, {})
        topic = lead.get("topic", "")[:60]
        if prev is None:
            new_leads.append({"id": lid, "topic": topic})
            continue
        if prev["status"] != "reply_received" and cur["status"] == "reply_received":
            new_replies.append({"id": lid, "topic": topic})
        if prev["status"] not in DEAD_STATUSES and cur["status"] in DEAD_STATUSES:
            newly_dead.append({"id": lid, "topic": topic, "status": cur["status"]})
        if cur["touches"] > prev["touches"] and cur["status"] not in DEAD_STATUSES:
            touched_again.append({"id": lid, "topic": topic})

    return {"new_leads": new_leads, "new_replies": new_replies,
            "newly_dead": newly_dead, "touched_again": touched_again}


def bot_liveness(now, pipeline):
    """last_run каждого бота -> жив сегодня или нет. Просто читаем,
    ничего не чиним - это weekly_check работа, здесь только видимость.

    Формат last_run НЕ одинаков между ботами (обнаружено на практике, не
    предположено): filings.py пишет last_run как словарь с диагностикой
    ({"ts": ..., "raw": ...}), inbox.py и account_watch.py - как голую
    ISO-строку. pipeline_sync.py вообще не пишет last_run в свой state -
    для него сигнал живости берём из pipeline.json.last_updated напрямую,
    это тот же файл, который он единственный обновляет."""
    out = {}
    for name, path in BOT_STATE_FILES.items():
        if name == "pipeline_sync":
            raw_ts = pipeline.get("last_updated")
        else:
            st = load_json(os.path.join(ROOT, path), None)
            if st is None:
                out[name] = "нет state-файла"
                continue
            lr = st.get("last_run")
            if isinstance(lr, dict):
                raw_ts = lr.get("ts")
            else:
                raw_ts = lr
        if not raw_ts:
            out[name] = "нет last_run"
            continue
        try:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            h = (now - ts).total_seconds() / 3600
            out[name] = "ok" if h <= STALE_HOURS else f"молчит {h/24:.1f} дн"
        except (ValueError, TypeError):
            out[name] = "битый last_run"
    return out


def account_watch_hits_today(now, hours=13):
    """Хиты account_watch за последние N часов (по умолчанию — с утра,
    примерно с начала рабочего дня до вечернего дайджеста). Без этого
    evening_digest знал только «account_watch жив», не «по какой компании
    было движение» — та же информация терялась между Telegram-сообщением
    и сводкой дня."""
    st = load_json(os.path.join(ROOT, "state_account_watch.json"), None)
    if st is None:
        return []
    cutoff = now - timedelta(hours=hours)
    out = []
    for h in st.get("hits", []):
        ts = h.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt >= cutoff:
            out.append(h)
    return out


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ! telegram error {e.code}: {body}", file=sys.stderr)
        return False


def render(diff, liveness, hits, now_msk):
    date_str = now_msk.strftime("%d %b")
    lines = [f"<b>\U0001f307 Итог дня</b> \u2014 {date_str}", ""]

    total_events = (len(diff["new_leads"]) + len(diff["new_replies"])
                     + len(diff["newly_dead"]) + len(diff["touched_again"])
                     + len(hits))

    if diff["new_replies"]:
        lines.append("\U0001f525 <b>Ответили:</b>")
        for r in diff["new_replies"][:5]:
            lines.append(f"\u2022 {esc(r['topic'])}")
        lines.append("")

    if diff["new_leads"]:
        lines.append(f"\U0001f195 <b>Новых лидов: {len(diff['new_leads'])}</b>")
        for n in diff["new_leads"][:5]:
            lines.append(f"\u2022 {esc(n['topic'])}")
        if len(diff["new_leads"]) > 5:
            lines.append(f"+{len(diff['new_leads']) - 5} ещё")
        lines.append("")

    if diff["touched_again"]:
        lines.append(f"\u270d\ufe0f <b>Касаний отправлено: {len(diff['touched_again'])}</b>")
        for t in diff["touched_again"][:5]:
            lines.append(f"\u2022 {esc(t['topic'])}")
        lines.append("")

    if hits:
        lines.append(f"\U0001f440 <b>Движение по счетам: {len(hits)}</b>")
        for h in hits[:5]:
            lines.append(f"\u2022 <b>{esc(h['company'])}</b> \u2014 {esc(h['title'][:70])}")
        if len(hits) > 5:
            lines.append(f"+{len(hits) - 5} ещё")
        lines.append("")

    if diff["newly_dead"]:
        lines.append(f"\U0001f480 <b>Закрылось: {len(diff['newly_dead'])}</b>")
        for d in diff["newly_dead"][:5]:
            lines.append(f"\u2022 {esc(d['topic'])} \u2192 {esc(d['status'])}")
        lines.append("")

    if total_events == 0:
        lines.append("<i>Движения по pipeline сегодня не было.</i>\n")

    dead_bots = [name for name, status in liveness.items() if status != "ok"]
    if dead_bots:
        lines.append("\u26a0\ufe0f <b>Не отчитались сегодня:</b>")
        for name in dead_bots:
            lines.append(f"\u2022 {esc(name)}: {esc(liveness[name])}")
    else:
        lines.append("\u2705 Все боты отчитались сегодня.")

    return "\n".join(lines)


def main():
    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc.astimezone(MSK)

    pipeline = load_pipeline()
    state = load_state()

    today_str = now_msk.strftime("%Y-%m-%d")
    snap = state.get("day_start_snapshot")
    snap_date = state.get("day_start_date")

    if snap is None:
        # Бот никогда раньше не запускался - сравнивать не с чем вообще.
        # Берём текущее состояние как первую точку отсчёта.
        print("No prior snapshot at all (first run ever) - seeding, no diff this run.")
        state["day_start_snapshot"] = snapshot_leads(pipeline)
        state["day_start_date"] = today_str
        save_state(state)
        return 0

    # snap - это состояние на момент ПОСЛЕДНЕГО сохранённого снэпшота,
    # независимо от того, сегодняшний он или вчерашний. Именно эта разница
    # и есть «что изменилось с прошлого раза» - ровно то, что нужно показать,
    # даже если это первый прогон нового календарного дня.
    diff = diff_leads(snap, snapshot_leads(pipeline), pipeline)
    liveness = bot_liveness(now_utc, pipeline)
    hits = account_watch_hits_today(now_utc)

    text = render(diff, liveness, hits, now_msk)
    tg_send(text)

    # Снэпшот переезжает на СЕЙЧАС после каждой отправки - следующий прогон
    # (сегодня вечером ещё раз, или завтра) сравнивает с этим моментом,
    # а не копит дифф за несколько дней подряд.
    state["day_start_snapshot"] = snapshot_leads(pipeline)
    state["day_start_date"] = today_str
    save_state(state)
    print(f"Sent evening digest: {sum(len(v) for v in diff.values())} pipeline changes, "
          f"{len(hits)} account_watch hit(s), "
          f"{len([b for b in liveness.values() if b != 'ok'])} bot(s) not reporting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
