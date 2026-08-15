#!/usr/bin/env python3
"""
Единая точка входа сервиса на Railway.

Заменяет собой сразу два механизма, которые были раньше:

  1. Тринадцать воркфлоу GitHub Actions с крон-расписаниями — теперь один
     процесс с APScheduler. Времена те же, часовой пояс Ташкента.
  2. Cloudflare Worker, принимавший вебхук Telegram и дёргавший
     repository_dispatch. Теперь вебхук приходит прямо сюда. Заодно отпадает
     classic PAT со scope repo, истекавший 15 августа: он был нужен только
     Worker'у.

Как устроено размещение. Модули бота вычисляют пути от собственного файла
(ROOT = dirname(__file__)), поэтому код раскладывается на постоянный том и
запускается оттуда — и состояние оказывается на томе само, без правок в
пяти тысячах строк бизнес-логики. Правила раскладки в seed.py: код едет из
образа и перезаписывается каждый деплой, состояние живёт на томе и образом
не трогается.

Зависимости: apscheduler. Веб-слой — стандартная библиотека, тащить фреймворк
ради двух ручек незачем.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import api
import seed

# APP — образ, откуда стартовал сервис. DATA — постоянный том, где живут
# и код, и состояние. Без DATA_DIR оба совпадают: это локальный запуск.
APP = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", APP)
TZ = os.environ.get("TZ_NAME", "Asia/Tashkent")
PORT = int(os.environ.get("PORT", "8080"))

# Секрет вебхука: Telegram присылает его в заголовке
# X-Telegram-Bot-Api-Secret-Token, если setWebhook вызван с secret_token.
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Расписание
#
# Времена местные, Ташкент (UTC+5). Узбекистан часы не переводит, сезонной
# правки не потребуется. Перенесено один в один из крон-выражений Actions;
# при расхождении правда за .github/workflows/.
#
# Формат: (имя, модуль, час, минута, дни недели, таймаут в секундах)
# ---------------------------------------------------------------------------
JOBS = [
    ("mission_control", "mission_control", "7",              "45",   "*",   300),
    ("digest_morning",  "digest",          "8",              "0",    "1-5", 480),
    ("daily_brief",     "daily_brief",     "8",              "30",   "*",   300),
    ("filings_early",   "filings",         "8",              "30",   "*",   600),
    ("linkedin_ideas",  "linkedin_ideas",  "9",              "0",    "*",   300),
    ("pipeline_sync",   "pipeline_sync",   "10-18",          "0,30", "*",   300),
    ("inbox",           "inbox",           "10,12,14,16,18", "0",    "*",   300),
    ("account_watch_1", "account_watch",   "13",             "15",   "*",   300),
    ("filings_day",     "filings",         "16",             "0",    "*",   600),
    ("account_watch_2", "account_watch",   "17",             "15",   "*",   300),
    ("evening_digest",  "evening_digest",  "18",             "30",   "*",   300),
    ("filings_evening", "filings",         "19",             "0",    "*",   600),
    ("digest_evening",  "digest",          "20",             "0",    "1-5", 480),
    ("filings_night",   "filings",         "22",             "0",    "*",   600),
    ("filings_late",    "filings",         "1",              "0",    "*",   600),
    ("weekly_check",    "weekly_check",    "19",             "0",    "sun", 600),
]

# Глобальный замок. Раньше эту роль играла concurrency group repo-writes.
# Гонок за файлы состояния больше нет, но два тяжёлых модуля разом на
# маленьком контейнере — лишняя нагрузка. Держим строго по одному.
RUN_LOCK = threading.Lock()

# Последние результаты — их отдаёт /health, чтобы не лезть в логи.
LAST: dict[str, dict] = {}


def run_module(name: str, module: str, timeout: int) -> None:
    """Запустить модуль отдельным процессом. Падение одного не роняет сервис."""
    if not RUN_LOCK.acquire(timeout=max(timeout, 60)):
        log.warning("[%s] пропуск: предыдущая задача всё ещё идёт", name)
        return
    started = time.time()
    try:
        script = os.path.join(DATA, module + ".py")
        if not os.path.exists(script):
            log.error("[%s] нет файла %s — модуль не приехал на том", name, script)
            LAST[name] = {"status": "нет файла модуля"}
            return
        log.info("[%s] старт", name)
        proc = subprocess.run(
            [sys.executable, script],
            cwd=DATA,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        took = time.time() - started
        for ln in (proc.stdout or "").strip().splitlines()[-8:]:
            log.info("[%s] %s", name, ln)
        if proc.returncode == 0:
            log.info("[%s] готово за %.1f c", name, took)
            LAST[name] = {"status": "ok", "took_sec": round(took, 1)}
        else:
            log.error("[%s] код возврата %s за %.1f c", name, proc.returncode, took)
            for ln in (proc.stderr or "").strip().splitlines()[-15:]:
                log.error("[%s] stderr | %s", name, ln)
            LAST[name] = {"status": "ошибка", "rc": proc.returncode,
                          "took_sec": round(took, 1)}
    except subprocess.TimeoutExpired:
        log.error("[%s] таймаут %d c", name, timeout)
        LAST[name] = {"status": "таймаут"}
    except Exception:
        log.exception("[%s] упал с исключением", name)
        LAST[name] = {"status": "исключение"}
    finally:
        RUN_LOCK.release()


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(
        timezone=TZ,
        job_defaults={
            "coalesce": True,           # накопившиеся пропуски — в один запуск
            "max_instances": 1,
            "misfire_grace_time": 900,  # деплой на 15 минут не съест задачу
        },
    )
    for name, module, hour, minute, dow, timeout in JOBS:
        sched.add_job(
            run_module,
            CronTrigger(hour=hour, minute=minute, day_of_week=dow, timezone=TZ),
            args=[name, module, timeout],
            id=name,
            name=name,
        )
    return sched


# ---------------------------------------------------------------------------
# Веб-слой: вебхук Telegram, health-check и ручки состояния
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "metals-bot"

    def _reply(self, code: int, body: str = "ok", ctype: str = "text/plain") -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code: int, obj) -> None:
        self._reply(code, json.dumps(obj, ensure_ascii=False, indent=2),
                    "application/json")

    def log_message(self, fmt, *args):
        log.info("http | " + fmt, *args)

    def do_GET(self):
        if self.path.startswith("/health"):
            jobs = []
            if SCHED:
                for j in SCHED.get_jobs():
                    jobs.append({
                        "id": j.id,
                        "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                        "last": LAST.get(j.id),
                    })
            self._json(200, {
                "status": "ok",
                "tz": TZ,
                "data_dir": DATA,
                "volume": DATA != APP,
                "modules_on_volume": sum(
                    1 for n in os.listdir(DATA) if n.endswith(".py")
                ) if os.path.isdir(DATA) else 0,
                "jobs": jobs,
                "state_api": "on" if os.environ.get("API_TOKEN") else "off (API_TOKEN не задан)",
            })
        elif self.path.startswith(("/state", "/pipeline", "/filings")):
            # Эти ручки отдают pipeline: компании, адреса, суммы. Ровно то,
            # что мы только что убрали из публичного доступа. Поэтому без
            # токена — отказ, а не открытая дверь.
            if not api.check_token(self.headers.get("Authorization")):
                if not os.environ.get("API_TOKEN"):
                    log.warning("state API: запрос при пустом API_TOKEN — отказ")
                    self._json(503, {"error": "API_TOKEN не задан, ручки состояния выключены"})
                else:
                    log.warning("state API: неверный токен, путь %s", self.path)
                    self._json(401, {"error": "нужен заголовок Authorization: Bearer <API_TOKEN>"})
                return
            code, body = api.handle(self.path, DATA)
            self._json(code, body)
        else:
            self._reply(200, "metals-news-bot")

    def do_POST(self):
        if not self.path.startswith("/tg"):
            self._reply(404, "not found")
            return

        # Секрет проверяем до чтения тела.
        if WEBHOOK_SECRET:
            got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if got != WEBHOOK_SECRET:
                log.warning("вебхук: неверный секрет, запрос отброшен")
                self._reply(403, "forbidden")
                return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        # Telegram ждёт быстрый 200: затянем — начнёт слать повторы.
        self._reply(200, "ok")
        threading.Thread(target=self._handle_update, args=(raw,), daemon=True).start()

    def _handle_update(self, raw: str) -> None:
        try:
            update = json.loads(raw)
        except Exception:
            log.exception("вебхук: не разобрал JSON")
            return
        try:
            import bot_commands  # с тома: см. sys.path в main()
            bot_commands.handle_update(update)
        except Exception:
            log.exception("вебхук: обработчик команды упал")


SCHED: BackgroundScheduler | None = None


def main() -> int:
    global SCHED
    os.makedirs(DATA, exist_ok=True)

    # Разложить код и состояние на том.
    try:
        seed.run(APP, DATA)
    except Exception:
        log.exception("раскладка не отработала, продолжаю — задачи могут падать")

    # Команды Telegram выполняются в этом же процессе, поэтому bot_commands и
    # всё, что он тянет, импортируем с тома: тогда их ROOT — это /data.
    if DATA not in sys.path:
        sys.path.insert(0, DATA)

    SCHED = build_scheduler()
    SCHED.start()
    log.info("планировщик поднят, задач: %d, пояс %s", len(JOBS), TZ)
    for j in SCHED.get_jobs():
        log.info("  %-16s следующий запуск %s", j.id, j.next_run_time)

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("http слушает :%d", PORT)

    def stop(signum, frame):
        log.info("сигнал %s, останавливаюсь", signum)
        try:
            SCHED.shutdown(wait=False)
        finally:
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
