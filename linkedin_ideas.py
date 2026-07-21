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
    "UMMC $550M CapEx program director / ~50 млрд руб, FEL 1-3. "
    "RUSAL CapEx 1+ млрд руб (НЕ $350M), 7 patents (aluminium alloys). "
    "Norilsk Nickel foundry-forge shop turnaround, 305 staff, 0 LTI. "
    "RUSAL casting expansion 40 ktpa, 18% below industry benchmark. "
    "Voice anchors: 'both seats — who decides and who carries it for three years' / "
    "'red flags are still fixable'. Never opens with 'Меня зовут Антон Зайцев, 16 лет...'."
)

LINKEDIN_RULES = (
    "You are Anton Zaytsev's LinkedIn voice: senior industry operator in non-ferrous "
    "metals/mining, NOT a consultant. Someone who signed off on $550M-level budgets and "
    "ran production, not advised from outside.\n\n"
    "HARD RULES:\n"
    "- English only, first person.\n"
    "- No greetings. Never 'Great post / Thanks for sharing / Interesting perspective'.\n"
    "- First sentence is the hook: a sharp insight, counter-take, concrete number, or direct challenge.\n"
    "- At least one of: a real metric, a case reference, a concrete consequence.\n"
    "- Banned words: insightful, fascinating, truly, incredible, game-changer, revolutionary.\n"
    "- 3-5 short paragraphs: hook -> one idea -> case with a number -> takeaway/opinion. "
    "No CTA-begging, no hashtag spam.\n"
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
    """Собирает кандидатов из двух источников. Каждый кандидат:
    {source, title, note, url}. note — уже готовое обоснование от прошлого
    прогона (skip/why для digest, тема хука для filings), не выдумываем заново."""
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
    for it in dh.get("items", []):
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

    return candidates


def deepseek_call(system, user, max_tokens, temperature=0.3):
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(DEEPSEEK_URL, data=data, headers={
        "Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
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
        with urllib.request.urlopen(req, timeout=20) as r:
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
    posted = set(state.get("posted_hashes", []))

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
    posted.add(h)
    state["posted_hashes"] = list(posted)
    save_state(state)
    print(f"Sent post idea from {chosen['source']}: {chosen['title'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
