"""Тесты для linkedin_ideas.py — сбор кандидатов на пост из двух
источников (размеченные filings-хуки, digest-новости), с фильтрацией
по окну времени и приоритету.

linkedin_ideas.py на верхнем уровне читает os.environ.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

sys.path.insert(0, "..")
import linkedin_ideas as li
import pytest


@pytest.fixture(autouse=True)
def _no_network_prices():
    """19.08.2026: gather_candidates() теперь вызывает
    gather_price_context_candidates(), которая дёргает pr.fetch_prices()
    (реальная сеть — Yahoo/Stooq). Без этого мока каждый тест в этом
    файле реально бил бы в сеть (медленно, нестабильно, зависит от
    внешнего сервиса). Дефолт — пустой словарь (цены недоступны),
    отдельные тесты источника price_context переопределяют вручную."""
    with mock.patch("linkedin_ideas.pr.fetch_prices", return_value={}):
        yield


def _mock_load_json(filings_history=None, digest_history=None):
    def fake(path, default=None):
        if path == li.FILINGS_HISTORY_PATH:
            return filings_history if filings_history is not None else default
        if path == li.DIGEST_HISTORY_PATH:
            return digest_history if digest_history is not None else default
        return default
    return fake


def test_gather_candidates_includes_good_label_within_window():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    filings = {"labels": [
        {"label": "good", "ts": fresh_ts, "topic": "Seligdar CapEx overrun", "note": "hook here", "url": "x"},
    ]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history=filings, digest_history={})):
        candidates = li.gather_candidates()
    assert len(candidates) == 1
    assert candidates[0]["source"] == "filings"
    assert candidates[0]["title"] == "Seligdar CapEx overrun"


def test_gather_candidates_excludes_bad_label():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    filings = {"labels": [
        {"label": "bad", "ts": fresh_ts, "topic": "Irrelevant news"},
    ]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history=filings, digest_history={})):
        candidates = li.gather_candidates()
    assert candidates == []


def test_gather_candidates_excludes_note_label():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    filings = {"labels": [
        {"label": "note", "ts": fresh_ts, "topic": "Unclear feedback"},
    ]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history=filings, digest_history={})):
        candidates = li.gather_candidates()
    assert candidates == []


def test_gather_candidates_excludes_stale_label_outside_window():
    # LABEL_WINDOW_DAYS = 3 — метка старше этого не годится
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    filings = {"labels": [
        {"label": "good", "ts": old_ts, "topic": "Old hook"},
    ]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history=filings, digest_history={})):
        candidates = li.gather_candidates()
    assert candidates == []


def test_gather_candidates_includes_digest_high_priority():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    digest = {"items": [
        {"ts": fresh_ts, "priority": "high", "title": "Rusal expands capacity", "why": "matters", "link": "x"},
    ]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history={}, digest_history=digest)):
        candidates = li.gather_candidates()
    assert len(candidates) == 1
    assert candidates[0]["source"] == "digest"


def test_gather_candidates_includes_digest_medium_priority():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    digest = {"items": [
        {"ts": fresh_ts, "priority": "medium", "title": "Copper price move", "why": "context", "link": "x"},
    ]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history={}, digest_history=digest)):
        candidates = li.gather_candidates()
    assert len(candidates) == 1


def test_gather_candidates_excludes_digest_low_priority():
    # "low-priority рыночный шум не годится для поста" (комментарий в коде)
    fresh_ts = datetime.now(timezone.utc).isoformat()
    digest = {"items": [
        {"ts": fresh_ts, "priority": "low", "title": "Generic market commentary", "why": "meh", "link": "x"},
    ]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history={}, digest_history=digest)):
        candidates = li.gather_candidates()
    assert candidates == []


def test_gather_candidates_excludes_stale_digest_item():
    # DIGEST_WINDOW_HOURS = 30 — новость старше этого не годится
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    digest = {"items": [
        {"ts": old_ts, "priority": "high", "title": "Old news", "why": "stale", "link": "x"},
    ]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history={}, digest_history=digest)):
        candidates = li.gather_candidates()
    assert candidates == []


def test_gather_candidates_excludes_malformed_timestamp():
    digest = {"items": [
        {"ts": "not-a-valid-date", "priority": "high", "title": "Broken ts", "why": "x", "link": "x"},
    ]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history={}, digest_history=digest)):
        candidates = li.gather_candidates()
    assert candidates == []


def test_gather_candidates_combines_both_sources():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    filings = {"labels": [{"label": "good", "ts": fresh_ts, "topic": "Filings hook"}]}
    digest = {"items": [{"ts": fresh_ts, "priority": "high", "title": "Digest news", "why": "x", "link": "x"}]}
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history=filings, digest_history=digest)):
        candidates = li.gather_candidates()
    assert len(candidates) == 2
    sources = {c["source"] for c in candidates}
    assert sources == {"filings", "digest"}


def test_gather_candidates_empty_sources_returns_empty():
    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history={}, digest_history={})):
        candidates = li.gather_candidates()
    assert candidates == []


# --- gather_facts_shifts_candidates (источник 3) ----------------------------
# 19.08.2026: filings-записи со сдвигом во времени (facts.compare_facts
# уже посчитала в filings.py, здесь читаем готовое поле 'shifts') — не
# требует ручной метки '+', сам факт сдвига уже сигнал.

def test_facts_shifts_includes_item_with_shifts_within_window():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    fh = {"items": [{
        "ts": fresh_ts, "company": "Asante Gold", "project": "Bibiani",
        "shifts": ["срок 2023 → 2026 (с 08-07)"], "why": "задержка проекта",
        "link": "https://example.com",
    }]}
    candidates = li.gather_facts_shifts_candidates(fh)
    assert len(candidates) == 1
    assert candidates[0]["source"] == "facts_shift"
    assert "Asante Gold" in candidates[0]["title"]
    assert "срок 2023 → 2026" in candidates[0]["title"]


def test_facts_shifts_excludes_item_without_shifts():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    fh = {"items": [{"ts": fresh_ts, "company": "X", "facts": {"horizon": ["2026"]}}]}
    candidates = li.gather_facts_shifts_candidates(fh)
    assert candidates == []


def test_facts_shifts_excludes_stale_item_outside_window():
    old_ts = (datetime.now(timezone.utc) - timedelta(days=li.LABEL_WINDOW_DAYS + 1)).isoformat()
    fh = {"items": [{"ts": old_ts, "company": "X", "shifts": ["сумма 10 → 20 млн $ (с 08-01)"]}]}
    candidates = li.gather_facts_shifts_candidates(fh)
    assert candidates == []


def test_facts_shifts_empty_history_returns_empty():
    assert li.gather_facts_shifts_candidates({}) == []


# --- gather_company_cluster_candidates (источник 4) -------------------------
# Та же логика, что cmd_synthesis, но срабатывает сама при сборе
# кандидатов, не по ручной команде.

def test_company_cluster_two_mentions_same_company_forms_candidate():
    fresh = datetime.now(timezone.utc).isoformat()
    items = [
        {"ts": fresh, "company": "Alcoa", "priority": "high", "title": "Alcoa news 1", "why": "a"},
        {"ts": fresh, "company": "Alcoa", "priority": "medium", "title": "Alcoa news 2", "why": "b"},
    ]
    candidates = li.gather_company_cluster_candidates(items)
    assert len(candidates) == 1
    assert candidates[0]["source"] == "company_cluster"
    assert "Alcoa" in candidates[0]["title"]
    assert "2 упоминания" in candidates[0]["title"]


def test_company_cluster_single_mention_not_enough():
    fresh = datetime.now(timezone.utc).isoformat()
    items = [{"ts": fresh, "company": "Alcoa", "priority": "high", "title": "x", "why": "a"}]
    assert li.gather_company_cluster_candidates(items) == []


def test_company_cluster_case_insensitive_grouping():
    fresh = datetime.now(timezone.utc).isoformat()
    items = [
        {"ts": fresh, "company": "Alcoa", "priority": "high", "title": "x", "why": "a"},
        {"ts": fresh, "company": "ALCOA", "priority": "high", "title": "y", "why": "b"},
    ]
    candidates = li.gather_company_cluster_candidates(items)
    assert len(candidates) == 1


def test_company_cluster_ignores_low_priority():
    fresh = datetime.now(timezone.utc).isoformat()
    items = [
        {"ts": fresh, "company": "Alcoa", "priority": "low", "title": "x", "why": "a"},
        {"ts": fresh, "company": "Alcoa", "priority": "low", "title": "y", "why": "b"},
    ]
    assert li.gather_company_cluster_candidates(items) == []


def test_company_cluster_excludes_stale_items_outside_window():
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    items = [
        {"ts": old, "company": "Alcoa", "priority": "high", "title": "x", "why": "a"},
        {"ts": old, "company": "Alcoa", "priority": "high", "title": "y", "why": "b"},
    ]
    assert li.gather_company_cluster_candidates(items, window_days=7) == []


def test_company_cluster_no_company_field_skipped():
    fresh = datetime.now(timezone.utc).isoformat()
    items = [{"ts": fresh, "priority": "high", "title": "x"}] * 3
    assert li.gather_company_cluster_candidates(items) == []


# --- gather_price_context_candidates (источник 5) ---------------------------
# Цена сдвинулась заметно + есть связанная новость того же металла =
# содержательнее голой новости или голой цены по отдельности.

def test_price_context_significant_move_with_related_news():
    fresh = datetime.now(timezone.utc).isoformat()
    items = [{"ts": fresh, "title": "Major copper smelter halts production", "desc": "", "why": "supply shock", "link": "x"}]
    with mock.patch("linkedin_ideas.pr.fetch_prices", return_value={"Cu": (9950.0, 3.5, "CME")}):
        candidates = li.gather_price_context_candidates(items)
    assert len(candidates) == 1
    assert candidates[0]["source"] == "price_context"
    assert "выросла" in candidates[0]["title"]
    assert "3.5%" in candidates[0]["title"]


def test_price_context_negative_move_uses_fell_wording():
    fresh = datetime.now(timezone.utc).isoformat()
    items = [{"ts": fresh, "title": "Aluminium demand drops sharply", "desc": "", "why": "x", "link": "x"}]
    with mock.patch("linkedin_ideas.pr.fetch_prices", return_value={"Al": (2200.0, -4.0, "CME")}):
        candidates = li.gather_price_context_candidates(items)
    assert len(candidates) == 1
    assert "упала" in candidates[0]["title"]


def test_price_context_small_move_ignored():
    fresh = datetime.now(timezone.utc).isoformat()
    items = [{"ts": fresh, "title": "Copper news", "desc": "", "why": "x", "link": "x"}]
    with mock.patch("linkedin_ideas.pr.fetch_prices", return_value={"Cu": (9950.0, 0.5, "CME")}):
        candidates = li.gather_price_context_candidates(items, move_threshold_pct=2.0)
    assert candidates == []


def test_price_context_no_related_news_no_candidate():
    """Заметное движение цены есть, но ни одна digest-заметка не
    упоминает этот металл — голая цифра без новости не пост."""
    fresh = datetime.now(timezone.utc).isoformat()
    items = [{"ts": fresh, "title": "Gold mine expansion in Nevada", "desc": "", "why": "x", "link": "x"}]
    with mock.patch("linkedin_ideas.pr.fetch_prices", return_value={"Cu": (9950.0, 5.0, "CME")}):
        candidates = li.gather_price_context_candidates(items)
    assert candidates == []


def test_price_context_fetch_failure_returns_empty_not_raises():
    with mock.patch("linkedin_ideas.pr.fetch_prices", side_effect=OSError("network down")):
        candidates = li.gather_price_context_candidates([])
    assert candidates == []


def test_price_context_no_change_data_ignored():
    """chg=None (Stooq без previous close) -> нельзя оценить движение,
    не считается значимым сдвигом."""
    fresh = datetime.now(timezone.utc).isoformat()
    items = [{"ts": fresh, "title": "Copper news", "desc": "", "why": "x", "link": "x"}]
    with mock.patch("linkedin_ideas.pr.fetch_prices", return_value={"Cu": (9950.0, None, "stooq")}):
        candidates = li.gather_price_context_candidates(items)
    assert candidates == []


# --- gather_candidates: интеграция всех пяти источников ---------------------

def test_gather_candidates_includes_all_five_sources_when_present():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    filings_labels = {"label": "good", "ts": fresh_ts, "topic": "Filings hook"}
    filings_items_with_shift = {"ts": fresh_ts, "company": "X", "shifts": ["сумма 10 → 20 млн $ (с 08-01)"]}
    digest_items = [
        {"ts": fresh_ts, "priority": "high", "title": "Digest news", "why": "x", "link": "x", "company": "Alcoa"},
        {"ts": fresh_ts, "priority": "high", "title": "Alcoa second mention", "why": "y", "link": "y", "company": "Alcoa"},
    ]
    filings = {"labels": [filings_labels], "items": [filings_items_with_shift]}
    digest = {"items": digest_items}

    with mock.patch("linkedin_ideas.load_json", side_effect=_mock_load_json(filings_history=filings, digest_history=digest)):
        with mock.patch("linkedin_ideas.pr.fetch_prices", return_value={"Cu": (9950.0, 5.0, "CME")}):
            candidates = li.gather_candidates()

    sources = {c["source"] for c in candidates}
    # filings (label) + digest (2 items, но company_cluster группирует их
    # ОТДЕЛЬНО, digest тоже остаётся по одной записи на item) + facts_shift +
    # company_cluster. price_context не сработает — ни одна digest-заметка
    # не упоминает медь текстуально.
    assert "filings" in sources
    assert "digest" in sources
    assert "facts_shift" in sources
    assert "company_cluster" in sources
