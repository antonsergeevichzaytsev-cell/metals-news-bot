#!/usr/bin/env python3
"""Telegram command handler for metals-news-bot.

v1, 09.08.2026. Первая интерактивность в этом боте — раньше он только
вещал по расписанию, никогда не слушал. Паттерн взят из fitness-bot:
Cloudflare Worker принимает Telegram webhook, шлёт repository_dispatch
с телом апдейта в client_payload, этот скрипт читает его из
TELEGRAM_UPDATE_JSON — БЕЗ повторного getUpdates (см. fitness-bot
DEPLOY.md/worker.js: getUpdates и активный webhook взаимоисключающи).

v2, 16.08.2026. Четыре новые команды поверх исходных пяти:
/orbit, /search, /feeds, /deep. /deep переиспользует
digest.deepseek_deep_analysis напрямую (импорт digest внутри функции,
не на уровне модуля — чтобы не тянуть DEEPSEEK_API_KEY проверку при
любом другом вызове bot_commands, если он вдруг понадобится без ключа).

v3, 16.08.2026. cmd_deep теперь подтягивает контекст тренда через
digest.find_similar_history перед вызовом deep-анализа — тот же путь,
что digest.py использует в основном прогоне для priority=high (см. v11
там). Ручной /deep больше не беднее автоматического разбора.

v4, 16.08.2026. /weekly — недельная сводка (high-priority + UZCOPPER-
орбита) на почту через новый net.smtp_send_retry. GMAIL_USER/
GMAIL_APP_PASSWORD уже были в секретах (использовались только для
чтения через imap_connect_retry в inbox.py/mission_control.py) —
переиспользованы для отправки, не заводили новый секрет.

v5, 16.08.2026. Пять новых команд: /prices (COMEX медь/алюминий через
Yahoo Finance chart API — LME напрямую платный, COMEX коррелирует
достаточно для контекста), /synthesis (связывает high-priority заметки
по компаниям, отдельный промпт от /deep — вопрос "что видно вместе",
не "что в одной заметке"), /watch /unwatch /watchlist (подписки на
ключевые слова в state_watchlist.json — сама сверка при публикации
заметки живёт в digest.py, эти три команды только читают/пишут список).

Команды:
  /digest    — внеплановый прогон digest.py прямо сейчас
  /company   <имя> — история упоминаний компании из history.json, 7 дней
  /search    <текст> — свободный поиск по всему тексту заметки, 14 дней
  /orbit     [дни] — UZCOPPER-орбита за N дней (по умолчанию 7)
  /why       <номер> — deep-analysis по номеру из последнего дайджеста
  /deep      <номер> — запросить deep-analysis для заметки без него
  /synthesis [дни] — связать high-priority заметки по компаниям
  /prices    — живые цены медь/алюминий (COMEX)
  /watch     <слово> — подписаться на ключевое слово
  /unwatch   <слово> — отписаться
  /watchlist — список текущих подписок
  /feeds     — здоровье источников последнего прогона
  /weekly    — недельная сводка на почту
  /status    — health: last_run, broken feeds, uzcopper-хиты за сутки, секреты
  /help      — список команд

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
import prices as pr
import weekly_check as wc

ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(ROOT, "history.json")
STATE_FILE = os.path.join(ROOT, "state.json")
LAST_DIGEST_FILE = os.path.join(ROOT, "state_last_digest_sent.json")
WATCHLIST_FILE = os.path.join(ROOT, "state_watchlist.json")

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


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


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
        "/search &lt;текст&gt; — поиск по всему тексту заметок, 14 дней\n"
        "/orbit [дни] — UZCOPPER-орбита, по умолчанию 7 дней\n"
        "/why &lt;номер&gt; — разбор заметки из последнего дайджеста\n"
        "/deep &lt;номер&gt; — запросить разбор для заметки без него\n"
        "/synthesis [дни] — связать high-priority заметки по компаниям\n"
        "/prices — живые цены медь/алюминий (COMEX)\n"
        "/watch &lt;слово&gt; — подписаться, отдельный алерт при совпадении\n"
        "/unwatch &lt;слово&gt; — отписаться\n"
        "/watchlist — список текущих подписок\n"
        "/feeds — какие источники сейчас рабочие/битые\n"
        "/weekly — недельная сводка на почту (high-priority + orbit)\n"
        "/status — health бота: последний прогон, битые ленты, секреты\n"
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
        trend = esc((deep.get("trend") or "").strip())
        if what:
            lines.append(f"• <b>что:</b> {what}")
        if who:
            lines.append(f"• <b>кого:</b> {who}")
        if action:
            lines.append(f"• <b>делать:</b> {action}")
        if trend:
            lines.append(f"• <b>тренд:</b> {trend}")
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
    # 19.08.2026: та же проверка, что раз в неделю делает weekly_check,
    # но по требованию, а не только по воскресеньям — секрет может
    # протухнуть в любой момент между отчётами (см. GMAIL_APP_PASSWORD,
    # реально протух на 61-й день молча, обнаружили только по failure
    # статистике Actions, не по еженедельному отчёту).
    try:
        overdue = wc.secrets_rotation_check(datetime.now(timezone.utc))
        if overdue:
            lines.append("\n<b>⚠️ Секреты требуют ротации:</b>")
            for name, age in overdue:
                lines.append(f"  • {esc(name)}: {age} дн. с последней смены")
    except Exception as e:
        print(f"secrets_rotation_check error in /status: {e}", file=sys.stderr)
    tg_send("\n".join(lines))


def cmd_prices():
    """Живые цены на медь и алюминий через COMEX-фьючерсы (Yahoo Finance,
    Stooq как fallback). Логика в prices.py — единый источник правды,
    тот же, что использует mission_control.py и linkedin_ideas.py.

    19.08.2026: раньше здесь была третья независимая копия сетевого
    запроса к Yahoo (без Stooq-fallback, без sanity-проверки, в других
    единицах — центы/фунт вместо $/тонна). Устранено — теперь везде
    одинаковые единицы и одна логика получения цены.
    """
    prices = pr.fetch_prices()
    labels = {"Cu": "Медь", "Al": "Алюминий"}
    lines = ["<b>💰 Цены</b> (COMEX, не LME напрямую)\n"]
    for sym, label in labels.items():
        if sym in prices:
            price, chg, src = prices[sym]
            if chg is None:
                lines.append(f"{label}: <b>${price:,.0f}/т</b> ({src})")
            else:
                arrow = "\U0001f53a" if chg > 0 else ("\U0001f53b" if chg < 0 else "\u25aa\ufe0f")
                lines.append(f"{label}: <b>${price:,.0f}/т</b> {arrow} {chg:+.2f}% ({src})")
        else:
            lines.append(f"{label}: недоступно (оба источника не ответили)")
    if not prices:
        lines.append("\n\u26a0\ufe0f Ни один тикер не ответил — источник(и) мог измениться.")
    tg_send("\n".join(lines))


def cmd_orbit(arg):
    """Новости в UZCOPPER/CIS-орбите за N дней (по умолчанию 7).

    Переиспользует is_uzcopper_relevant из digest.py вместо повторной
    реализации regex — единая точка правды для того, что считается
    orbit-релевантным, не две расходящиеся копии списка слов.
    """
    days = 7
    arg = (arg or "").strip()
    if arg.isdigit():
        days = max(1, min(int(arg), 30))
    history = load_json(HISTORY_FILE, {"items": []})
    items = history.get("items", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    matches = [it for it in items if it.get("ts", "") >= cutoff and it.get("uzcopper")]
    if not matches:
        tg_send(f"🏭 За {days} дн. в UZCOPPER-орбите ничего не найдено.")
        return
    matches.sort(key=lambda x: x.get("ts", ""), reverse=True)
    lines = [f"<b>🏭 UZCOPPER-орбита</b> — {len(matches)} за {days} дн.\n"]
    for it in matches[:15]:
        title = esc(it.get("title", ""))
        link = esc(it.get("link", ""))
        dot = {"high": "🔴", "medium": "🟡"}.get(it.get("priority"), "⚪")
        lines.append(f'{dot} <a href="{link}">{title}</a>')
    if len(matches) > 15:
        lines.append(f"\n+{len(matches) - 15} ещё, не показаны")
    tg_send("\n".join(lines))


def cmd_search(arg):
    """Свободный поиск по title+desc+why в history.json, 14 дней.

    Отличие от /company: та ищет только по полю company (точный
    справочник), эта — по всему тексту заметки, для случаев когда
    интересующее слово не название компании (например 'смелтер' или
    'CBAM' или конкретный регион).
    """
    arg = (arg or "").strip()
    if not arg:
        tg_send("Формат: <code>/search smelter restart</code>")
        return
    history = load_json(HISTORY_FILE, {"items": []})
    items = history.get("items", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    needle = arg.lower()
    matches = []
    for it in items:
        if it.get("ts", "") < cutoff:
            continue
        haystack = " ".join([
            it.get("title", ""), it.get("desc", ""),
            it.get("why", ""), it.get("company", ""),
        ]).lower()
        if needle in haystack:
            matches.append(it)
    if not matches:
        tg_send(f"🔍 За 14 дней ничего по «{esc(arg)}» не найдено.")
        return
    matches.sort(key=lambda x: x.get("ts", ""), reverse=True)
    lines = [f"<b>🔍 «{esc(arg)}»</b> — {len(matches)} за 14 дней\n"]
    for it in matches[:10]:
        title = esc(it.get("title", ""))
        link = esc(it.get("link", ""))
        dot = {"high": "🔴", "medium": "🟡"}.get(it.get("priority"), "⚪")
        lines.append(f'{dot} <a href="{link}">{title}</a>')
    if len(matches) > 10:
        lines.append(f"\n+{len(matches) - 10} ещё, не показаны")
    tg_send("\n".join(lines))


def cmd_feeds():
    """Здоровье источников из последнего прогона — не только битые,
    но и общая картина (сколько всего, какие конкретно отвалились).
    Отдельно от /status: там сводка всего прогона, здесь фокус только
    на источниках, для случая когда интересно именно это.
    """
    state = load_json(STATE_FILE, {})
    last_run = state.get("last_run", {})
    if not last_run:
        tg_send("⚠️ Нет данных — state.json пуст.")
        return
    total = last_run.get("feeds_total", 0)
    broken = last_run.get("broken", {})
    ok = total - len(broken)
    lines = [f"<b>📡 Источники</b>: {ok}/{total} рабочих"]
    if broken:
        lines.append("\n<b>Не отвечают:</b>")
        for name, reason in broken.items():
            lines.append(f"  • {esc(name)}: {esc(str(reason))}")
    else:
        lines.append("Все источники рабочие.")
    tg_send("\n".join(lines))


def cmd_deep(arg):
    """Принудительный deep-analysis для ЛЮБОЙ заметки из последнего
    дайджеста, не только priority=high (в отличие от /why, который
    только показывает то, что уже посчитано в основном прогоне).
    Живой вызов DeepSeek — не бесплатно и не мгновенно, поэтому
    отдельная команда, не автоматическое поведение /why.
    """
    arg = (arg or "").strip()
    if not arg.isdigit():
        tg_send("Формат: <code>/deep 5</code> — номер из последнего дайджеста")
        return
    idx = int(arg)
    last_sent = load_json(LAST_DIGEST_FILE, {"items": []})
    items = last_sent.get("items", [])
    if not items:
        tg_send("Нет данных о последнем дайджесте.")
        return
    if idx < 1 or idx > len(items):
        tg_send(f"Номер вне диапазона — заметок было {len(items)}.")
        return
    it = items[idx - 1]
    if it.get("deep"):
        tg_send(f"У заметки {idx} уже есть разбор — смотри <code>/why {idx}</code>.")
        return
    tg_send("⏳ Запрашиваю разбор…")
    try:
        import digest as dg
    except Exception as e:
        tg_send(f"⚠️ Не смог загрузить модуль анализа: {esc(str(e))}")
        return
    history = load_json(HISTORY_FILE, {"items": []})
    prior = dg.find_similar_history(history, it.get("company", ""), it.get("title", ""))
    deep = dg.deepseek_deep_analysis(
        it.get("title", ""), it.get("desc", ""), "",
        it.get("why", ""), it.get("company", ""),
        prior_items=prior,
    )
    if not deep:
        tg_send("⚠️ DeepSeek не ответил — попробуй позже.")
        return
    title = esc(it.get("title", ""))
    link = esc(it.get("link", ""))
    lines = [f'<b>{idx}.</b> <a href="{link}">{title}</a>\n']
    what = esc((deep.get("what") or "").strip())
    who = esc((deep.get("who") or "").strip())
    action = esc((deep.get("action") or "").strip())
    trend = esc((deep.get("trend") or "").strip())
    if what:
        lines.append(f"• <b>что:</b> {what}")
    if who:
        lines.append(f"• <b>кого:</b> {who}")
    if action:
        lines.append(f"• <b>делать:</b> {action}")
    if trend:
        lines.append(f"• <b>тренд:</b> {trend}")
    tg_send("\n".join(lines))


SYNTHESIS_SYS_PROMPT = (
    "You are an analyst supporting a senior independent consultant in non-ferrous metals and "
    "mining. You are given several HIGH-priority news items about the SAME company from the "
    "past days, each already individually flagged as important. Your job is to see the "
    "combined picture that no single item shows on its own — do they point the same direction, "
    "contradict each other, or show an escalating pattern? In Russian, 3 bullets max, each "
    "under 20 words: "
    "(1) картина целиком — what the combined items say together that one item alone doesn't; "
    "(2) противоречия — note any items that conflict or complicate each other, empty string if "
    "none, don't invent a conflict that isn't there; "
    "(3) вопрос — one sharp question this combination raises worth investigating, or empty "
    "string if genuinely nothing beyond the individual items' own actions. "
    "Reply ONLY with valid JSON: {\"picture\": str, \"conflicts\": str, \"question\": str}."
)


def synthesize_cluster(company, items):
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": SYNTHESIS_SYS_PROMPT},
            {"role": "user", "content": (
                f"COMPANY: {company}\n\nITEMS:\n" + "\n".join(
                    f"- [{(it.get('ts') or '')[:10]}] {it.get('title', '')}: {it.get('why', '')}"
                    for it in items
                )
            )},
        ],
        "temperature": 0.2,
        "max_tokens": 260,
        "response_format": {"type": "json_object"},
        # 20.08.2026: deepseek-v4-flash по умолчанию thinking=on
        # (effort=high) — при малом max_tokens это съедает лимит на
        # рассуждения, оставляя пустой content. Отключаем явно.
        "thinking": {"type": "disabled"},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    with net.urlopen_retry(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return json.loads(resp["choices"][0]["message"]["content"])


def cmd_synthesis(arg):
    """Сводная аналитика: high-priority заметки по компаниям, у которых
    их 2+ за последние N дней (по умолчанию 14). Одна заметка — это уже
    /why, здесь ценность именно в связке нескольких сразу, чего не видно
    построчно в дайджесте.

    Не переиспользует deepseek_deep_analysis — тот работает с одной
    заметкой плюс контекст истории, этот с явным набором заметок и
    вопросом "что видно вместе, чего не видно поодиночке" — разные
    промпты для разных задач, смешивать не стоит.
    """
    days = 14
    arg = (arg or "").strip()
    if arg.isdigit():
        days = max(3, min(int(arg), 30))
    history = load_json(HISTORY_FILE, {"items": []})
    items = history.get("items", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    high = [
        it for it in items
        if it.get("ts", "") >= cutoff and it.get("priority") == "high" and it.get("company")
    ]
    by_company = {}
    for it in high:
        by_company.setdefault(it["company"], []).append(it)
    clusters = {c: rows for c, rows in by_company.items() if len(rows) >= 2}
    if not clusters:
        tg_send(f"За {days} дн. нет компаний с 2+ high-priority заметками — нечего связывать.")
        return
    tg_send(f"⏳ Нашёл {len(clusters)} связку(и), синтезирую…")
    for company, rows in sorted(clusters.items(), key=lambda kv: -len(kv[1]))[:3]:
        rows.sort(key=lambda x: x.get("ts", ""))
        try:
            result = synthesize_cluster(company, rows)
        except Exception as e:
            tg_send(f"⚠️ {esc(company)}: синтез не удался ({esc(str(e)[:60])})")
            continue
        lines = [f"<b>🔗 {esc(company)}</b> — {len(rows)} заметки за {days} дн.\n"]
        for it in rows:
            ts = (it.get("ts") or "")[:10]
            lines.append(f"• [{ts}] {esc(it.get('title', ''))}")
        picture = esc((result.get("picture") or "").strip())
        conflicts = esc((result.get("conflicts") or "").strip())
        question = esc((result.get("question") or "").strip())
        if picture:
            lines.append(f"\n<b>картина:</b> {picture}")
        if conflicts:
            lines.append(f"<b>противоречия:</b> {conflicts}")
        if question:
            lines.append(f"<b>вопрос:</b> {question}")
        tg_send("\n".join(lines))


def cmd_weekly(arg):
    """Недельная сводка на почту: priority=high + весь UZCOPPER-орбита,
    7 дней. GMAIL_USER/GMAIL_APP_PASSWORD читаются лениво здесь, не на
    уровне модуля — команды, не касающиеся почты, не должны падать при
    импорте bot_commands, если эти секреты почему-то не заданы в env.

    Письмо шлётся самому себе (GMAIL_USER -> GMAIL_USER) — это архив/
    дайджест для перечитывания, не оповещение кого-то ещё.
    """
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not gmail_user or not gmail_password:
        tg_send("⚠️ GMAIL_USER/GMAIL_APP_PASSWORD не заданы — /weekly недоступна.")
        return

    history = load_json(HISTORY_FILE, {"items": []})
    items = history.get("items", [])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week_items = [it for it in items if it.get("ts", "") >= cutoff]
    if not week_items:
        tg_send("📭 За 7 дней нечего собрать — history.json пуст за этот период.")
        return

    high = [it for it in week_items if it.get("priority") == "high"]
    orbit = [it for it in week_items if it.get("uzcopper")]
    high.sort(key=lambda x: x.get("ts", ""), reverse=True)
    orbit.sort(key=lambda x: x.get("ts", ""), reverse=True)

    def render_section(title, rows):
        if not rows:
            return f"{title}\n(пусто)\n"
        lines = [title]
        for it in rows:
            ts = (it.get("ts") or "")[:10]
            lines.append(f"- [{ts}] {it.get('title', '')}")
            if it.get("why"):
                lines.append(f"    {it['why']}")
            lines.append(f"    {it.get('link', '')}")
        return "\n".join(lines) + "\n"

    now = datetime.now(timezone.utc)
    body = (
        f"Metals & Mining — недельная сводка\n"
        f"{(now - timedelta(days=7)).strftime('%d.%m')} — {now.strftime('%d.%m.%Y')}\n\n"
        f"{render_section(f'HIGH PRIORITY ({len(high)}):', high)}\n"
        f"{render_section(f'UZCOPPER-ОРБИТА ({len(orbit)}):', orbit)}"
    )

    import email.message
    msg = email.message.EmailMessage()
    msg["Subject"] = f"Metals digest — неделя до {now.strftime('%d.%m.%Y')}"
    msg["From"] = gmail_user
    msg["To"] = gmail_user
    msg.set_content(body)

    tg_send("⏳ Собираю и отправляю сводку…")
    try:
        net.smtp_send_retry("smtp.gmail.com", 465, gmail_user, gmail_password, msg)
    except Exception as e:
        tg_send(f"⚠️ Не удалось отправить письмо: {esc(str(e))}")
        return
    tg_send(
        f"📧 Отправлено на {esc(gmail_user)}: "
        f"{len(high)} high-priority, {len(orbit)} в UZCOPPER-орбите."
    )


# --- Watchlist -------------------------------------------------------------
# Подписки на ключевые слова: state_watchlist.json — просто список строк,
# сверка происходит в digest.py при публикации новой заметки (см. там
# check_watchlist), не здесь — bot_commands.py не участвует в основном
# прогоне. Эти три команды только читают/пишут сам список.

def cmd_watch(arg):
    term = (arg or "").strip().lower()
    if not term:
        tg_send("Формат: <code>/watch smelter restart</code>")
        return
    watchlist = load_json(WATCHLIST_FILE, {"terms": []})
    terms = watchlist.get("terms", [])
    if term in terms:
        tg_send(f"«{esc(term)}» уже в списке.")
        return
    terms.append(term)
    watchlist["terms"] = terms
    save_json(WATCHLIST_FILE, watchlist)
    tg_send(f"✅ Добавлено: «{esc(term)}». Алерт придёт при совпадении в новой заметке.")


def cmd_unwatch(arg):
    term = (arg or "").strip().lower()
    if not term:
        tg_send("Формат: <code>/unwatch smelter restart</code>")
        return
    watchlist = load_json(WATCHLIST_FILE, {"terms": []})
    terms = watchlist.get("terms", [])
    if term not in terms:
        tg_send(f"«{esc(term)}» не было в списке.")
        return
    terms.remove(term)
    watchlist["terms"] = terms
    save_json(WATCHLIST_FILE, watchlist)
    tg_send(f"❌ Убрано: «{esc(term)}».")


def cmd_watchlist():
    watchlist = load_json(WATCHLIST_FILE, {"terms": []})
    terms = watchlist.get("terms", [])
    if not terms:
        tg_send("Список подписок пуст. Добавь через <code>/watch слово</code>.")
        return
    lines = ["<b>👁 Подписки</b>\n"]
    for t in terms:
        lines.append(f"• {esc(t)}")
    tg_send("\n".join(lines))


# --- Dispatch ------------------------------------------------------------

COMMANDS = {
    "/digest": lambda arg: cmd_digest(),
    "/company": cmd_company,
    "/why": cmd_why,
    "/status": lambda arg: cmd_status(),
    "/orbit": cmd_orbit,
    "/search": cmd_search,
    "/feeds": lambda arg: cmd_feeds(),
    "/deep": cmd_deep,
    "/weekly": cmd_weekly,
    "/prices": lambda arg: cmd_prices(),
    "/synthesis": cmd_synthesis,
    "/watch": cmd_watch,
    "/unwatch": cmd_unwatch,
    "/watchlist": lambda arg: cmd_watchlist(),
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
