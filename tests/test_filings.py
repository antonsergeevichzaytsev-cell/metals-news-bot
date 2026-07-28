"""Тесты для чистой логики filings.py — prefilter (гейт до DeepSeek),
разбор вердикта разметки, проверка свежести.

filings.py на верхнем уровне читает os.environ — подставляем переменные
до импорта.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

sys.path.insert(0, "..")
import filings as fl


# --- prefilter ---------------------------------------------------------
# True = отдаём DeepSeek (платно). False = режем бесплатно как шум.
# Сигнал побеждает шум (сначала проверяется SIGNAL_RE), совсем нейтральный
# текст без явного шума проходит по умолчанию (лучше лишний раз спросить
# DeepSeek, чем молча выкинуть настоящую новость).

def test_prefilter_passes_clear_engineering_signal():
    assert fl.prefilter("Company Announces Feasibility Study Results", "") is True


def test_prefilter_blocks_clear_noise_private_placement():
    assert fl.prefilter("Company Closes Non-Brokered Private Placement", "") is False


def test_prefilter_blocks_board_appointment_noise():
    assert fl.prefilter("Company Appoints New CFO", "") is False


def test_prefilter_blocks_agm_noise():
    assert fl.prefilter("Company Announces Voting Results at Annual General Meeting", "") is False


def test_prefilter_signal_beats_noise_when_both_present():
    # Заголовок формально похож на noise (appoint), но описание содержит
    # capex — сигнал должен победить, чтобы не резать реальную новость
    # из-за случайного слова про смену менеджмента в том же релизе.
    title = "Company Appoints New Project Director"
    desc = "as construction begins with updated capital cost estimate of $200M"
    assert fl.prefilter(title, desc) is True


def test_prefilter_passes_ambiguous_text_by_default():
    # Ни явного сигнала, ни явного шума в заголовке — по умолчанию True
    # (лучше потратить DeepSeek-вызов, чем молча выкинуть настоящую новость)
    assert fl.prefilter("Company Provides Corporate Update", "") is True


def test_prefilter_blocks_conference_presentation():
    assert fl.prefilter("Company to Present at Mining Investment Conference", "") is False


def test_prefilter_passes_capex_overrun():
    assert fl.prefilter("Company Reports Cost Overrun at Flagship Project", "") is True


def test_prefilter_passes_pea_technical_report():
    assert fl.prefilter("Company Files NI 43-101 Technical Report for PEA", "") is True


# --- parse_verdict -------------------------------------------------------
# Дословный и тупой разбор намеренно (комментарий в коде): непонятное
# честно кладём как note, не угадываем смысл.

def test_parse_verdict_good_exact_words():
    for word in ["+", "++", "да", "годится", "ок", "топ", "yes"]:
        assert fl.parse_verdict(word) == "good", f"failed on {word!r}"


def test_parse_verdict_bad_exact_words():
    for word in ["-", "--", "нет", "мимо", "шум", "no"]:
        assert fl.parse_verdict(word) == "bad", f"failed on {word!r}"


def test_parse_verdict_good_first_word_no_trailing_punct():
    assert fl.parse_verdict("да полезно для брифа") == "good"


def test_parse_verdict_bad_first_word_no_trailing_punct():
    assert fl.parse_verdict("мимо это не про CIS") == "bad"


def test_parse_verdict_comma_after_first_word_now_matches():
    # Fix 28.07: было note (баг — запятая после первого слова не
    # чистилась), теперь корректно распознаётся как good/bad.
    assert fl.parse_verdict("да, полезно для брифа") == "good"
    assert fl.parse_verdict("мимо, это не про CIS") == "bad"


def test_parse_verdict_unclear_text_is_note():
    # Ключевое поведение: не угадываем смысл, честно note
    assert fl.parse_verdict("интересно, но не сейчас") == "note"
    assert fl.parse_verdict("хм") == "note"


def test_parse_verdict_empty_is_note():
    assert fl.parse_verdict("") == "note"
    assert fl.parse_verdict(None) == "note"


def test_parse_verdict_case_and_punctuation_insensitive():
    assert fl.parse_verdict("ДА!") == "good"
    assert fl.parse_verdict("Нет.") == "bad"
    assert fl.parse_verdict("ок)") == "good"


# --- is_recent -----------------------------------------------------------

def test_is_recent_true_for_fresh_item():
    dt = datetime.now(timezone.utc) - timedelta(hours=1)
    assert fl.is_recent(dt) is True


def test_is_recent_false_for_old_item():
    dt = datetime.now(timezone.utc) - timedelta(hours=48)
    assert fl.is_recent(dt) is False


def test_is_recent_true_just_inside_boundary():
    # Не ровно 36ч: is_recent() зовёт datetime.now() чуть позже теста,
    # так что тест точно на границе — гонка на миллисекундах, не баг.
    dt = datetime.now(timezone.utc) - timedelta(hours=35, minutes=59)
    assert fl.is_recent(dt) is True


def test_is_recent_true_when_date_unknown():
    # None = дату не удалось распарсить — не режем на всякий случай
    assert fl.is_recent(None) is True
