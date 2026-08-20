"""Тесты для чистой логики mission_control.py — определение мёртвости лида
(дублирует каденцию из pipeline_sync, важно не разойтись), анализ
пайплайна, проверка свежести коммита, sanity-check цен металлов.

mission_control.py на верхнем уровне читает os.environ — подставляем
переменные до импорта.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")
os.environ.setdefault("GMAIL_USER", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

sys.path.insert(0, "..")
import mission_control as mc


# --- is_dead ---------------------------------------------------------------
# ВАЖНО: эта логика ДУБЛИРУЕТ каденцию из pipeline_sync.due_for_followup
# (комментарий в коде §29 инструкции: "единственный источник правды —
# каденция, дублируется в mission_control.is_dead()"). Разойдётся —
# mission_control покажет живым то, что pipeline_sync уже считает мёртвым,
# или наоборот. Тесты здесь — страховка от такого расхождения при рефакторинге.

def test_is_dead_true_for_explicit_dead_status():
    assert mc.is_dead({"status": "dead"}) is True
    assert mc.is_dead({"status": "declined"}) is True
    assert mc.is_dead({"status": "channel_failed"}) is True


def test_is_dead_false_for_reply_received_regardless_of_silence():
    # "Полученный ответ не умирает никогда — он и есть деньги" (комментарий)
    lead = {"status": "reply_received", "silence_days": 100}
    assert mc.is_dead(lead) is False


def test_is_dead_true_when_cadence_exhausted():
    lead = {"status": "sent_no_reply", "silence_days": 22}
    assert mc.is_dead(lead) is True


def test_is_dead_false_within_cadence_window():
    lead = {"status": "sent_no_reply", "silence_days": 10}
    assert mc.is_dead(lead) is False


def test_is_dead_false_for_won_status():
    # 28.07: won защищён от каденции в pipeline_sync (process_sent не
    # перезаписывает won). Здесь won не входит в DEAD_STATUSES и не
    # попадает в ветку sent_no_reply/follow_up_overdue -> is_dead
    # возвращает False по умолчанию (won = жив, что семантически верно:
    # выигранная сделка не мертва). Регрессия на будущее: если кто-то
    # добавит won в DEAD_STATUSES по ошибке, этот тест поймает.
    lead = {"status": "won", "silence_days": 100}
    assert mc.is_dead(lead) is False


def test_is_dead_false_for_unknown_status():
    lead = {"status": "some_new_status_not_yet_handled", "silence_days": 50}
    assert mc.is_dead(lead) is False


# --- pipeline_staleness_hours ------------------------------------------------

def test_pipeline_staleness_none_when_missing():
    assert mc.pipeline_staleness_hours({}) is None


def test_pipeline_staleness_none_when_malformed():
    assert mc.pipeline_staleness_hours({"last_updated": "not-a-date"}) is None


def test_pipeline_staleness_computes_hours():
    ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
    hours = mc.pipeline_staleness_hours({"last_updated": ts})
    assert hours is not None
    assert 4.9 <= hours <= 5.1


# --- analyze_pipeline --------------------------------------------------------

def test_analyze_pipeline_counts_live_and_dead():
    pipeline = {"leads": [
        {"status": "dead", "silence_days": 5},
        {"status": "sent_no_reply", "silence_days": 5},
        {"status": "reply_received", "silence_days": 2},
    ]}
    result = mc.analyze_pipeline(pipeline)
    assert result["total_leads"] == 3
    assert result["live_leads"] == 2
    assert result["dead_leads"] == 1


def test_analyze_pipeline_won_lead_counts_as_live():
    pipeline = {"leads": [
        {"status": "won", "silence_days": 30, "type": "partnership"},
    ]}
    result = mc.analyze_pipeline(pipeline)
    assert result["live_leads"] == 1
    assert result["dead_leads"] == 0


def test_analyze_pipeline_stale_reply_flagged_separately():
    pipeline = {"leads": [
        {"status": "reply_received", "silence_days": 10},  # старый ответ, не тронут
        {"status": "reply_received", "silence_days": 1},   # свежий
    ]}
    result = mc.analyze_pipeline(pipeline)
    assert len(result["new_replies"]) == 2
    assert len(result["stale_replies"]) == 1
    assert result["stale_replies"][0]["silence_days"] == 10


def test_analyze_pipeline_overdue_followup_excludes_reply_received():
    pipeline = {"leads": [
        {"status": "sent_no_reply", "silence_days": 8, "type": "partnership"},
        {"status": "reply_received", "silence_days": 8, "type": "partnership"},
    ]}
    result = mc.analyze_pipeline(pipeline)
    # reply_received не должен дублироваться в overdue_followup —
    # он уже поднят как stale_reply (комментарий в коде)
    assert len(result["overdue_followup"]) == 1
    assert result["overdue_followup"][0]["status"] == "sent_no_reply"


def test_analyze_pipeline_empty():
    result = mc.analyze_pipeline({"leads": []})
    assert result["total_leads"] == 0
    assert result["live_leads"] == 0


# 19.08.2026: тесты is_plausible_price/fetch_prices/fetch_yahoo/fetch_stooq
# переехали в test_prices.py вместе с самими функциями — mission_control.py
# теперь использует их через prices.py (import prices as pr), больше не
# определяет эту логику сам.
