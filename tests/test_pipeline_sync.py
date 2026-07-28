"""Тесты для чистой бизнес-логики pipeline_sync.py — каденция follow-up,
матчинг лидов по домену, фильтрация адресатов.

pipeline_sync.py на верхнем уровне читает os.environ — подставляем
переменные до импорта, иначе модуль не загрузится.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("GMAIL_USER", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")

sys.path.insert(0, "..")
import pipeline_sync as ps


# --- due_for_followup ------------------------------------------------------
# Каденция: 4-7 дней тишины, касаний меньше трёх, статус sent_no_reply.

def test_due_for_followup_true_in_window():
    lead = {"status": "sent_no_reply", "silence_days": 5, "touches": 1}
    assert ps.due_for_followup(lead) is True


def test_due_for_followup_false_too_early():
    lead = {"status": "sent_no_reply", "silence_days": 2, "touches": 1}
    assert ps.due_for_followup(lead) is False


def test_due_for_followup_false_too_late_but_within_max_silence():
    # 21 — верхняя граница CADENCE_MAX_SILENCE, включительно
    lead = {"status": "sent_no_reply", "silence_days": 21, "touches": 1}
    assert ps.due_for_followup(lead) is True


def test_due_for_followup_false_beyond_max_silence():
    lead = {"status": "sent_no_reply", "silence_days": 22, "touches": 1}
    assert ps.due_for_followup(lead) is False


def test_due_for_followup_false_wrong_status():
    lead = {"status": "dead", "silence_days": 5, "touches": 1}
    assert ps.due_for_followup(lead) is False


def test_due_for_followup_false_won_status_never_due():
    # Регрессия 28.07: won защищён от каденции — не должен всплывать
    # для follow-up, даже если формально попадает в окно тишины.
    lead = {"status": "won", "silence_days": 5, "touches": 1}
    assert ps.due_for_followup(lead) is False


def test_due_for_followup_false_touches_exhausted():
    lead = {"status": "sent_no_reply", "silence_days": 5, "touches": 3}
    assert ps.due_for_followup(lead) is False


def test_due_for_followup_true_touches_just_under_max():
    lead = {"status": "sent_no_reply", "silence_days": 5, "touches": 2}
    assert ps.due_for_followup(lead) is True


# --- is_trackable_recipient -------------------------------------------------
# Регрессия 21.07: example.com проходил как валидный адресат и засорял
# account_watch поиском по слову "Example" в новостях.

def test_trackable_recipient_normal_address():
    assert ps.is_trackable_recipient("ceo@somecompany.com", "own.com") is True


def test_trackable_recipient_rejects_placeholder_example_com():
    assert ps.is_trackable_recipient("mari@example.com", "own.com") is False


def test_trackable_recipient_rejects_test_domain():
    assert ps.is_trackable_recipient("x@test.com", "own.com") is False


def test_trackable_recipient_rejects_own_domain():
    assert ps.is_trackable_recipient("me@own.com", "own.com") is False


def test_trackable_recipient_rejects_noreply():
    assert ps.is_trackable_recipient("noreply@somecompany.com", "own.com") is False


def test_trackable_recipient_rejects_mailer_daemon():
    assert ps.is_trackable_recipient("mailer-daemon@somecompany.com", "own.com") is False


def test_trackable_recipient_accepts_info_and_priemnaya():
    # ВАЖНО (комментарий в коде, 354): это исходящие, не входящие —
    # info@/приёмная@ — это именно то, куда Антон и пишет outreach.
    # is_auto_notification НЕ должна тут применяться.
    assert ps.is_trackable_recipient("info@somecompany.com", "own.com") is True
    assert ps.is_trackable_recipient("priemnaya@somecompany.ru", "own.com") is True


def test_trackable_recipient_rejects_empty_or_malformed():
    assert ps.is_trackable_recipient("", "own.com") is False
    assert ps.is_trackable_recipient("not-an-email", "own.com") is False
    assert ps.is_trackable_recipient(None, "own.com") is False


# --- find_lead_by_domain -----------------------------------------------------

def test_find_lead_by_domain_exact_to_domain_match():
    pipeline = {"leads": [
        {"id": "a", "to_domain": "seligdar.ru", "topic": "NDA"},
        {"id": "b", "to_domain": "other.com", "topic": "other"},
    ]}
    lead = ps.find_lead_by_domain(pipeline, "mail.seligdar.ru", "seligdar.ru")
    assert lead["id"] == "a"


def test_find_lead_by_domain_no_match_returns_none():
    pipeline = {"leads": [{"id": "a", "to_domain": "seligdar.ru", "topic": "NDA"}]}
    lead = ps.find_lead_by_domain(pipeline, "totally-unrelated.com", "totally-unrelated.com")
    assert lead is None


def test_find_lead_by_domain_matches_via_searchable_content():
    pipeline = {"leads": [
        {"id": "a", "topic": "Talco electrolysis", "notes": "talco.tj domain mentioned"},
    ]}
    lead = ps.find_lead_by_domain(pipeline, "talco.tj", "talco.tj")
    assert lead["id"] == "a"


# --- days_since_last_dispatch / days_since_last_won -------------------------
# Бизнес-алерты 28.07: технический alert_on_failure.yml ловит "бот упал",
# эти две функции ловят "бот работает исправно, но результат — ноль" —
# зелёный прогон и есть проблема, техническому слою тут нечего показать.

def test_days_since_last_dispatch_none_when_empty():
    assert ps.days_since_last_dispatch({"dispatches": {}}) is None


def test_days_since_last_dispatch_computes_days():
    old_date = (datetime.now(timezone.utc) - timedelta(days=25)).strftime("%Y-%m-%d")
    state = {"dispatches": {"a": {"kind": "dispatch", "date": old_date}}}
    assert ps.days_since_last_dispatch(state) == 25


def test_days_since_last_dispatch_ignores_baseline_records():
    # baseline-записи (seed_platform_baseline) не должны считаться реальным
    # диспатчем — иначе алерт никогда не сработает после первого сидирования
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d")
    state = {"dispatches": {
        "a": {"kind": "dispatch", "date": recent},
        "b": {"kind": "baseline", "date": old},
    }}
    assert ps.days_since_last_dispatch(state) == 3


def test_days_since_last_dispatch_takes_most_recent():
    d1 = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    d2 = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    state = {"dispatches": {
        "a": {"kind": "dispatch", "date": d1},
        "b": {"kind": "dispatch", "date": d2},
    }}
    assert ps.days_since_last_dispatch(state) == 2


def test_days_since_last_won_none_when_no_won_leads():
    assert ps.days_since_last_won({"leads": []}) is None
    assert ps.days_since_last_won({"leads": [{"status": "sent_no_reply", "last_activity": "2026-07-01"}]}) is None


def test_days_since_last_won_computes_days():
    old_date = (datetime.now(timezone.utc) - timedelta(days=25)).strftime("%Y-%m-%d")
    pipeline = {"leads": [{"status": "won", "last_activity": old_date}]}
    assert ps.days_since_last_won(pipeline) == 25


def test_days_since_last_won_takes_most_recent_among_multiple_won():
    old = (datetime.now(timezone.utc) - timedelta(days=50)).strftime("%Y-%m-%d")
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    pipeline = {"leads": [
        {"status": "won", "last_activity": old},
        {"status": "won", "last_activity": recent},
    ]}
    assert ps.days_since_last_won(pipeline) == 5


def test_days_since_last_won_none_for_malformed_date():
    pipeline = {"leads": [{"status": "won", "last_activity": "not-a-date"}]}
    assert ps.days_since_last_won(pipeline) is None

