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


# --- v9: UZCOPPER-relevance tag ---------------------------------------------

def test_uzcopper_relevant_named_entity():
    assert dg.is_uzcopper_relevant("Almalyk MMC announces expansion") is True


def test_uzcopper_relevant_copper_smelter_combo():
    assert dg.is_uzcopper_relevant("New copper smelter capacity in Central Asia") is True


def test_uzcopper_relevant_copper_concentrate_phrase():
    assert dg.is_uzcopper_relevant("Copper concentrate exports rise sharply") is True


def test_uzcopper_not_relevant_unrelated_copper_news():
    # "copper" solo без переработки/региона — не должно ловиться, иначе тег
    # обесценится (сработает на треть потока про медь вообще).
    assert dg.is_uzcopper_relevant("Copper prices rise on demand outlook") is False


def test_uzcopper_not_relevant_empty_text():
    assert dg.is_uzcopper_relevant("") is False


def test_uzcopper_not_relevant_other_metal():
    assert dg.is_uzcopper_relevant("Nickel prices fall amid oversupply") is False


# --- v11: find_similar_history for deep-analysis trend context -------------

def test_find_similar_history_no_company_returns_empty():
    history = {"items": [{"ts": "2026-08-01T00:00:00Z", "company": "RUSAL", "title": "x"}]}
    assert dg.find_similar_history(history, "", "current title") == []


def test_find_similar_history_matches_same_company_case_insensitive():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    history = {"items": [
        {"ts": recent, "company": "rusal", "title": "Old RUSAL news", "why": "context"},
    ]}
    result = dg.find_similar_history(history, "RUSAL", "current title")
    assert len(result) == 1
    assert result[0]["title"] == "Old RUSAL news"


def test_find_similar_history_excludes_current_title():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    history = {"items": [
        {"ts": recent, "company": "RUSAL", "title": "Same as current"},
    ]}
    result = dg.find_similar_history(history, "RUSAL", "Same as current")
    assert result == []


def test_find_similar_history_respects_days_window():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    history = {"items": [
        {"ts": old, "company": "RUSAL", "title": "Too old"},
    ]}
    result = dg.find_similar_history(history, "RUSAL", "current", days=30)
    assert result == []


def test_find_similar_history_different_company_excluded():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).isoformat()
    history = {"items": [
        {"ts": recent, "company": "Glencore", "title": "Unrelated"},
    ]}
    result = dg.find_similar_history(history, "RUSAL", "current")
    assert result == []


def test_find_similar_history_sorted_newest_first_and_limited():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    items = []
    for days_ago in [10, 5, 20, 1, 15]:
        ts = (now - timedelta(days=days_ago)).isoformat()
        items.append({"ts": ts, "company": "RUSAL", "title": f"item-{days_ago}"})
    history = {"items": items}
    result = dg.find_similar_history(history, "RUSAL", "current", limit=3)
    assert len(result) == 3
    # Newest first: days_ago 1, 5, 10
    assert [it["title"] for it in result] == ["item-1", "item-5", "item-10"]


# --- v12: watchlist_matches -------------------------------------------------

def test_watchlist_matches_finds_substring():
    assert dg.watchlist_matches("Smelter restart announced in Chile", ["smelter restart"]) == ["smelter restart"]


def test_watchlist_matches_case_insensitive():
    assert dg.watchlist_matches("CBAM tariffs increase", ["cbam"]) == ["cbam"]


def test_watchlist_matches_multiple_terms_multiple_hits():
    text = "Copper smelter restart amid CBAM concerns"
    hits = dg.watchlist_matches(text, ["smelter restart", "cbam", "nickel"])
    assert set(hits) == {"smelter restart", "cbam"}


def test_watchlist_matches_no_hits():
    assert dg.watchlist_matches("Gold prices steady", ["smelter", "cbam"]) == []


def test_watchlist_matches_empty_text():
    assert dg.watchlist_matches("", ["smelter"]) == []


def test_watchlist_matches_empty_terms():
    assert dg.watchlist_matches("Some smelter news", []) == []


# --- v12: load_watchlist -----------------------------------------------

def test_load_watchlist_missing_file_returns_empty(tmp_path, monkeypatch):
    fake_path = tmp_path / "nonexistent_watchlist.json"
    monkeypatch.setattr(dg, "WATCHLIST_FILE", str(fake_path))
    assert dg.load_watchlist() == []


