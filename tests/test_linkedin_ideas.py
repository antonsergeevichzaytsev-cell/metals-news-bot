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
