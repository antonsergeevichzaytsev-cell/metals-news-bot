"""Тесты для prices.py — единый источник цен металлов, вынесенный
19.08.2026 из mission_control.py (устранена третья независимая копия
той же логики, ранее жившая в bot_commands.py:cmd_prices, без
Stooq-fallback и без sanity-проверки).

Раньше эта логика не имела юнит-тестов вообще — только проверялась
вживую через реальные вызовы Yahoo/Stooq.
"""
import json
import sys
from unittest import mock

sys.path.insert(0, "..")
import prices as pr


class FakeResp:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data


# --- fetch_yahoo -------------------------------------------------------------

def test_fetch_yahoo_success_with_change():
    body = json.dumps({
        "chart": {"result": [{"meta": {
            "regularMarketPrice": 4.5123,
            "previousClose": 4.5000,
        }}]}
    }).encode()
    with mock.patch.object(pr.net, "urlopen_retry", return_value=FakeResp(body)):
        price, chg = pr.fetch_yahoo("HG=F")
    assert price == 4.5123
    expected_chg = (4.5123 - 4.5) / 4.5 * 100.0
    assert abs(chg - expected_chg) < 1e-9


def test_fetch_yahoo_no_prev_close_returns_none_change():
    body = json.dumps({
        "chart": {"result": [{"meta": {"regularMarketPrice": 4.5}}]}
    }).encode()
    with mock.patch.object(pr.net, "urlopen_retry", return_value=FakeResp(body)):
        price, chg = pr.fetch_yahoo("HG=F")
    assert price == 4.5
    assert chg is None


def test_fetch_yahoo_empty_result_raises():
    body = json.dumps({"chart": {"result": None}}).encode()
    with mock.patch.object(pr.net, "urlopen_retry", return_value=FakeResp(body)):
        try:
            pr.fetch_yahoo("HG=F")
            assert False, "should have raised"
        except RuntimeError:
            pass


def test_fetch_yahoo_all_urls_fail_raises_last_error():
    with mock.patch.object(pr.net, "urlopen_retry", side_effect=OSError("timeout")):
        try:
            pr.fetch_yahoo("HG=F")
            assert False, "should have raised"
        except RuntimeError as e:
            assert "timeout" in str(e)


# --- fetch_stooq ---------------------------------------------------------

def test_fetch_stooq_success():
    body = "Symbol,Date,Time,Open,High,Low,Close\nHG.F,2026-08-19,00:00:00,4.5,4.6,4.4,4.55\n".encode()
    with mock.patch.object(pr.net, "urlopen_retry", return_value=FakeResp(body)):
        price, chg = pr.fetch_stooq("hg.f")
    assert price == 4.55
    assert chg is None  # stooq не даёт previous close в этом формате


def test_fetch_stooq_no_data_raises():
    body = "Symbol,Date,Time,Open,High,Low,Close\nHG.F,N/D,N/D,N/D,N/D,N/D,N/D\n".encode()
    with mock.patch.object(pr.net, "urlopen_retry", return_value=FakeResp(body)):
        try:
            pr.fetch_stooq("hg.f")
            assert False, "should have raised"
        except RuntimeError:
            pass


def test_fetch_stooq_empty_response_raises():
    body = "Symbol,Date,Time,Open,High,Low,Close\n".encode()
    with mock.patch.object(pr.net, "urlopen_retry", return_value=FakeResp(body)):
        try:
            pr.fetch_stooq("hg.f")
            assert False, "should have raised"
        except RuntimeError:
            pass


# --- is_plausible_price ----------------------------------------------------

def test_is_plausible_price_within_range():
    assert pr.is_plausible_price("Al", 2500) is True
    assert pr.is_plausible_price("Cu", 9000) is True


def test_is_plausible_price_outside_range():
    assert pr.is_plausible_price("Al", 200) is False  # похоже на смену единиц
    assert pr.is_plausible_price("Cu", 50000) is False


