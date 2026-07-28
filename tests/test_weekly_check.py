"""Тесты для weekly_check.py — watchdog (сторож, ловящий 'бот рапортует
бодро, а данные под ним окаменели' — комментарий в коде про инцидент с
8 июня, тот же класс проблемы, что esc() 27.07, только на уровне данных,
не синтаксиса), и вспомогательные парсеры дат.

CADENCE_MAX_SILENCE / DEAD_STATUSES здесь — ТРЕТЬЯ независимая копия той
же каденции, что в pipeline_sync.due_for_followup и mission_control.is_dead
(комментарий в коде сам это признаёт: "синхронно с mission_control.is_dead()").
Три копии одной константы в трёх файлах — риск расхождения при рефакторинге
любого из них поодиночке; тесты здесь фиксируют текущие значения как
регрессионную страховку, не решают архитектурную проблему.

weekly_check.py на верхнем уровне читает os.environ.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")
os.environ.setdefault("GITHUB_TOKEN", "test")
os.environ.setdefault("GITHUB_REPOSITORY", "test/test")

sys.path.insert(0, "..")
import weekly_check as wc

MSK = wc.MSK


# --- parse_dt / parse_date ---------------------------------------------

def test_parse_dt_valid_iso():
    dt = wc.parse_dt("2026-07-27T10:00:00Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 7 and dt.day == 27


def test_parse_dt_none_for_empty():
    assert wc.parse_dt(None) is None
    assert wc.parse_dt("") is None


def test_parse_dt_none_for_malformed():
    assert wc.parse_dt("not-a-date") is None


def test_parse_date_valid():
    d = wc.parse_date("2026-07-27")
    assert d.year == 2026 and d.month == 7 and d.day == 27


def test_parse_date_none_for_malformed():
    assert wc.parse_date("not-a-date") is None
    assert wc.parse_date(None) is None


# --- watchdog ----------------------------------------------------------
# load_json мокается по пути: возвращает разные фикстуры для pipeline.json
# vs state_inbox.json vs history.json, как это будет в реальном прогоне.

def _mock_load_json(pipeline=None, inbox_state=None, history=None):
    def fake(path, default=None):
        if path == wc.PIPELINE_PATH:
            return pipeline
        if path == wc.INBOX_STATE_PATH:
            return inbox_state
        if path == wc.HISTORY_PATH:
            return history
        return default
    return fake


def test_watchdog_alarms_when_pipeline_missing():
    now = datetime.now(MSK)
    with mock.patch("weekly_check.load_json", side_effect=_mock_load_json(pipeline=None)):
        alarms, facts = wc.watchdog(now)
    assert any("pipeline.json не читается" in a for a in alarms)


def test_watchdog_alarms_when_pipeline_stale():
    now = datetime.now(MSK)
    stale_ts = (now - timedelta(hours=100)).isoformat()
    pipeline = {"last_updated": stale_ts, "leads": []}
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("не обновлялся" in a and "pipeline_sync не бежит" in a for a in alarms)


def test_watchdog_alarms_when_no_new_leads_fossil():
    # Регрессия на инцидент "пайплайн простоял с 8 июня" (комментарий
    # в коде) — главная проверка watchdog, её отсутствие стоило 6 недель.
    now = datetime.now(MSK)
    old_date = (now.date() - timedelta(days=20)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": old_date, "status": "sent_no_reply", "silence_days": 5}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("окаменел" in a for a in alarms)


def test_watchdog_no_fossil_alarm_when_recent_lead_exists():
    now = datetime.now(MSK)
    recent_date = (now.date() - timedelta(days=2)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "status": "sent_no_reply", "silence_days": 2}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert not any("окаменел" in a for a in alarms)


def test_watchdog_alarms_when_zero_live_leads():
    now = datetime.now(MSK)
    recent_date = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "status": "dead", "silence_days": 5}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("живых лидов ноль" in a for a in alarms)


def test_watchdog_alarms_on_cadence_zombies():
    # Каденция исчерпана (silence_days > 21), но статус всё ещё "живой"
    # в файле — не закрыт. watchdog должен это поймать.
    now = datetime.now(MSK)
    recent_date = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "status": "sent_no_reply", "silence_days": 25}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("каденция исчерпана" in a for a in alarms)


def test_watchdog_no_alarms_on_healthy_state():
    now = datetime.now(MSK)
    recent_date = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "last_activity": recent_date,
                   "status": "sent_no_reply", "silence_days": 3, "touches": 1}],
    }
    inbox_state = {"last_run": now.isoformat(), "seen": ["a", "b"]}
    history = {"items": [{"ts": now.isoformat()}]}
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state=inbox_state, history=history)):
        alarms, facts = wc.watchdog(now)
    assert alarms == []
    assert len(facts) > 0


def test_watchdog_alarms_when_inbox_dead():
    now = datetime.now(MSK)
    recent_date = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "status": "sent_no_reply", "silence_days": 1}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state=None, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("inbox.py мёртв" in a for a in alarms)
