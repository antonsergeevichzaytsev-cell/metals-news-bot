#!/usr/bin/env python3
"""
Раскладка рабочего каталога на постоянном томе.

Идея, ради которой всё это устроено именно так: модули бота вычисляют свои
пути от собственного расположения —

    ROOT = os.path.dirname(os.path.abspath(__file__))
    STATE = os.path.join(ROOT, "state.json")

Значит, если положить сами модули на том и запускать их оттуда, ROOT внутри
каждого модуля сам собой окажется равен /data, и состояние окажется на томе
без единой правки в пяти тысячах строк бизнес-логики. Это и делается здесь.

Отсюда два разных действия, и путать их нельзя:

  sync_code   — код и конфиги: образ ГЛАВНЕЕ тома. Каждый деплой перезаписывает,
                иначе правки в репозитории не доедут до бота.
  seed_state  — состояние: том ГЛАВНЕЕ образа. Копируется только то, чего на
                томе ещё нет. Иначе каждый деплой откатывал бы бота назад, и он
                слал бы в Telegram новости, которые уже слал.

Список STATE_FILES — единственная граница между этими двумя режимами.
"""
from __future__ import annotations

import json
import logging
import os
import shutil

log = logging.getLogger("seed")

# Состояние. Живёт на томе, образом не перезаписывается никогда.
STATE_FILES = [
    "state.json",
    "history.json",
    "state_last_digest_sent.json",
    "pipeline.json",
    "account_overrides.json",
    "state_account_watch.json",
    "filings_history.json",
    "state_filings.json",
    "state_evening_digest.json",
    "state_inbox.json",
    "state_linkedin_ideas.json",
    "state_pipeline_sync.json",
    "anton_state.json",
    "secrets_rotation.json",
]

# Разумные пустые значения, если файла нет ни на томе, ни в образе.
DEFAULTS = {
    "history.json": {"items": []},
    "filings_history.json": {"items": []},
    "pipeline.json": {"schema_version": 1, "leads": []},
}

# Что вообще относим к коду и конфигам. Всё прочее (архивы, картинки, README)
# на том не едет — там ему делать нечего.
CODE_SUFFIXES = (".py", ".txt", ".json", ".yml", ".yaml", ".ini", ".cfg")

# Служебное: сюда не заглядываем.
SKIP_DIRS = {".git", ".github", "__pycache__", ".venv", "node_modules"}

# Файлы самого сервиса. На томе они не нужны — сервис запускается из образа,
# а лишний main.py рядом с модулями только путал бы.
SERVICE_FILES = {"main.py", "seed.py", "api.py", "requirements.txt",
                 "runtime.txt", "Procfile", "railway.toml"}


def sync_code(app: str, data: str) -> None:
    """
    Разложить код и конфиги из образа на том. Образ главнее: перезаписываем.

    Копируем только когда файл реально изменился — так у неизменившихся
    модулей сохраняется mtime, и .pyc-кэш переживает деплой.
    """
    copied = 0
    shipped: set[str] = set()

    for name in sorted(os.listdir(app)):
        if name in SERVICE_FILES or name in STATE_FILES:
            continue
        if name in SKIP_DIRS or name.startswith("."):
            continue
        src = os.path.join(app, name)
        if not os.path.isfile(src) or not name.endswith(CODE_SUFFIXES):
            continue

        shipped.add(name)
        dst = os.path.join(data, name)
        if os.path.exists(dst):
            same = (os.path.getsize(src) == os.path.getsize(dst)
                    and open(src, "rb").read() == open(dst, "rb").read())
            if same:
                continue
        shutil.copy2(src, dst)
        copied += 1

    # Модуль, удалённый из репозитория, не должен остаться жить на томе:
    # планировщик его не позовёт, но при импорте он может перебить актуальный.
    # Трогаем только .py и только то, что когда-то приехало из образа.
    orphans = []
    for name in sorted(os.listdir(data)):
        if not name.endswith(".py") or name in shipped or name in SERVICE_FILES:
            continue
        os.remove(os.path.join(data, name))
        orphans.append(name)

    log.info("код: на томе %d файлов, обновлено %d", len(shipped), copied)
    if orphans:
        log.info("код: удалены устаревшие — %s", ", ".join(orphans))
    if not shipped:
        log.error("код: из образа не приехало НИ ОДНОГО модуля — проверьте сборку")


def seed_state(app: str, data: str) -> None:
    """Состояние: том главнее образа. Копируем только недостающее."""
    copied, kept, created = [], [], []

    for name in STATE_FILES:
        dst = os.path.join(data, name)
        src = os.path.join(app, name)

        if os.path.exists(dst):
            kept.append(name)
            continue
        if os.path.exists(src):
            shutil.copy2(src, dst)
            copied.append(name)
            continue
        if name in DEFAULTS:
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS[name], f, ensure_ascii=False, indent=2)
            created.append(name)

    if copied:
        log.info("seed: перенесено на том впервые — %s", ", ".join(copied))
    if created:
        log.info("seed: создано пустым — %s", ", ".join(created))
    if kept:
        log.info("seed: уже на томе, не трогаю — %d файлов", len(kept))

    # Главная диагностика деплоя. Если тут нули после первого запуска —
    # состояние не переехало, и бот вот-вот пришлёт полторы тысячи старых
    # новостей. Строка сделана заметной намеренно.
    for name, key in (("state.json", "seen"), ("history.json", "items")):
        p = os.path.join(data, name)
        if not os.path.exists(p):
            log.warning("seed: %s на томе нет", name)
            continue
        try:
            with open(p, encoding="utf-8") as f:
                obj = json.load(f)
            val = obj.get(key) if isinstance(obj, dict) else None
            log.info("seed: %s -> %s = %d", name, key,
                     len(val) if val is not None else 0)
        except Exception:
            log.warning("seed: %s не читается как JSON", name)


def run(app: str, data: str) -> None:
    if os.path.abspath(app) == os.path.abspath(data):
        log.info("DATA_DIR совпадает с каталогом кода — локальный режим, раскладка пропущена")
        return
    os.makedirs(data, exist_ok=True)
    sync_code(app, data)
    seed_state(app, data)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    a = os.path.dirname(os.path.abspath(__file__))
    run(a, os.environ.get("DATA_DIR", a))
