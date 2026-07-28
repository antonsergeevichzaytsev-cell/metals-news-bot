#!/usr/bin/env python3
"""Eval для filings.py — единственная проверка, действительно ли гейт
DeepSeek (prefilter + screen) работает, а не просто "не падает".

Технические тесты (test_filings.py) проверяют детерминированную логику
(prefilter regex, parse_verdict) — они не могут проверить, хорошие ли
решения принимает LLM, потому что LLM недетерминирован и "правильный"
ответ — вопрос человеческого суждения, не assert. Единственный источник
правды здесь — разметка Антона (+/− кнопки, добавлены 27.07.2026).

На 28.07.2026: 28 items отправлено, 0 labels — ни разу за всю историю
репозитория (с 17.07). Это не пробел в инфраструктуре, это факт: разметка
физически не происходила. Скрипт это явно называет, а не молчит и не
делает вид, что метрики посчитаны на пустоте.

Запуск: python3 eval_filings.py
Порог MIN_LABELS_FOR_VERDICT — ниже него любые проценты вводят в
заблуждение (одна разметка = 100% или 0%, оба бессмысленны).
"""
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(ROOT, "filings_history.json")

MIN_LABELS_FOR_VERDICT = 20  # меньше — доля срабатывания статистически не значит ничего


def load_history():
    with open(HISTORY_PATH, encoding="utf-8") as f:
        return json.load(f)


def summarize_labels(history):
    """Считает распределение вердиктов и связывает их с priority,
    которую гейт присвоил тому же item (по совпадению link).

    Возвращает dict с ключами:
    - total_items: сколько хуков вообще отправлено
    - total_labels: сколько размечено
    - verdict_counts: Counter{'good': N, 'bad': N, 'note': N}
    - by_priority: Counter{(priority, verdict): N} — распределение вердиктов
      внутри каждого priority (high/medium/low), чтобы увидеть, не мажет ли
      гейт систематически на каком-то одном уровне приоритета
    """
    items_by_link = {it.get("link"): it for it in history.get("items", []) if it.get("link")}
    labels = history.get("labels", [])

    verdict_counts = Counter(lab.get("verdict", "note") for lab in labels)
    by_priority = Counter()
    for lab in labels:
        link = lab.get("link")
        item = items_by_link.get(link)
        priority = (item.get("priority") if item else lab.get("priority")) or "unknown"
        by_priority[(priority, lab.get("verdict", "note"))] += 1

    return {
        "total_items": len(history.get("items", [])),
        "total_labels": len(labels),
        "verdict_counts": verdict_counts,
        "by_priority": by_priority,
    }


def render_report(summary):
    lines = []
    lines.append(f"Items отправлено: {summary['total_items']}")
    lines.append(f"Labels получено: {summary['total_labels']}")
    lines.append("")

    if summary["total_labels"] == 0:
        lines.append("НЕТ ДАННЫХ. Ни одной разметки за всё время — eval невозможен.")
        lines.append("Это не значит, что гейт плохой или хороший: значит, что судить не на чем.")
        lines.append("Единственный способ узнать — нажимать 👍/👎 на хуки, когда они приходят.")
        return "\n".join(lines)

    vc = summary["verdict_counts"]
    lines.append(f"Вердикты: good={vc.get('good', 0)}, bad={vc.get('bad', 0)}, note={vc.get('note', 0)}")

    if summary["total_labels"] < MIN_LABELS_FOR_VERDICT:
        lines.append("")
        lines.append(
            f"ПРЕДУПРЕЖДЕНИЕ: {summary['total_labels']} labels < {MIN_LABELS_FOR_VERDICT} — "
            f"любой процент на этой выборке статистически не значит ничего. "
            f"Цифры ниже показаны для информации, не как вердикт по гейту."
        )

    lines.append("")
    lines.append("Разбивка по priority гейта x вердикт человека:")
    for (priority, verdict), n in sorted(summary["by_priority"].items()):
        lines.append(f"  {priority:8} | {verdict:5} | {n}")

    return "\n".join(lines)


def main():
    try:
        history = load_history()
    except FileNotFoundError:
        print(f"Не найден {HISTORY_PATH}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, OSError) as e:
        print(f"Не удалось прочитать {HISTORY_PATH}: {e}", file=sys.stderr)
        return 1

    summary = summarize_labels(history)
    print(render_report(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