def test_load_watchlist_reads_terms(tmp_path, monkeypatch):
    import json as json_mod
    fake_path = tmp_path / "watchlist.json"
    fake_path.write_text(json_mod.dumps({"terms": ["cbam", "smelter"]}), encoding="utf-8")
    monkeypatch.setattr(dg, "WATCHLIST_FILE", str(fake_path))
    assert dg.load_watchlist() == ["cbam", "smelter"]


def test_load_watchlist_corrupt_file_returns_empty(tmp_path, monkeypatch):
    fake_path = tmp_path / "watchlist.json"
    fake_path.write_text("not valid json{{{", encoding="utf-8")
    monkeypatch.setattr(dg, "WATCHLIST_FILE", str(fake_path))
    assert dg.load_watchlist() == []


# --- MAX_ENRICH_ATTEMPTS: защитный потолок на число попыток -----------------
# 20.08.2026: изолированная проверка логики цикла enrichment (не полный
# main() — он не тестируется здесь как интеграционная функция, см.
# заголовок файла). Симулирует именно тот паттерн, который раньше давал
# неограниченное число вызовов DeepSeek за прогон: если модель массово
# говорит skip=True, len(enriched) почти не растёт, а без верхнего
# предела на попытки цикл продолжает бы дёргать API для каждого
# кандидата. На бэклоге после простоя (сотни кандидатов) это упирало
# 8-минутный timeout воркфлоу даже с отключённым thinking mode.

def test_enrich_loop_respects_max_attempts_when_mostly_skipped():
    """Симулирует реальный цикл из digest.py:main() с мокнутым
    deepseek_enrich, который всегда говорит skip=True — воспроизводит
    точную форму бага (enriched не растёт, но без капа попытки росли
    бы неограниченно)."""
    candidates = [{"title": f"c{i}", "desc": "", "domain": "x"} for i in range(500)]
    n_attempts = 0
    enriched = []

    def fake_enrich(title, desc, domain):
        return {"skip": True}

    for c in candidates:
        if len(enriched) >= dg.MAX_ITEMS_PER_RUN:
            break
        if n_attempts >= dg.MAX_ENRICH_ATTEMPTS:
            break
        n_attempts += 1
        verdict = fake_enrich(c["title"], c["desc"], c["domain"])
        if verdict.get("skip"):
            continue
        enriched.append(c)

    assert n_attempts == dg.MAX_ENRICH_ATTEMPTS
    assert n_attempts < len(candidates)  # не проехал по всем 500 -> кап реально сработал


def test_enrich_loop_stops_at_max_items_before_hitting_attempt_cap():
    """Когда кандидатов принимается достаточно быстро, MAX_ITEMS_PER_RUN
    останавливает цикл раньше MAX_ENRICH_ATTEMPTS — кап на попытки не
    мешает нормальному (быстрому) случаю."""
    candidates = [{"title": f"c{i}", "desc": "", "domain": "x"} for i in range(500)]
    n_attempts = 0
    enriched = []

    def fake_enrich(title, desc, domain):
        return {"skip": False, "why": "x", "priority": "low", "company": ""}

    for c in candidates:
        if len(enriched) >= dg.MAX_ITEMS_PER_RUN:
            break
        if n_attempts >= dg.MAX_ENRICH_ATTEMPTS:
            break
        n_attempts += 1
        verdict = fake_enrich(c["title"], c["desc"], c["domain"])
        if verdict.get("skip"):
            continue
        enriched.append(c)

    assert len(enriched) == dg.MAX_ITEMS_PER_RUN
    assert n_attempts == dg.MAX_ITEMS_PER_RUN  # ни одного skip -> attempts == enriched
    assert n_attempts < dg.MAX_ENRICH_ATTEMPTS


def test_max_enrich_attempts_exceeds_max_items_per_run():
    """Сама константа должна быть больше MAX_ITEMS_PER_RUN — иначе кап
    на попытки сработает раньше, чем бот успеет набрать нормальную
    дневную квоту публикаций даже без единого skip."""
    assert dg.MAX_ENRICH_ATTEMPTS > dg.MAX_ITEMS_PER_RUN