def test_is_plausible_price_unknown_symbol_always_true():
    assert pr.is_plausible_price("Ni", 15000) is True  # нет диапазона -> не фильтруем


# --- fetch_prices (интеграция yahoo+stooq+sanity) ---------------------------

def test_fetch_prices_yahoo_success_skips_stooq():
    def fake_yahoo(symbol, timeout=10):
        # Cu использует mult=2204.62 (перевод из $/lb в $/т) — значение
        # должно быть в диапазоне до умножения, иначе sanity-check его
        # отбросит. Al использует mult=1.0.
        if symbol == "HG=F":
            return (4.3, 1.5)  # 4.3 * 2204.62 ≈ 9480 -> в диапазоне Cu (5000-15000)
        return (2500.0, 1.5)  # Al: 2500 * 1.0 -> в диапазоне (1500-5000)

    with mock.patch.object(pr, "fetch_yahoo", side_effect=fake_yahoo):
        with mock.patch.object(pr, "fetch_stooq") as mock_stooq:
            prices = pr.fetch_prices()
    assert "Cu" in prices
    assert prices["Cu"][2] == "CME"
    assert "Al" in prices
    mock_stooq.assert_not_called()  # yahoo сработал первым для обоих -> stooq не нужен


def test_fetch_prices_falls_back_to_stooq_on_yahoo_failure():
    def fake_yahoo(symbol, timeout=10):
        raise RuntimeError("yahoo down")

    def fake_stooq(symbol, timeout=10):
        if symbol == "hg.f":
            return (4.3, None)  # 4.3 * 2204.62 ≈ 9480 -> в диапазоне Cu
        return (2500.0, None)  # Al: 2500 * 1.0 -> в диапазоне

    with mock.patch.object(pr, "fetch_yahoo", side_effect=fake_yahoo):
        with mock.patch.object(pr, "fetch_stooq", side_effect=fake_stooq):
            prices = pr.fetch_prices()
    assert "Cu" in prices
    assert prices["Cu"][2] == "stooq"
    assert "Al" in prices
    assert prices["Al"][2] == "stooq"


def test_fetch_prices_implausible_value_excluded():
    """Yahoo вернул технически валидное число, но вне разумного диапазона
    (похоже на смену единиц/битый парсинг) -> символ пропущен, не попадает
    в результат с мусорным значением. Stooq тоже недоступен, чтобы
    изолировать именно эту ветку (без fallback-спасения)."""
    def fake_yahoo(symbol, timeout=10):
        if symbol == "ALI=F":
            return (1.0, None)  # Al: 1.0 * 1.0 = 1.0, вне (1500,5000)
        return (4.3, None)  # Cu: 4.3 * 2204.62 ≈ 9480, в диапазоне

    with mock.patch.object(pr, "fetch_yahoo", side_effect=fake_yahoo):
        with mock.patch.object(pr, "fetch_stooq", side_effect=RuntimeError("also down")):
            prices = pr.fetch_prices()
    assert "Al" not in prices
    assert "Cu" in prices


def test_fetch_prices_both_sources_fail_symbol_missing():
    with mock.patch.object(pr, "fetch_yahoo", side_effect=RuntimeError("down")):
        with mock.patch.object(pr, "fetch_stooq", side_effect=RuntimeError("down")):
            prices = pr.fetch_prices()
    assert prices == {}


# --- format_prices -----------------------------------------------------------

def test_format_prices_empty():
    assert pr.format_prices({}) == ""


def test_format_prices_with_change():
    text = pr.format_prices({"Cu": (9950.5, 1.23, "CME")})
    assert "Cu" in text
    assert "9,950" in text or "9,951" in text
    assert "1.2%" in text


def test_format_prices_negative_change_shows_down_arrow():
    text = pr.format_prices({"Al": (2480.0, -0.5, "CME")})
    assert "\u25bc" in text


def test_format_prices_no_change_no_arrow():
    text = pr.format_prices({"Al": (2480.0, None, "stooq")})
    assert "\u25b2" not in text and "\u25bc" not in text
