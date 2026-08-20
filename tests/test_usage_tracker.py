"""Тесты для usage_tracker.py — учёт трат DeepSeek API, добавлен
20.08.2026. До этого ни одно из семи мест, вызывающих DeepSeek, не
читало поле "usage" из ответа — реальные деньги без видимости.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "..")
import usage_tracker as ut


# --- is_peak_now -------------------------------------------------------------

def test_is_peak_now_true_in_early_morning_window():
    dt = datetime(2026, 8, 20, 2, 30, tzinfo=timezone.utc)
    assert ut.is_peak_now(dt) is True


def test_is_peak_now_true_in_late_morning_window():
    dt = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
    assert ut.is_peak_now(dt) is True


def test_is_peak_now_false_outside_windows():
    dt = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    assert ut.is_peak_now(dt) is False


def test_is_peak_now_boundary_hour_4_is_off_peak():
    # PEAK_HOURS_UTC = {1,2,3} | {6,7,8,9} -> час 4 и 5 не входят
    dt = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)
    assert ut.is_peak_now(dt) is False


def test_is_peak_now_boundary_hour_10_is_off_peak():
    dt = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    assert ut.is_peak_now(dt) is False


# --- compute_cost_usd ---------------------------------------------------------

def test_compute_cost_off_peak_matches_manual_calculation():
    usage = {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 0, "completion_tokens": 0}
    cost = ut.compute_cost_usd(usage, peak=False)
    assert abs(cost - 0.007) < 1e-9  # ровно ставка cache_hit за 1M токенов


def test_compute_cost_cache_miss_rate():
    usage = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1_000_000, "completion_tokens": 0}
    cost = ut.compute_cost_usd(usage, peak=False)
    assert abs(cost - 0.22) < 1e-9


def test_compute_cost_output_rate():
    usage = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0, "completion_tokens": 1_000_000}
    cost = ut.compute_cost_usd(usage, peak=False)
    assert abs(cost - 0.66) < 1e-9


def test_compute_cost_peak_is_exactly_double_off_peak():
    usage = {"prompt_cache_hit_tokens": 500, "prompt_cache_miss_tokens": 2000, "completion_tokens": 300}
    off_peak = ut.compute_cost_usd(usage, peak=False)
    peak = ut.compute_cost_usd(usage, peak=True)
    assert abs(peak - 2 * off_peak) < 1e-12


def test_compute_cost_missing_cache_split_falls_back_to_prompt_tokens_as_miss():
    """Ответ без разбивки по кэшу (не должно происходить у DeepSeek, но
    не должно крашиться) — весь prompt_tokens трактуется как miss, это
    консервативная (более высокая) оценка, не занижает расход."""
    usage = {"prompt_tokens": 1000, "completion_tokens": 0}
    cost = ut.compute_cost_usd(usage, peak=False)
    expected = (1000 / 1e6) * ut.RATES_OFF_PEAK["cache_miss"]
    assert abs(cost - expected) < 1e-12


def test_compute_cost_empty_usage_is_zero():
    assert ut.compute_cost_usd({}, peak=False) == 0.0


# --- record_usage + summary (integration через файловую систему) ------------

def test_record_usage_empty_dict_is_noop(tmp_path, monkeypatch):
    fake_path = tmp_path / "state_usage.json"
    monkeypatch.setattr(ut, "USAGE_PATH", str(fake_path))
    ut.record_usage("test.caller", {})
    assert not fake_path.exists()  # ничего не записано, файл не создан


def test_record_usage_creates_file_and_accumulates(tmp_path, monkeypatch):
    fake_path = tmp_path / "state_usage.json"
    monkeypatch.setattr(ut, "USAGE_PATH", str(fake_path))

    usage = {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 200, "completion_tokens": 50}
    ut.record_usage("digest.deepseek_enrich", usage)
    ut.record_usage("digest.deepseek_enrich", usage)

    total_cost, total_calls, by_caller = ut.summary(days=7)
    assert total_calls == 2
    assert total_cost > 0
    assert len(by_caller) == 1
    assert by_caller[0][0] == "digest.deepseek_enrich"


def test_record_usage_separates_by_caller(tmp_path, monkeypatch):
    fake_path = tmp_path / "state_usage.json"
    monkeypatch.setattr(ut, "USAGE_PATH", str(fake_path))

    usage_a = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1000, "completion_tokens": 0}
    usage_b = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10000, "completion_tokens": 0}
    ut.record_usage("caller_a", usage_a)
    ut.record_usage("caller_b", usage_b)

    _, total_calls, by_caller = ut.summary(days=7)
    assert total_calls == 2
    names = dict(by_caller)
    assert "caller_a" in names and "caller_b" in names
    assert names["caller_b"] > names["caller_a"]  # caller_b потратил больше


def test_summary_excludes_days_outside_window(tmp_path, monkeypatch):
    fake_path = tmp_path / "state_usage.json"
    monkeypatch.setattr(ut, "USAGE_PATH", str(fake_path))

    import json as json_mod
    old_day = "2020-01-01"  # заведомо вне любого разумного окна
    data = {"daily": {old_day: {
        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1_000_000,
        "completion_tokens": 0, "calls": 5, "cost_usd": 0.22,
        "by_caller": {"old_caller": {"calls": 5, "cost_usd": 0.22}},
    }}}
    fake_path.write_text(json_mod.dumps(data), encoding="utf-8")

    total_cost, total_calls, by_caller = ut.summary(days=7)
    assert total_calls == 0
    assert total_cost == 0.0
    assert by_caller == []


def test_summary_no_file_returns_zeros(tmp_path, monkeypatch):
    fake_path = tmp_path / "nonexistent.json"
    monkeypatch.setattr(ut, "USAGE_PATH", str(fake_path))
    total_cost, total_calls, by_caller = ut.summary(days=7)
    assert total_cost == 0.0
    assert total_calls == 0
    assert by_caller == []


def test_record_usage_corrupt_file_recovers(tmp_path, monkeypatch):
    fake_path = tmp_path / "state_usage.json"
    fake_path.write_text("not valid json{{{", encoding="utf-8")
    monkeypatch.setattr(ut, "USAGE_PATH", str(fake_path))

    usage = {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 0, "completion_tokens": 0}
    ut.record_usage("caller", usage)  # не должно упасть

    total_cost, total_calls, _ = ut.summary(days=7)
    assert total_calls == 1
