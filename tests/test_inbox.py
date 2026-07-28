"""Тесты для inbox.py — matching входящих писем на платформу по домену,
детект срочности по ключевым словам.

inbox.py на верхнем уровне читает os.environ.
"""
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")
os.environ.setdefault("GMAIL_USER", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")

sys.path.insert(0, "..")
import inbox as ib


# Фикстура зеркалит реальную структуру platforms.json (не выдумана —
# скопирована из формата, использованного в проде).
CFG = {
    "platforms": [
        {"name": "GLG", "domains": ["glgroup.com", "glg.it", "glgresearch.com"]},
        {"name": "Guidepoint", "domains": ["guidepoint.com"]},
        {"name": "AlphaSights", "domains": ["alphasights.com"]},
    ]
}


# --- match_platform ----------------------------------------------------

def test_match_platform_exact_domain():
    assert ib.match_platform("glgroup.com", CFG) == "GLG"


def test_match_platform_subdomain():
    # sender_domain.endswith("." + d) — поддомен тоже матчится
    assert ib.match_platform("mail.glgroup.com", CFG) == "GLG"


def test_match_platform_case_sensitivity_of_input():
    # match_platform сам не лоуэркейсит sender_domain — предполагается,
    # что вызывающий код (domain_of) уже привёл к нижнему регистру
    assert ib.match_platform("GLGROUP.COM", CFG) is None


def test_match_platform_no_match_returns_none():
    assert ib.match_platform("randomcompany.com", CFG) is None


def test_match_platform_empty_domain():
    assert ib.match_platform("", CFG) is None
    assert ib.match_platform(None, CFG) is None


def test_match_platform_does_not_false_match_similar_domain():
    # "notglgroup.com" не должен матчиться на "glgroup.com" — ни равенство,
    # ни суффикс ".glgroup.com" тут не подходят
    assert ib.match_platform("notglgroup.com", CFG) is None


def test_match_platform_second_platform_in_list():
    assert ib.match_platform("guidepoint.com", CFG) == "Guidepoint"


# --- is_urgent -----------------------------------------------------------

def test_is_urgent_true_when_keyword_present():
    assert ib.is_urgent("Urgent: please respond by EOD", ["urgent", "asap"]) is True


def test_is_urgent_false_when_no_keyword():
    assert ib.is_urgent("Weekly newsletter update", ["urgent", "asap"]) is False


def test_is_urgent_case_insensitive():
    assert ib.is_urgent("URGENT REQUEST", ["urgent"]) is True


def test_is_urgent_substring_match():
    # any(kw in s for kw in keywords) — подстрока, не только целое слово
    assert ib.is_urgent("this is time-sensitive", ["time-sensitive"]) is True


def test_is_urgent_empty_keywords_list():
    assert ib.is_urgent("Anything at all", []) is False
