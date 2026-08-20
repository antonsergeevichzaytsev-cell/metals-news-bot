#!/usr/bin/env python3
"""LinkedIn ideas -> Telegram, раз в день (будни).

Источники темы, в порядке приоритета:
  1. filings-хуки, помеченные Антоном "+" за последние 3 дня (filings_history.json["labels"])
     — сигнал уже прошёл человеческую фильтрацию, самый сильный материал.
  2. Общий рыночный digest за последние 24ч (history.json["items"]) — то, что
     помечено priority "high" в предыдущих прогонах digest.py, если такая метка
     есть в самой записи (иначе весь свежий пул).

Если ни то ни другое не даёт материала уровня поста (а не просто "было
упоминание") — бот молчит. Это прямое следствие правила из скилла linkedin:
"Вне зоны → skip." Пустой день лучше слабого поста.

Генерация — двухшаговая, не одним вызовом:
  Шаг 1 (SELECT): из пула кандидатов выбрать ОДИН лучший угол, коротким
  json-вердиктом. Дёшево, можно прогнать по многим кандидатам разом.
  Шаг 2 (WRITE): полный пост по выбранному углу, строго по правилам скилла
  linkedin (голос, структура, длина, факты). Дороже, вызывается один раз.

Факты об Антоне (кейсы, цифры) — канон из SKILL.md напрямую в промпте, не
из anton_state.json: тот использует другие цифры для RUSAL CapEx (350M vs
скилл говорит "1+ млрд руб, НЕ $350M") — расхождение уже отмечено при сборке
этого бота 21.07, не исправлено само по себе, скилл главнее как более новая
и явно исправленная версия.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import net
import prices as pr
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
FILINGS_HISTORY_PATH = os.path.join(ROOT, "filings_history.json")
DIGEST_HISTORY_PATH = os.path.join(ROOT, "history.json")
STATE_PATH = os.path.join(ROOT, "state_linkedin_ideas.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

LABEL_WINDOW_DAYS = 3
DIGEST_WINDOW_HOURS = 30

# --- Канон голоса, дословно из /mnt/skills/user/linkedin/SKILL.md 21.07.26 ---
LINKEDIN_VOICE_FACTS = (
    "UMMC $550M CapEx program director / ~50 млрд руб, FEL 1-3, gold-from-tailings greenfield. "
    "RUSAL CapEx 1+ млрд руб (НЕ $350M), 7 patents (aluminium alloys), Head of Technology. "
    "Norilsk Nickel foundry-forge shop turnaround, 305 staff, 0 LTI, $4.2M savings. "
    "RUSAL casting expansion 40 ktpa, 18% below industry benchmark. "
    "Never opens with 'Меня зовут Антон Зайцев, 16 лет...'."
)

# Примеры ТОНА, не готовые строки для копирования. Найдено 21.07: модель
# вставляла 'Red flags are still fixable' буквально в пост про South32/Alcoa,
# где речь вообще не шла о red flags — фраза сработала как чек-бокс, не как
# мысль. Anchor описывает КАК он формулирует мысли этого типа, а не что
# писать всегда. Если тема поста не про решения/риск-разделение — эти
# анкоры вообще не должны появиться, ни в каком виде.
VOICE_TONE_NOTE = (
    "Two recurring THEMES in his voice (use only when the post is actually about that theme, "
    "never as a decorative closing line): (a) distinguishing the person who decides from the "
    "person who lives with the consequences for years — comes up when discussing accountability "
    "or decision-making structure specifically; (b) technical/operational risk being addressable "
    "if caught early in due diligence — comes up when discussing DD, risk assessment, or red "
    "flags specifically. If today's topic is neither of those, don't reach for either theme."
)

LINKEDIN_RULES = (
    "You are Anton Zaytsev's LinkedIn voice: senior industry operator in non-ferrous "
    "metals/mining, NOT a consultant. Someone who signed off on $550M-level budgets and "
    "ran production, not advised from outside.\n\n"
    "HARD RULES:\n"
    "- English only, first person.\n"
    "- No greetings. Never 'Great post / Thanks for sharing / Interesting perspective'.\n"
    "- First sentence is the hook: a sharp insight, counter-take, concrete number, or direct challenge.\n"
    "- Banned words: insightful, fascinating, truly, incredible, game-changer, revolutionary.\n"
    "- 3-5 short paragraphs: hook -> develop the idea -> takeaway/opinion.\n"
    "- A personal case reference (UMMC/RUSAL/Norilsk Nickel) is a BONUS, not a requirement. "
    "Include one ONLY if it is genuinely, specifically relevant to this exact topic — same "
    "commodity, same type of decision, or a directly comparable situation. A case that needs "
    "'would have', 'this reminds me of', or any hedge to connect it to the topic is NOT genuinely "
    "relevant — leave it out entirely rather than force it. A post with zero case references but "
    "a sharp, well-argued opinion is a fully successful output.\n"
    f"- {VOICE_TONE_NOTE}\n"
    "- No CTA-begging, no hashtag spam.\n"
    "- Domain: mining, non-ferrous, aluminium, CapEx, FEL, turnaround, equipment, commodity markets. "
    "If the material doesn't genuinely fit, say so plainly instead of forcing a post.\n\n"
    f"FACTS (only use these, never invent numbers or cases): {LINKEDIN_VOICE_FACTS}"
)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_state():
    return load_json(STATE_PATH, {"posted_hashes": []})


def save_state(state):
    state["posted_hashes"] = state.get("posted_hashes", [])[-200:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def gather_candidates():
    """Собирает кандидатов из пяти источников. Каждый кандидат:
    {source, title, note, url}. note — уже готовое обоснование от прошлого
    прогона (skip/why для digest, тема хука для filings), не выдумываем заново.

    19.08.2026: добавлены три новых источника (facts_shifts, company_clusters,
    price_context) поверх исходных двух (filings labels, digest priority).
    До этого момента у бота были только сырые новости — ни связок цифр во
    времени, ни повторных упоминаний одной компании, ни контекста цены
    металла не попадало в материал, хотя вся эта логика уже существовала
    в системе (facts.compare_facts в filings.py, priority-кластеры в
    cmd_synthesis, prices.py) и просто не была подключена сюда.
    """
    candidates = []

    fh = load_json(FILINGS_HISTORY_PATH, {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LABEL_WINDOW_DAYS)).isoformat()
    for lab in fh.get("labels", []):
        if lab.get("label") != "good":
            continue
        if lab.get("ts", "") < cutoff:
            continue
        candidates.append({
            "source": "filings",
            "title": lab.get("topic") or lab.get("title", ""),
            "note": lab.get("note", ""),
            "url": lab.get("url", ""),
        })

    dh = load_json(DIGEST_HISTORY_PATH, {})
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=DIGEST_WINDOW_HOURS)
    digest_items = dh.get("items", [])
    for it in digest_items:
        try:
            ts = datetime.fromisoformat(it.get("ts", "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ts < cutoff_dt:
            continue
        if it.get("priority") not in ("high", "medium"):
            continue  # low-priority рыночный шум не годится для поста
        candidates.append({
            "source": "digest",
            "title": it.get("title", ""),
            "note": it.get("why", ""),
            "url": it.get("link", ""),
        })

    candidates.extend(gather_facts_shifts_candidates(fh))
    candidates.extend(gather_company_cluster_candidates(digest_items))
    candidates.extend(gather_price_context_candidates(digest_items))

    return candidates


def gather_facts_shifts_candidates(filings_history):
    """Источник 3: filings-записи, где facts.compare_facts уже нашла
    сдвиг во времени (сумма/срок/извлечение изменились с прошлого
    упоминания того же проекта) — вычисляется и сохраняется в
    filings.py, здесь просто читаем готовое поле 'shifts'. Не требует
    ручной метки '+' от Антона (в отличие от источника filings выше) —
    сам факт сдвига уже сигнал, независимо от того, размечен ли пост."""
    candidates = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LABEL_WINDOW_DAYS)).isoformat()
    for it in filings_history.get("items", []):
        shifts = it.get("shifts")
        if not shifts:
            continue
        if it.get("ts", "") < cutoff:
            continue
        company = it.get("company", "")
        project = it.get("project", "")
        where = f"{company} — {project}" if project else company
        candidates.append({
            "source": "facts_shift",
            "title": f"{where}: {'; '.join(shifts)}",
            "note": it.get("why", ""),
            "url": it.get("link", ""),
        })
    return candidates


def gather_company_cluster_candidates(digest_items, min_count=2, window_days=7):
    """Источник 4: одна компания упомянута 2+ раз за неделю среди
    high/medium-priority заметок digest — та же логика, что в
    cmd_synthesis (bot_commands.py), но здесь не требует ручной
    команды /synthesis, срабатывает сама при сборе кандидатов.
    Повторное появление одной компании — сигнал тренда, не шума,
    даже если ни одна отдельная заметка не выглядела как пост."""
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=window_days)
    by_company = {}
    for it in digest_items:
        company = (it.get("company") or "").strip()
        if not company:
            continue
        if it.get("priority") not in ("high", "medium"):
            continue
        try:
            ts = datetime.fromisoformat(it.get("ts", "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if ts < cutoff_dt:
            continue
        by_company.setdefault(company.lower(), []).append(it)

    candidates = []
    for key, items in by_company.items():
        if len(items) < min_count:
            continue
        company_name = items[0].get("company", key)
        titles = [i.get("title", "") for i in items]
        candidates.append({
            "source": "company_cluster",
            "title": f"{company_name}: {len(items)} упоминания за {window_days} дн — " + " | ".join(titles[:3]),
            "note": " / ".join(i.get("why", "") for i in items if i.get("why")),
            "url": items[-1].get("link", ""),
        })
    return candidates


def gather_price_context_candidates(digest_items, window_hours=24, move_threshold_pct=2.0):
    """Источник 5: если цена меди или алюминия за последние сутки
    сдвинулась заметно (>= move_threshold_pct), связывает это с
    рыночными new digest-заметками того же металла за то же окно —
    'цена упала X% + новость про закрытие смелтера' куда содержательнее
    голой новости или голой цены по отдельности. Молчит, если цены
    недоступны (сетевой сбой) или сдвиг незначительный."""
    try:
        prices = pr.fetch_prices()
    except Exception as e:
        print(f"  ! price_context: fetch_prices failed: {e}", file=sys.stderr)
        return []

    candidates = []
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    keywords = {"Cu": ("copper", "медь", "медн"), "Al": ("aluminium", "aluminum", "алюмин")}

    for sym, (price, chg, src) in prices.items():
        if chg is None or abs(chg) < move_threshold_pct:
            continue
        related = []
        for it in digest_items:
            try:
                ts = datetime.fromisoformat(it.get("ts", "").replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if ts < cutoff_dt:
                continue
            title_lower = (it.get("title", "") + " " + it.get("desc", "")).lower()
            if any(kw in title_lower for kw in keywords.get(sym, ())):
                related.append(it)
        if not related:
            continue  # движение цены без связанной новости — не пост, просто цифра
        arrow = "выросла" if chg > 0 else "упала"
        metal_name = {"Cu": "Медь", "Al": "Алюминий"}.get(sym, sym)
        candidates.append({
            "source": "price_context",
            "title": f"{metal_name} {arrow} на {abs(chg):.1f}% (${price:,.0f}/т, {src}) на фоне: {related[0].get('title', '')}",
            "note": related[0].get("why", ""),
            "url": related[0].get("link", ""),
        })
    return candidates


def deepseek_call(system, user, max_tokens, temperature=0.3):
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        # 20.08.2026: см. digest.py:deepseek_enrich — thinking mode
        # отключён явно, иначе default effort=high съедает max_tokens.
        "thinking": {"type": "disabled"},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(DEEPSEEK_URL, data=data, headers={
        "Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"})
    with net.urlopen_retry(req, timeout=45) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return json.loads(resp["choices"][0]["message"]["content"])


SELECT_SYS = (
    "You screen news candidates for a LinkedIn post idea for a senior non-ferrous "
    "metals/mining operator (see voice/domain rules). From the candidate list, pick "
    "AT MOST ONE that has genuine LinkedIn-post potential: something with a real "
    "operational, strategic, or numeric angle he could credibly comment on with his "
    "own experience — not just 'mentioned in the news'. "
    "If NONE of the candidates clear that bar, say so explicitly. "
    f"{LINKEDIN_RULES}\n\n"
    "Reply ONLY with valid JSON: "
    "{\"has_candidate\": bool, \"index\": int (0-based, -1 if none), \"angle\": str "
    "(one sentence: what specific angle makes this postable)}."
)

WRITE_SYS = (
    "Write the actual LinkedIn post now, following every rule above exactly. "
    "Output ONLY the post text as a JSON field, nothing else. "
    f"{LINKEDIN_RULES}\n\n"
    "Reply ONLY with valid JSON: {\"post\": str}."
)


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
        with net.urlopen_retry(req, timeout=20) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        print(f"  ! telegram error {e.code}: {e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        return False


def render(candidate, angle, post_text):
    lines = ["\U0001f4dd <b>Тема на сегодня для LinkedIn</b>", ""]
    lines.append(f"<i>{esc(angle)}</i>")
    lines.append(f"<code>источник: {esc(candidate['source'])} \u00b7 {esc(candidate['title'][:80])}</code>")
    if candidate.get("url"):
        lines.append(f'<a href="{esc(candidate["url"])}">исходник</a>')
    lines.append("")
    lines.append("\u2500\u2500\u2500")
    lines.append("")
    lines.append(esc(post_text))
    return "\n".join(lines)


def main():
    state = load_state()
    # dict, а не set: state[...] обрезается срезом [-N:], а у множества
    # порядок произвольный — обрезка выбрасывала бы случайные записи
    # вместо самых старых. В digest.py этот же дефект дал 5 повторных
    # публикаций за неделю (28.07-03.08). Мембершип-тест не меняется.
    posted = dict.fromkeys(state.get("posted_hashes", []))

    candidates = gather_candidates()
    print(f"Gathered {len(candidates)} candidate(s): "
          f"{sum(1 for c in candidates if c['source']=='filings')} filings, "
          f"{sum(1 for c in candidates if c['source']=='digest')} digest")

    if not candidates:
        print("No candidates today - nothing to post.")
        save_state(state)
        return 0

    candidates_text = "\n".join(
        f"[{i}] ({c['source']}) {c['title']} — {c['note']}"
        for i, c in enumerate(candidates)
    )

    try:
        verdict = deepseek_call(SELECT_SYS, candidates_text, max_tokens=150)
    except Exception as e:
        print(f"  ! select call failed: {e}", file=sys.stderr)
        save_state(state)
        return 1

    if not verdict.get("has_candidate") or verdict.get("index", -1) < 0:
        print(f"No postable angle today: {verdict.get('angle', 'no reason given')}")
        save_state(state)
        return 0

    idx = verdict["index"]
    if idx >= len(candidates):
        print(f"  ! model returned out-of-range index {idx}", file=sys.stderr)
        save_state(state)
        return 1

    chosen = candidates[idx]
    angle = verdict.get("angle", "")

    # Не постить дважды один и тот же источник в разные дни, если он
    # почему-то остаётся в окне (напр. digest 30ч окно пересекает 2 прогона).
    h = f"{chosen['source']}:{chosen['title'][:100]}"
    if h in posted:
        print(f"Already posted this candidate before: {chosen['title'][:60]} - skipping")
        save_state(state)
        return 0

    try:
        result = deepseek_call(
            WRITE_SYS,
            f"CANDIDATE: {chosen['title']}\nNOTE: {chosen['note']}\nANGLE: {angle}",
            max_tokens=500, temperature=0.4,
        )
    except Exception as e:
        print(f"  ! write call failed: {e}", file=sys.stderr)
        save_state(state)
        return 1

    post_text = result.get("post", "").strip()
    if not post_text:
        print("  ! empty post text returned", file=sys.stderr)
        save_state(state)
        return 1

    tg_send(render(chosen, angle, post_text))
    posted[h] = None
    state["posted_hashes"] = list(posted)
    save_state(state)
    print(f"Sent post idea from {chosen['source']}: {chosen['title'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
