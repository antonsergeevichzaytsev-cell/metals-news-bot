"""Тесты для evening_digest.py — снимок/diff лидов за день, живость ботов.

evening_digest.py — единственный агрегатор, читающий состояние ВСЕХ
остальных ботов сразу (см. Штаб §8). bot_liveness явно документирует,
что формат last_run не унифицирован между ботами (dict у filings, голая
строка у остальных, pipeline_sync вообще не пишет last_run) — это
задокументированная асимметрия, не баг; тесты фиксируют все три ветки.

evening_digest.py на верхнем уровне читает os.environ.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")

sys.path.insert(0, "..")
import evening_digest as ed


# --- snapshot_leads ----------------------------------------------------

def test_snapshot_leads_extracts_minimal_fields():
    pipeline = {"leads": [
        {"id": "a", "status": "sent_no_reply", "touches": 2, "last_activity": "2026-07-20", "topic": "ignored"},
    ]}
    snap = ed.snapshot_leads(pipeline)
    assert snap == {"a": {"status": "sent_no_reply", "touches": 2, "last_activity": "2026-07-20"}}


def test_snapshot_leads_empty_pipeline():
    assert ed.snapshot_leads({"leads": []}) == {}


# --- diff_leads ----------------------------------------------------------

def test_diff_leads_detects_new_lead():
    before = {}
    after = {"a": {"status": "sent_no_reply", "touches": 1, "last_activity": "2026-07-27"}}
    pipeline = {"leads": [{"id": "a", "topic": "New outreach"}]}
    diff = ed.diff_leads(before, after, pipeline)
    assert len(diff["new_leads"]) == 1
    assert diff["new_leads"][0]["id"] == "a"


def test_diff_leads_detects_new_reply():
    before = {"a": {"status": "sent_no_reply", "touches": 1, "last_activity": "2026-07-20"}}
    after = {"a": {"status": "reply_received", "touches": 1, "last_activity": "2026-07-27"}}
    pipeline = {"leads": [{"id": "a", "topic": "Some deal"}]}
    diff = ed.diff_leads(before, after, pipeline)
    assert len(diff["new_replies"]) == 1
    assert diff["new_replies"][0]["id"] == "a"


def test_diff_leads_detects_newly_dead():
    before = {"a": {"status": "sent_no_reply", "touches": 3, "last_activity": "2026-07-01"}}
    after = {"a": {"status": "dead", "touches": 3, "last_activity": "2026-07-01"}}
    pipeline = {"leads": [{"id": "a", "topic": "Cold lead"}]}
    diff = ed.diff_leads(before, after, pipeline)
    assert len(diff["newly_dead"]) == 1
    assert diff["newly_dead"][0]["status"] == "dead"


def test_diff_leads_detects_touched_again():
    before = {"a": {"status": "sent_no_reply", "touches": 1, "last_activity": "2026-07-20"}}
    after = {"a": {"status": "sent_no_reply", "touches": 2, "last_activity": "2026-07-27"}}
    pipeline = {"leads": [{"id": "a", "topic": "Follow-up sent"}]}
    diff = ed.diff_leads(before, after, pipeline)
    assert len(diff["touched_again"]) == 1


def test_diff_leads_no_touched_again_if_now_dead():
    # touches вырос, но статус уже мёртв — не считаем это активным касанием
    before = {"a": {"status": "sent_no_reply", "touches": 1, "last_activity": "2026-07-20"}}
    after = {"a": {"status": "dead", "touches": 2, "last_activity": "2026-07-20"}}
    pipeline = {"leads": [{"id": "a", "topic": "Dead now"}]}
    diff = ed.diff_leads(before, after, pipeline)
    assert diff["touched_again"] == []
    assert len(diff["newly_dead"]) == 1


def test_diff_leads_no_false_positives_on_unchanged():
    before = {"a": {"status": "sent_no_reply", "touches": 1, "last_activity": "2026-07-20"}}
    after = {"a": {"status": "sent_no_reply", "touches": 1, "last_activity": "2026-07-20"}}
    pipeline = {"leads": [{"id": "a", "topic": "Unchanged"}]}
    diff = ed.diff_leads(before, after, pipeline)
    assert diff == {"new_leads": [], "new_replies": [], "newly_dead": [], "touched_again": []}


def test_diff_leads_reply_received_to_reply_received_not_double_counted():
    # Уже был reply_received, остался reply_received — не новый ответ
    before = {"a": {"status": "reply_received", "touches": 1, "last_activity": "2026-07-20"}}
    after = {"a": {"status": "reply_received", "touches": 1, "last_activity": "2026-07-25"}}
    pipeline = {"leads": [{"id": "a", "topic": "Old reply"}]}
    diff = ed.diff_leads(before, after, pipeline)
    assert diff["new_replies"] == []


# --- bot_liveness --------------------------------------------------------
# Формат last_run асимметричен между ботами (задокументировано в коде) —
# тестируем все три ветки: dict-формат (filings), голая строка (остальные),
# особый случай pipeline_sync (нет своего last_run, берём из pipeline.json).

def test_bot_liveness_pipeline_sync_from_pipeline_last_updated():
    now = datetime.now(timezone.utc)
    pipeline = {"last_updated": now.isoformat()}
    with mock.patch("evening_digest.load_json", return_value=None):
        result = ed.bot_liveness(now, pipeline)
    assert result["pipeline_sync"] == "ok"


def test_bot_liveness_dict_format_ts_field():
    # filings.py пишет last_run как {"ts": ..., "raw": ...}
    now = datetime.now(timezone.utc)
    fresh_ts = now.isoformat()

    def fake_load(path, default=None):
        if "state_filings" in path:
            return {"last_run": {"ts": fresh_ts, "raw": 5}}
        return None

    with mock.patch("evening_digest.load_json", side_effect=fake_load):
        result = ed.bot_liveness(now, {"last_updated": now.isoformat()})
    assert result["filings"] == "ok"


def test_bot_liveness_plain_string_format():
    # inbox.py / account_watch.py пишут last_run как голую ISO-строку
    now = datetime.now(timezone.utc)
    fresh_ts = now.isoformat()

    def fake_load(path, default=None):
        if "state_inbox" in path:
            return {"last_run": fresh_ts}
        return None

    with mock.patch("evening_digest.load_json", side_effect=fake_load):
        result = ed.bot_liveness(now, {"last_updated": now.isoformat()})
    assert result["inbox"] == "ok"


def test_bot_liveness_reports_stale():
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(hours=48)).isoformat()

    def fake_load(path, default=None):
        if "state_inbox" in path:
            return {"last_run": old_ts}
        return None

    with mock.patch("evening_digest.load_json", side_effect=fake_load):
        result = ed.bot_liveness(now, {"last_updated": now.isoformat()})
    assert "молчит" in result["inbox"]


def test_bot_liveness_reports_missing_state_file():
    now = datetime.now(timezone.utc)
    with mock.patch("evening_digest.load_json", return_value=None):
        result = ed.bot_liveness(now, {"last_updated": now.isoformat()})
    assert result["filings"] == "нет state-файла"


def test_bot_liveness_reports_missing_last_run_field():
    now = datetime.now(timezone.utc)

    def fake_load(path, default=None):
        if "state_inbox" in path:
            return {}  # файл есть, но last_run отсутствует
        return None

    with mock.patch("evening_digest.load_json", side_effect=fake_load):
        result = ed.bot_liveness(now, {"last_updated": now.isoformat()})
    assert result["inbox"] == "нет last_run"
