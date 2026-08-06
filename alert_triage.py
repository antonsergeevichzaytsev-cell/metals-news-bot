#!/usr/bin/env python3
"""Решает, слать ли алерт о падении воркфлоу, и шлёт его.

Зачем отдельным файлом, а не строками в alert_on_failure.yml:
правка любого файла под .github/workflows требует токена со scope
`workflow`, которого у коннектора нет. Логика, живущая здесь,
правится обычным доступом к репозиторию. yml остаётся тонкой
оболочкой: триггер + `python alert_triage.py`.

Критерий отправки
-----------------
Инфраструктурные отмены на стороне GitHub дают run.conclusion=failure
при job.conclusion=cancelled и ПУСТОМ списке шагов: раннер не выдали,
код не выполнялся ни строчки. Найдено 06.08.26 — авария Actions
(Major Outage, инцидент 15:22 UTC): filings/digest/evening отменялись
подряд, каждая отмена шла «упал». Пять ложных тревог за час — после
такого на алерт перестаёшь смотреть, и настоящее падение проходит мимо.

Шлём, только если хотя бы один job реально упал (conclusion=failure).
cancelled/skipped — молчим. Если jobs API не ответил, шлём: ложная
тревога дешевле пропущенного падения.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30


def _get(url, token):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "metals-news-bot-alert",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def decide(payload):
    """(отправлять?, причина). payload=None — API недоступен."""
    if not isinstance(payload, dict) or "jobs" not in payload:
        return True, "jobs API недоступен — не глотаем"
    jobs = payload["jobs"]
    failed = [j for j in jobs if j.get("conclusion") == "failure"]
    summary = ", ".join(
        f"{j.get('name')}={j.get('conclusion')}/{len(j.get('steps') or [])}шаг"
        for j in jobs
    )
    if failed:
        return True, f"упавших job: {len(failed)} [{summary}]"
    return False, f"ни одного упавшего job — инфраструктурная отмена [{summary}]"


def send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(
        {
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["RUN_ID"]
    gh_token = os.environ["GH_TOKEN"]

    try:
        payload = _get(
            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
            gh_token,
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"jobs API: {exc}")
        payload = None

    should_send, reason = decide(payload)
    print(reason)
    if not should_send:
        return 0

    name = os.environ.get("WF_NAME", "?")
    url = os.environ.get("WF_URL", "")
    sha = os.environ.get("WF_SHA", "")[:7]
    send(
        f"\U0001f534 <b>{name}</b> упал\n"
        f"Коммит: <code>{sha}</code>\n"
        f'<a href="{url}">Логи</a>'
    )
    print("алерт отправлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
