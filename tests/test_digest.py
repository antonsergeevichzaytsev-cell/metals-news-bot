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


# --- tg_send / tg_send_chunks: feedback loop --------------------------------
# 20.08.2026: tg_send теперь возвращает message_id (раньше отбрасывался),
# tg_send_chunks принимает (block, link) пары вместо голых блоков и
# возвращает (total, [(message_id, [links])]) — нужно, чтобы связать
# реакцию пользователя на сообщение с конкретными новостями внутри него
# (bot_commands.py читает эту связку из state_feedback_map.json).

def test_tg_send_returns_message_id(monkeypatch):
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json as json_mod
            return json_mod.dumps({"ok": True, "result": {"message_id": 12345}}).encode()

    monkeypatch.setattr(dg.net, "urlopen_retry", lambda *a, **k: FakeResp())
    mid = dg.tg_send("test message")
    assert mid == 12345


def test_tg_send_chunks_single_message_maps_all_links(monkeypatch):
    sent_ids = iter([100])

    def fake_tg_send(text):
        return next(sent_ids)

    monkeypatch.setattr(dg, "tg_send", fake_tg_send)
    items = [("block1\n", "https://a.com"), ("block2\n", "https://b.com")]
    total, sent = dg.tg_send_chunks(items, "HEADER\n")
    assert total == 1
    assert sent == [(100, ["https://a.com", "https://b.com"])]


def test_tg_send_chunks_splits_by_budget_and_maps_correctly(monkeypatch):
    original_budget = dg.TG_BUDGET
    monkeypatch.setattr(dg, "TG_BUDGET", 50)  # маленький бюджет форсирует разбивку

    sent_ids = iter([201, 202])

    def fake_tg_send(text):
        return next(sent_ids)

    monkeypatch.setattr(dg, "tg_send", fake_tg_send)
    items = [
        ("x" * 30 + "\n", "https://a.com"),
        ("y" * 30 + "\n", "https://b.com"),
    ]
    total, sent = dg.tg_send_chunks(items, "H\n")
    assert total == 2
    # каждая ссылка попадает ровно в то сообщение, где реально оказался её блок
    all_links = [link for _, links in sent for link in links]
    assert "https://a.com" in all_links
    assert "https://b.com" in all_links
    monkeypatch.setattr(dg, "TG_BUDGET", original_budget)


def test_tg_send_chunks_empty_items_no_send(monkeypatch):
    calls = []
    monkeypatch.setattr(dg, "tg_send", lambda text: calls.append(text) or 1)
    total, sent = dg.tg_send_chunks([], "just a header\n")
    # заголовок сам по себе непустой -> одно сообщение с пустым списком ссылок
    assert total == 1
    assert sent == [(1, [])]


def test_tg_send_chunks_none_message_id_on_failure_still_tracked(monkeypatch):
    """Если tg_send вернёт None (сбой Telegram), запись всё равно
    появляется в sent — вызывающий код (main()) отвечает за то, чтобы
    не записывать None message_id в feedback map."""
    monkeypatch.setattr(dg, "tg_send", lambda text: None)
    items = [("block\n", "https://a.com")]
    total, sent = dg.tg_send_chunks(items, "H\n")
    assert total == 1
    assert sent == [(None, ["https://a.com"])]
