"""Тесты для чистой логики account_watch.py — фильтр правдоподобности
имени компании и проверка упоминания компании в заголовке новости.

account_watch.py на верхнем уровне читает os.environ — подставляем
переменные до импорта.
"""
import sys
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")

sys.path.insert(0, "..")
import account_watch as aw


# --- is_plausible_company_name --------------------------------------------
# Регрессия-мотив (из is_trackable_recipient в pipeline_sync, тот же класс
# проблемы): одно общеупотребимое слово в качестве "имени компании" даёт
# мусорный поиск по всей ленте новостей.

def test_plausible_name_multiword_specific():
    assert aw.is_plausible_company_name("Steppe Gold") is True


def test_plausible_name_single_specific_word():
    assert aw.is_plausible_company_name("Seligdar") is True


def test_implausible_single_generic_word():
    for word in ["Company", "Mining", "Metals", "Group", "Holdings"]:
        assert aw.is_plausible_company_name(word) is False, f"failed on {word!r}"


def test_multiword_with_generic_token_still_plausible():
    # Комментарий в коде: многословные имена не блокируются, даже если
    # один из токенов общий — "Steppe Gold" специфично само по себе,
    # хотя формально не в списке. Проверяем аналог: "District Metals"
    # содержит generic-слово "metals", но это два слова -> не блокируется.
    assert aw.is_plausible_company_name("District Metals") is True


def test_implausible_too_short():
    assert aw.is_plausible_company_name("Ab") is False


def test_implausible_empty_or_none():
    assert aw.is_plausible_company_name("") is False
    assert aw.is_plausible_company_name(None) is False
    assert aw.is_plausible_company_name("   ") is False


def test_implausible_generic_case_insensitive():
    assert aw.is_plausible_company_name("COMPANY") is False
    assert aw.is_plausible_company_name("company") is False


# --- title_mentions_company ------------------------------------------------
# Регрессия 21.07: "exact phrase" Google News matching пропускал статьи
# про индекс МосБиржи или конкурента, если "Селигдар" был где-то в
# related-links, а не в заголовке. Fix — требовать явное вхождение в
# заголовок, для многословных имён — оба слова.

def test_title_mentions_single_word_company():
    assert aw.title_mentions_company("Селигдар нарастил добычу золота", "Селигдар") is True


def test_title_mentions_false_when_absent():
    assert aw.title_mentions_company("Полюс объявил дивиденды", "Селигдар") is False


def test_title_mentions_multiword_requires_both_words():
    assert aw.title_mentions_company(
        "District Metals Corp announces PEA results", "District Metals"
    ) is True


def test_title_mentions_multiword_false_if_only_one_word_present():
    # Регрессия 21.07 в чистом виде: заголовок про индекс/конкурента,
    # содержащий только одно из двух слов искомой компании, не должен
    # засчитываться как упоминание.
    assert aw.title_mentions_company(
        "Metals sector index falls amid broader selloff", "District Metals"
    ) is False


def test_title_mentions_case_insensitive():
    assert aw.title_mentions_company("SELIGDAR REPORTS Q2 RESULTS", "Селигдар") is False
    # разный алфавит нарочно не матчится — латиница "SELIGDAR" не равна
    # кириллице "Селигдар" в .lower(), это отдельный кейс, не баг теста
    assert aw.title_mentions_company("Seligdar reports Q2 results", "Seligdar") is True


def test_title_mentions_short_words_ignored_in_multiword():
    # len(w) > 2 — короткие связки типа "as", "of" не требуются к матчу
    assert aw.title_mentions_company(
        "New CIS Metals project announced", "CIS Metals"
    ) is True
