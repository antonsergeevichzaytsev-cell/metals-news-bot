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
JOBS = [
    ("Mission Control", "mission_control.yml", [(7, 45)], [1, 2, 3, 4, 5]),
    ("Anton Daily brief", "daily_brief.yml", [(8, 30)], [1, 2, 3, 4, 5]),
    ("Metals digest", "digest.yml", [(8, 0), (20, 0)], [1, 2, 3, 4, 5]),
    ("Filings watcher", "filings.yml",
     [(8, 30), (16, 0), (19, 0), (22, 0), (1, 0)], [1, 2, 3, 4, 5]),
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
        print(f"  ! HTTP {e.code} for {method} {path}: {body}", file=sys.stderr)
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
    for title, wf_file, slots, wdays in JOBS:
        if needs_split(slots):
            print(f"{title}: slots don't form a clean grid, creating {len(slots)} separate jobs")
            for i, (h, m) in enumerate(slots, 1):
                payload = build_job_payload(f"{title} #{i}", wf_file, [(h, m)], wdays)
                result = cronjob_request("PUT", "/jobs", payload)
                if result and "jobId" in result:
                    created.append((f"{title} #{i}", result["jobId"]))
                    print(f"  created jobId={result['jobId']} at {h:02d}:{m:02d} MSK")
                time.sleep(1.2)  # rate limit: 1 req/sec, 5/min for job creation
        else:
            payload = build_job_payload(title, wf_file, slots, wdays)
            result = cronjob_request("PUT", "/jobs", payload)
            if result and "jobId" in result:
                created.append((title, result["jobId"]))
                print(f"{title}: created jobId={result['jobId']}, slots={slots}")
            time.sleep(1.2)

    print(f"\nDone. Created {len(created)} job(s):")
    for title, job_id in created:
        print(f"  {job_id}  {title}")
    return 0 if len(created) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
