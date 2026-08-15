#!/usr/bin/env python3
"""
Раскладка рабочего каталога на постоянном томе.

Идея, ради которой всё устроено именно так: модули бота вычисляют свои пути
от собственного расположения либо от текущего каталога —

    ROOT = os.path.dirname(os.path.abspath(__file__))
    STATE = os.path.join(ROOT, "state.json")     # digest, filings, ...
    PLATFORMS_PATH = "platforms.json"            # inbox, mission_control, ...

Значит, если положить сами модули на том и запускать их оттуда с cwd на томе,
пути внутри каждого модуля сами собой указывают на /data — и состояние
оказывается на томе без единой правки в пяти тысячах строк бизнес-логики.

Отсюда два разных режима, и путать их нельзя:

  sync_code   — код и конфиги: ОБРАЗ ГЛАВНЕЕ. Каждый деплой перезаписывает,
                иначе правки в репозитории не доедут до бота.
  seed_state  — состояние: ТОМ ГЛАВНЕЕ. Копируется только то, чего на томе ещё
                нет. Иначе каждый деплой откатывал бы бота назад, и он слал бы
                в Telegram новости, которые уже слал.

Граница проведена так, чтобы ошибка была безопасной: конфиг — это код и
явно перечисленные JSON (CONFIG_JSON), а ВСЁ остальное .json считается
состоянием. Если бот однажды заведёт себе новый файл состояния, его никто
не затрёт: правило по умолчанию защищает данные, а не перезаписывает их.
"""
from __future__ import annotations

import json
import logging
import os
import shutil

log = logging.getLogger("seed")

# Код и конфиги — приезжают из репозитория каждый деплой.
CODE_SUFFIXES = (".py", ".txt", ".yml", ".yaml", ".ini", ".cfg")

# JSON, которые редактирует человек в репозитории, а не бот на ходу.
# Всё прочее .json — состояние. Список намеренно короткий и явный.
CONFIG_JSON = {"phrases.json", "platforms.json"}

# Файлы самого сервиса: запускаются из образа, на томе им делать нечего.
SERVICE_FILES = {"main.py", "seed.py", "api.py", "requirements.txt",
                 "runtime.txt", "Procfile", "railway.toml"}

SKIP_DIRS = {".git", ".github", "__pycache__", ".venv", "node_modules",
             "tests", "cloudflare-worker"}

# Разумные пустые значения, если файла нет ни на томе, ни в образе.
DEFAULTS = {
    "state.json": {"seen": []},
    "history.json": {"items": []},
    "filings_history.json": {"items": []},
    "pipeline.json": {"schema_version": 1, "leads": []},
}


def _is_code(name: str) -> bool:
    return name.endswith(CODE_SUFFIXES) or name in CONFIG_JSON


def _is_state(name: str) -> bool:
    return name.endswith(".json") and name not in CONFIG_JSON


def sync_code(app: str, data: str) -> None:
    """Код и конфиги из образа на том. Образ главнее: перезаписываем."""
    copied = 0
    shipped: set[str] = set()

    for name in sorted(os.listdir(app)):
        if name in SERVICE_FILES or name in SKIP_DIRS or name.startswith("."):
            continue
        src = os.path.join(app, name)
        if not os.path.isfile(src) or not _is_code(name):
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
    # Трогаем только .py — состояние здесь ни при чём.
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

    # Всё, что бот считает своим состоянием, — по факту наличия в образе.
    names = {n for n in os.listdir(app)
             if os.path.isfile(os.path.join(app, n)) and _is_state(n)}
    names |= set(DEFAULTS)

    for name in sorted(names):
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

    # Главная диагностика деплоя. Нули после первого запуска означают, что
    # состояние не переехало и бот вот-вот пришлёт полторы тысячи старых
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
