#!/usr/bin/env python3
"""
Read-only API поверх состояния бота.

Зачем: бот и без того каждый день собирает и нормализует то, что иначе
пришлось бы собирать заново — ленты, filings, статусы сделок, историю
касаний. Пока эти данные лежали JSON-файлами в репозитории, добраться до
них снаружи было нечем, и работа дублировалась. Теперь бот отдаёт их как
источник, а суждение и действия строятся поверх.

БЕЗОПАСНОСТЬ. Здесь наружу смотрит pipeline.json: названия компаний,
адреса контактов, суммы, сроки молчания. Ровно те данные, которые
13.08.2026 лежали в публичном репозитории. Поэтому:

  * без API_TOKEN в окружении ручки не работают вовсе — 503, а не
    открытый доступ. Отказ в закрытую сторону, не в открытую;
  * токен сверяется побайтово через hmac.compare_digest;
  * отдаётся только явный белый список документов. Пути не склеиваются
    из пользовательского ввода — обход каталога невозможен по устройству.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("api")

# Что вообще разрешено отдавать. Всё, чего тут нет, недоступно.
READABLE = {
    "pipeline": "pipeline.json",
    "history": "history.json",
    "filings": "filings_history.json",
    "last_digest": "state_last_digest_sent.json",
    "account_watch": "state_account_watch.json",
    "inbox": "state_inbox.json",
}


def _load(data_dir: str, fname: str):
    path = os.path.join(data_dir, fname)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("api: %s не читается", fname)
        return None


def check_token(header_value: str | None) -> bool:
    """Пусто в API_TOKEN — доступа нет ни у кого. Фейл в закрытую сторону."""
    expected = os.environ.get("API_TOKEN", "")
    if not expected:
        return False
    got = (header_value or "").removeprefix("Bearer ").strip()
    if not got:
        return False
    return hmac.compare_digest(got, expected)


def _iso_days_ago(value) -> int | None:
    """Сколько дней прошло от ISO-даты. None, если распарсить не вышло."""
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def pipeline_summary(data_dir: str) -> dict:
    """
    Сжатая картина пайплайна: то, ради чего иначе пришлось бы тянуть
    весь файл и разбирать его на месте.

    Раскладка по температуре повторяет правило, которым Антон уже
    пользуется: тишина больше 14 дней — остывает, больше 30 — мёртвое.
    """
    raw = _load(data_dir, "pipeline.json")
    if raw is None:
        return {"available": False, "reason": "pipeline.json отсутствует"}

    leads = raw.get("leads") if isinstance(raw, dict) else raw
    if not isinstance(leads, list):
        return {"available": False, "reason": "неожиданная структура pipeline.json"}

    hot, cooling, dead, open_move = [], [], [], []
    for ld in leads:
        if not isinstance(ld, dict):
            continue
        status = (ld.get("status") or "").lower()
        if status in ("closed", "lost", "won", "archived"):
            continue

        silence = ld.get("silence_days")
        if silence is None:
            silence = _iso_days_ago(ld.get("last_activity"))

        item = {
            "id": ld.get("id"),
            "company": ld.get("company_name"),
            "status": ld.get("status"),
            "silence_days": silence,
            "touches": ld.get("touches"),
            "next_action": ld.get("next_action"),
            "value_usd": ld.get("value_usd"),
            "topic": ld.get("topic"),
        }

        if not ld.get("touches"):
            open_move.append(item)
        elif silence is None:
            hot.append(item)
        elif silence > 30:
            dead.append(item)
        elif silence > 14:
            cooling.append(item)
        else:
            hot.append(item)

    key = lambda x: (x.get("silence_days") is None, -(x.get("silence_days") or 0))
    return {
        "available": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "hot": len(hot), "cooling": len(cooling),
            "dead": len(dead), "not_started": len(open_move),
        },
        "cooling": sorted(cooling, key=key),
        "not_started": open_move,
        "dead": sorted(dead, key=key),
        "hot": hot,
    }


def filings_recent(data_dir: str, days: int = 7) -> dict:
    """Свежие сигналы из filings — вход для Brief и для outreach."""
    raw = _load(data_dir, "filings_history.json")
    if raw is None:
        return {"available": False, "reason": "filings_history.json отсутствует"}

    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return {"available": False, "reason": "неожиданная структура filings_history.json"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        age = _iso_days_ago(it.get("ts") or it.get("date"))
        if age is not None and age > days:
            continue
        out.append(it)

    return {
        "available": True,
        "window_days": days,
        "count": len(out),
        "items": out[-200:],
    }


def index() -> dict:
    return {
        "documents": sorted(READABLE.keys()),
        "views": ["pipeline/summary", "filings/recent?days=N"],
        "auth": "заголовок Authorization: Bearer <API_TOKEN>",
    }


def handle(path: str, data_dir: str) -> tuple[int, dict]:
    """Роутер. Возвращает (http-код, тело)."""
    p = path.strip("/")

    if p in ("state", "state/"):
        return 200, index()

    if p == "pipeline/summary":
        return 200, pipeline_summary(data_dir)

    if p.startswith("filings/recent"):
        days = 7
        if "?" in path:
            q = path.split("?", 1)[1]
            for part in q.split("&"):
                if part.startswith("days="):
                    try:
                        days = max(1, min(90, int(part[5:])))
                    except ValueError:
                        pass
        return 200, filings_recent(data_dir, days)

    if p.startswith("state/"):
        name = p[len("state/"):].split("?")[0]
        fname = READABLE.get(name)
        if not fname:
            return 404, {"error": "неизвестный документ", "available": sorted(READABLE)}
        doc = _load(data_dir, fname)
        if doc is None:
            return 404, {"error": "файл отсутствует на томе", "document": name}
        return 200, {"document": name, "data": doc}

    return 404, {"error": "нет такой ручки"}
