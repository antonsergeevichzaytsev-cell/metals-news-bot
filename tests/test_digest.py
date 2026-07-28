"""Тесты для чистой логики digest.py — разбор заголовка/источника,
блок-лист источников, дедупликация по похожести заголовков.
"""
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")
os.environ.setdefault("DEEPSEEK_API_KEY", "test")

sys.path.insert(0, "..")
import digest as dg


# --- split_title_and_source -------------------------------------------------

def test_split_title_and_source_with_dash():
    title, pub = dg.split_title_and_source("Company Reports Q2 Results - Reuters")
    assert title == "Company Reports Q2 Results"
    assert pub == "Reuters"


def test_split_title_and_source_with_em_dash():
    title, pub = dg.split_title_and_source("Company Reports Q2 Results — Bloomberg")
    assert title == "Company Reports Q2 Results"
    assert pub == "Bloomberg"


def test_split_title_and_source_no_separator():
    title, pub = dg.split_title_and_source("Just A Title With No Source")
    assert title == "Just A Title With No Source"
    assert pub == ""


def test_split_title_and_source_empty():
    assert dg.split_title_and_source("") == ("", "")
    assert dg.split_title_and_source(None) == ("", "")


def test_split_title_and_source_uses_last_separator():
    # Заголовок сам может содержать " - " по смыслу — берём ПОСЛЕДНЕЕ
    # вхождение, чтобы не отрезать источник посреди заголовка.
    title, pub = dg.split_title_and_source("Copper - Aluminium Comparison - Mining.com")
    assert title == "Copper - Aluminium Comparison"
    assert pub == "Mining.com"


# --- source_to_domain --------------------------------------------------------

def test_source_to_domain_known_label():
    assert dg.source_to_domain("Reuters", "https://example.com/x") == "reuters.com"


def test_source_to_domain_case_insensitive():
    assert dg.source_to_domain("REUTERS", "https://example.com/x") == "reuters.com"


def test_source_to_domain_unknown_label_returned_as_is():
    assert dg.source_to_domain("SomeRandomBlog", "https://example.com/x") == "SomeRandomBlog"


def test_source_to_domain_empty_label_falls_back_to_url():
    assert dg.source_to_domain("", "https://www.mining.com/article") == "mining.com"
    assert dg.source_to_domain(None, "https://www.mining.com/article") == "mining.com"


# --- is_blocked ---------------------------------------------------------

def test_is_blocked_by_label():
    assert dg.is_blocked("Yahoo Finance", "somesite.com") is True


def test_is_blocked_by_domain():
    assert dg.is_blocked("Some Label", "seekingalpha.com") is True


def test_is_blocked_false_for_legit_source():
    assert dg.is_blocked("Reuters", "reuters.com") is False


def test_is_blocked_case_insensitive():
    assert dg.is_blocked("BENZINGA", "somesite.com") is True


# --- title_tokens / is_near_duplicate ---------------------------------------

def test_title_tokens_extracts_meaningful_words():
    toks = dg.title_tokens("Copper Prices Rise After Chile Mine Strike")
    assert "copper" in toks
    assert "chile" in toks
    assert "strike" in toks


def test_title_tokens_filters_stopwords():
    toks = dg.title_tokens("The New Market Chatter Faces Copper")
    assert "the" not in toks
    assert "new" not in toks
    assert "market" not in toks
    assert "chatter" not in toks
    assert "faces" not in toks
    assert "copper" in toks


def test_title_tokens_filters_short_words():
    toks = dg.title_tokens("Al Cu at $5 up 3%")
    # "al", "cu" - 2 буквы, отрезаются порогом >= 3
    assert "al" not in toks
    assert "cu" not in toks


def test_is_near_duplicate_true_for_similar_titles():
    sig_a = dg.title_tokens("Rusal Reports Record Aluminium Output This Quarter")
    sig_b = dg.title_tokens("Rusal Reports Record Aluminium Production This Quarter")
    assert dg.is_near_duplicate(sig_a, [sig_b]) is True


def test_is_near_duplicate_false_for_different_titles():
    sig_a = dg.title_tokens("Rusal Reports Record Aluminium Output")
    sig_b = dg.title_tokens("Copper Prices Fall Amid Chile Strike News")
    assert dg.is_near_duplicate(sig_a, [sig_b]) is False


def test_is_near_duplicate_false_for_empty_signature():
    assert dg.is_near_duplicate(set(), [{"copper", "chile"}]) is False


def test_is_near_duplicate_false_when_no_kept_sigs():
    sig = dg.title_tokens("Copper Prices Rise")
    assert dg.is_near_duplicate(sig, []) is False


def test_is_near_duplicate_respects_threshold():
    # Ровно на грани: 1 общий токен из 4 в объединении (1/4=0.25) — ниже
    # дефолтного threshold=0.5, не должно считаться дубликатом.
    sig_a = {"copper", "prices", "rise", "today"}
    sig_b = {"copper", "falls", "yesterday", "again"}
    assert dg.is_near_duplicate(sig_a, [sig_b], threshold=0.5) is False
