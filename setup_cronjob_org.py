#!/usr/bin/env python3
"""Разовый скрипт: создаёт 9 заданий на cron-job.org через их REST API.

Зачем: GitHub Actions `on: schedule` документирован как best-effort —
21.07.26 подтвердилось на практике: массовый пропуск плановых прогонов
за одно утро, без единой строки в логах. cron-job.org — независимый
внешний планировщик, который не наследует эту проблему: он сам бьёт по
GitHub API endpoint (workflow_dispatch через /dispatches), время
срабатывания гарантирует cron-job.org, не GitHub.

Каждое задание cron-job.org — это POST-запрос на
/repos/.../actions/workflows/{file}/dispatches с телом {"ref":"main"}
и заголовком Authorization: Bearer <DISPATCH_PAT>.

Запускать один раз. Повторный запуск создаст дубликаты заданий (API
create не идемпотентен по title) — если нужно поменять расписание,
редактировать через cron-job.org Console вручную или писать отдельный
update-скрипт по jobId.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

CRONJOB_API_KEY = os.environ["CRONJOB_ORG_API_KEY"]
DISPATCH_PAT = os.environ["DISPATCH_PAT"]
REPO = "antonsergeevichzaytsev-cell/metals-news-bot"

CRONJOB_ENDPOINT = "https://api.cron-job.org"

# (title, workflow_file, [(hour_msk, minute), ...], wdays)
# wdays: [-1] = каждый день недели (мы всё равно фильтруем через 1-5 в
# самом расписании GitHub, здесь используем 1,2,3,4,5 = Пн-Пт где нужно;
# 0 = воскресенье для weekly_check).
# 21.07.26: первый прогон создал только 7 из 13 заданий (упёрся в rate limit
# 5/мин cron-job.org, throttle тогда был неверный). Список ниже урезан до
# оставшихся 6 для второго прогона - не пересоздавать уже созданные.
JOBS = [
    ("Filings watcher #5", "filings.yml", [(1, 0)], [1, 2, 3, 4, 5]),
    ("Inbox consolidator", "inbox.yml",
     [(10, 0), (12, 0), (14, 0), (16, 0), (18, 0)], [1, 2, 3, 4, 5]),
    ("Pipeline sync", "pipeline_sync.yml",
     [(h, m) for h in range(10, 19) for m in (0, 30)], [1, 2, 3, 4, 5]),
    ("Account watch", "account_watch.yml", [(13, 15), (17, 15)], [1, 2, 3, 4, 5]),
    ("Evening digest", "evening_digest.yml", [(18, 30)], [1, 2, 3, 4, 5]),
    ("Weekly Check", "weekly_check.yml", [(19, 0)], [0]),
]


def cronjob_request(method, path, payload=None):
    url = CRONJOB_ENDPOINT + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {CRONJOB_API_KEY}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        tag = "RATE LIMIT" if e.code == 429 else "HTTP ERROR"
        print(f"  ! {tag} {e.code} for {method} {path}: {body}", file=sys.stderr)
        return None


def build_job_payload(title, wf_file, slots, wdays):
    hours = sorted({h for h, m in slots})
    minutes = sorted({m for h, m in slots})
    # Если часы/минуты не образуют полное декартово произведение (напр.
    # 08:30 и 20:00 — разные минуты для разных часов), cron-job.org всё
    # равно сработает на всех комбинациях hours x minutes, что создаст
    # лишние срабатывания. Для таких случаев (digest, filings) заводим
    # отдельные под-задания по одному на каждый уникальный (h,m).
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{wf_file}/dispatches"
    return {
        "job": {
            "url": url,
            "enabled": True,
            "title": title,
            "requestMethod": 1,  # POST
            "schedule": {
                "timezone": "Europe/Moscow",
                "hours": hours,
                "minutes": minutes,
                "mdays": [-1],
                "months": [-1],
                "wdays": wdays,
            },
            "extendedData": {
                "headers": {
                    "Authorization": f"Bearer {DISPATCH_PAT}",
                    "Accept": "application/vnd.github+json",
                },
                "body": '{"ref":"main"}',
            },
        }
    }


def needs_split(slots):
    """True если часы и минуты не образуют чистое произведение —
    тогда одно cron-job.org задание с hours=[..] x minutes=[..] сработает
    на комбинациях, которых мы не просили (напр. digest 08:00+20:00 дало
    бы 08:00, 08:20(нет, но с одной minute ок)... для filings с разными
    minutes на разных hours — точно нужен split)."""
    hours = sorted({h for h, m in slots})
    minutes = sorted({m for h, m in slots})
    product = {(h, m) for h in hours for m in minutes}
    return product != set(slots)


def main():
    created = []
    request_count = 0

    def throttled_create(payload):
        nonlocal request_count
        # Лимит cron-job.org для создания задания: 1/сек И 5/мин. После
        # каждых 5 запросов ждём остаток минуты, не только 1.2с между
        # соседними — иначе 6-й+ запрос получает 429 и теряется молча
        # (обнаружено 21.07: из 13 ожидаемых создалось только 7 - ровно
        # столько, сколько успело пройти до упора в 5/мин лимит).
        if request_count > 0 and request_count % 5 == 0:
            print(f"  rate limit pause (created {request_count} so far, waiting 65s)...")
            time.sleep(65)
        else:
            time.sleep(1.5)
        request_count += 1
        return cronjob_request("PUT", "/jobs", payload)

    for title, wf_file, slots, wdays in JOBS:
        if needs_split(slots):
            print(f"{title}: slots don't form a clean grid, creating {len(slots)} separate jobs")
            for i, (h, m) in enumerate(slots, 1):
                payload = build_job_payload(f"{title} #{i}", wf_file, [(h, m)], wdays)
                result = throttled_create(payload)
                if result and "jobId" in result:
                    created.append((f"{title} #{i}", result["jobId"]))
                    print(f"  created jobId={result['jobId']} at {h:02d}:{m:02d} MSK")
                else:
                    print(f"  ! FAILED to create {title} #{i} at {h:02d}:{m:02d} MSK", file=sys.stderr)
        else:
            payload = build_job_payload(title, wf_file, slots, wdays)
            result = throttled_create(payload)
            if result and "jobId" in result:
                created.append((title, result["jobId"]))
                print(f"{title}: created jobId={result['jobId']}, slots={slots}")
            else:
                print(f"  ! FAILED to create {title}", file=sys.stderr)

    print(f"\nDone. Created {len(created)} job(s):")
    for title, job_id in created:
        print(f"  {job_id}  {title}")
    return 0 if len(created) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
