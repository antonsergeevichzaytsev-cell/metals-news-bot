#!/usr/bin/env python3
"""Telegram command handler for metals-news-bot.

v1, 09.08.2026. Первая интерактивность в этом боте — раньше он только
вещал по расписанию, никогда не слушал. Паттерн взят из fitness-bot:
Cloudflare Worker принимает Telegram webhook, шлёт repository_dispatch
с телом апдейта в client_payload, этот скрипт читает его из
TELEGRAM_UPDATE_JSON — БЕЗ повторного getUpdates (см. fitness-bot
DEPLOY.md/worker.js: getUpdates и активный webhook взаимоисключающи).

Команды:
  /digest   — внеплановый прогон digest.py прямо сейчас
  /company  <имя> — история упоминаний компании из history.json, 7 дней
  /why      <номер> — deep-analysis по номеру из последнего дайджеста
  /status   — health: last_run, broken feeds, uzcopper-хиты за сутки
  /help     — список команд

Не сделано намеренно: произвольный чат/вопрос модели. Это бы превратило
дайджест-бота в chat-интерфейс с открытым концом — другая функция,
другая стоимость, другой контроль. Команды — фиксированный, предсказуемый
набор, как и остальная система.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import net

ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(ROOT, "history.json")
STATE_FILE = os.path.join(ROOT, "state.json")
LAST_DIGEST_FILE = os.path.join(ROOT, "state_last_digest_sent.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Кто разрешён писать команды. Апдейт с другим chat_id молча игнорируется —
# бот приватный, отвечать чужим людям не должен, но и не должен палить
# ошибкой, что чат существует.
AUTHORIZED_CHAT_ID = CHAT_ID


def load_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


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
        with net.urlopen_retry(req, timeout=20) as r:
            r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ! telegram error {e.code}: {body}", file=sys.stderr)
        raise


# --- Command implementations -------------------------------------------------

def cmd_help():
    tg_send(
        "<b>Команды</b>\n\n"
        "/digest — внеплановый прогон дайджеста сейчас\n"
        "/company &lt;имя&gt; — история упоминаний за 7 дней\n"
        "/why &lt;номер&gt; — разбор заметки из последнего дайджеста\n"
        "/status — health бота: последний прогон, битые ленты\n"
        "/help — это сообщение"
    )


def cmd_digest():
    """Синхронный вызов digest.py в этом же процессе воркфлоу.

    Не отдельный workflow_dispatch — тот означал бы ждать нового прогона
    Actions (задержка, вторая очередь concurrency group). Прямой запуск
    внутри уже идущего job'а быстрее и не сталкивается с 'repo-writes'
    concurrency group, которую и digest.yml, и fitness_bot.yml используют.
    """
    tg_send("⏳ Запускаю внеплановый дайджест…")
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "digest.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=420,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        tg_send("⚠️ Дайджест не уложился в таймаут (7 мин). Попробуй позже.")
        return
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-500:]
        tg_send(f"⚠️ Дайджест упал с ошибкой:\n<code>{esc(tail)}</code>")
        return
    # digest.py сам шлёт сообщения (или молчит, если enriched пуст) — здесь
    # только подтверждение, что прогон реально состоялся, не дублируем вывод.
    print(result.stdout[-2000:])


def cmd_company(arg):
    arg = (arg or "").strip()
    if not arg:
        tg_send("Формат: <code>/company Almalyk MMC</code>")
        return
    history = load_json(HISTORY_FILE, {"items": []})
    items = history.get("items", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    needle = arg.lower()
    matches = [
        it for it in items
        if it.get("ts", "") >= cutoff and needle in (it.get("company") or "").lower()
    ]
    if not matches:
        tg_send(f"🔍 За 7 дней ничего по «{esc(arg)}» не найдено.")
        return
    matches.sort(key=lambda x: x.get("ts", ""), reverse=True)
    lines = [f"<b>🔍 {esc(arg)}</b> — {len(matches)} за 7 дней\n"]
    for it in matches[:10]:
        title = esc(it.get("title", ""))
        link = esc(it.get("link", ""))
        why = esc(it.get("why", ""))
        dot = {"high": "🔴", "medium": "🟡"}.get(it.get("priority"), "⚪")
        lines.append(f'{dot} <a href="{link}">{title}</a>')
        if why:
            lines.append(f"   💡 {why}")
    if len(matches) > 10:
        lines.append(f"\n+{len(matches) - 10} ещё, не показаны")
    tg_send("\n".join(lines))


def cmd_why(arg):
    arg = (arg or "").strip()
    if not arg.isdigit():
        tg_send("Формат: <code>/why 3</code> — номер из последнего дайджеста")
        return
    idx = int(arg)
    last_sent = load_json(LAST_DIGEST_FILE, {"items": []})
    items = last_sent.get("items", [])
    if not items:
        tg_send("Нет данных о последнем дайджесте — он либо не запускался, либо был пуст.")
        return
    if idx < 1 or idx > len(items):
        tg_send(f"Номер вне диапазона — в последнем дайджесте было {len(items)} заметок.")
        return
    it = items[idx - 1]
    title = esc(it.get("title", ""))
    link = esc(it.get("link", ""))
    company = esc(it.get("company", ""))
    why = esc(it.get("why", ""))
    deep = it.get("deep")
    lines = [f'<b>{idx}.</b> <a href="{link}">{title}</a>']
    if company:
        lines.append(f"<i>{company}</i>")
    if why:
        lines.append(f"\n💡 {why}")
    if deep:
        what = esc((deep.get("what") or "").strip())
        who = esc((deep.get("who") or "").strip())
        action = esc((deep.get("action") or "").strip())
        if what:
            lines.append(f"• <b>что:</b> {what}")
        if who:
            lines.append(f"• <b>кого:</b> {who}")
        if action:
            lines.append(f"• <b>делать:</b> {action}")
    else:
        lines.append("\n<i>(углублённого разбора нет — заметка не была priority=high)</i>")
    tg_send("\n".join(lines))


def cmd_status():
    state = load_json(STATE_FILE, {})
    last_run = state.get("last_run", {})
    if not last_run:
        tg_send("⚠️ Нет данных о прогонах — state.json пуст или не найден.")
        return
    ts = last_run.get("ts", "?")
    broken = last_run.get("broken", {})
    lines = [
        "<b>📊 Status</b>",
        f"Последний прогон: {esc(ts)}",
        f"Сырых заметок: {last_run.get('raw', '?')} → кандидатов: {last_run.get('candidates', '?')} → отправлено: {last_run.get('enriched', '?')}",
        f"Битых лент: {last_run.get('feeds_broken', 0)} из {last_run.get('feeds_total', 0)}",
    ]
    if broken:
        lines.append("\n<b>Битые ленты:</b>")
        for name, reason in list(broken.items())[:8]:
            lines.append(f"  • {esc(name)}: {esc(str(reason))}")
    tg_send("\n".join(lines))


# --- Dispatch ------------------------------------------------------------

COMMANDS = {
    "/digest": lambda arg: cmd_digest(),
    "/company": cmd_company,
    "/why": cmd_why,
    "/status": lambda arg: cmd_status(),
    "/help": lambda arg: cmd_help(),
    "/start": lambda arg: cmd_help(),
}


def handle_update(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        print("No message in update, ignoring (likely callback_query or other type).")
        return
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(AUTHORIZED_CHAT_ID):
        print(f"Ignoring message from unauthorized chat_id={chat_id}")
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        print("Not a command, ignoring.")
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0]  # strip @botname suffix (group chats)
    arg = parts[1] if len(parts) > 1 else ""
    handler = COMMANDS.get(cmd)
    if handler is None:
        tg_send(f"Неизвестная команда {esc(cmd)}. /help — список команд.")
        return
    print(f"Dispatching {cmd} arg={arg!r}")
    handler(arg)


def main():
    raw = os.environ.get("TELEGRAM_UPDATE_JSON", "")
    if not raw or raw == "null":
        print("No TELEGRAM_UPDATE_JSON in environment — nothing to do.")
        return 0
    try:
        update = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Failed to parse TELEGRAM_UPDATE_JSON: {e}", file=sys.stderr)
        return 1
    handle_update(update)
    return 0


if __name__ == "__main__":
    sys.exit(main())
