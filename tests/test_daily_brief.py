"""Тесты для daily_brief.py — выбор фразы дня (детерминированный,
без сети) и pure-фильтрация в pick_top_of_week (priority-отсечка,
early-return на пустых входных).

daily_brief.py на верхнем уровне читает os.environ.
"""
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")

sys.path.insert(0, "..")
import daily_brief as db


# Фикстура зеркалит реальную структуру phrases.json
PHRASES = [
    {"cat": "anchor", "text": "Anchor phrase 1"},
    {"cat": "anchor", "text": "Anchor phrase 2"},
    {"cat": "rate", "text": "Rate phrase 1"},
    {"cat": "closing", "text": "Closing phrase 1"},
    {"cat": "pitch", "text": "Pitch phrase 1"},
]


# --- pick_phrase ----------------------------------------------------------

def test_pick_phrase_monday_uses_anchor_rate_categories():
    # DAY_CATEGORIES[0] = ["anchor", "rate"] (понедельник)
    result = db.pick_phrase(PHRASES, weekday=0, week_index=0)
    assert result["cat"] in ("anchor", "rate")


def test_pick_phrase_deterministic_by_week_index():
    # Один и тот же weekday+week_index -> всегда одна и та же фраза
    a = db.pick_phrase(PHRASES, weekday=0, week_index=3)
    b = db.pick_phrase(PHRASES, weekday=0, week_index=3)
    assert a == b


def test_pick_phrase_cycles_through_pool():
    # week_index растёт -> проходит по пулу категории (не всегда одна и та же)
    pool_for_monday = [p for p in PHRASES if p["cat"] in ("anchor", "rate")]
    seen = {db.pick_phrase(PHRASES, weekday=0, week_index=i)["text"] for i in range(len(pool_for_monday))}
    assert len(seen) == len(pool_for_monday)


def test_pick_phrase_falls_back_to_full_pool_if_category_empty():
    # День без совпадающих категорий в фразах -> берёт весь список,
    # а не падает и не возвращает None
    phrases_no_match = [{"cat": "nonexistent_category", "text": "Only one"}]
    result = db.pick_phrase(phrases_no_match, weekday=0, week_index=0)
    assert result is not None
    assert result["text"] == "Only one"


def test_pick_phrase_none_for_empty_phrases():
    assert db.pick_phrase([], weekday=0, week_index=0) is None


def test_pick_phrase_unknown_weekday_falls_back():
    # weekday вне 0-6 -> DAY_CATEGORIES.get() вернёт [], пул пуст -> весь список
    result = db.pick_phrase(PHRASES, weekday=99, week_index=0)
    assert result is not None


# --- pick_top_of_week: pure-часть (до реального вызова DeepSeek) ------------

def test_pick_top_of_week_returns_none_without_api_key():
    original = db.DEEPSEEK_KEY
    db.DEEPSEEK_KEY = ""
    try:
        item, for_call = db.pick_top_of_week({"items": [{"title": "x"}]})
        assert item is None
        assert for_call == ""
    finally:
        db.DEEPSEEK_KEY = original


def test_pick_top_of_week_returns_none_for_empty_history():
    original = db.DEEPSEEK_KEY
    db.DEEPSEEK_KEY = "fake-key-for-this-test"
    try:
        item, for_call = db.pick_top_of_week({"items": []})
        assert item is None
        assert for_call == ""
    finally:
        db.DEEPSEEK_KEY = original


def test_pick_top_of_week_returns_none_for_missing_items_key():
    original = db.DEEPSEEK_KEY
    db.DEEPSEEK_KEY = "fake-key-for-this-test"
    try:
        item, for_call = db.pick_top_of_week({})
        assert item is None
        assert for_call == ""
    finally:
        db.DEEPSEEK_KEY = original
