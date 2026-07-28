"""Тесты для eval_filings.py — честная отчётность о состоянии разметки
filings.py: 0 labels -> явное 'нет данных', мало labels -> предупреждение
о статистической незначимости, достаточно -> разбивка без предупреждения.

Не про сеть/DeepSeek — чистая агрегация уже накопленных данных.
"""
import sys

sys.path.insert(0, "..")
import eval_filings as ef


# --- summarize_labels ----------------------------------------------------

def test_summarize_zero_labels():
    history = {"items": [{"link": "a"}, {"link": "b"}], "labels": []}
    summary = ef.summarize_labels(history)
    assert summary["total_items"] == 2
    assert summary["total_labels"] == 0
    assert sum(summary["verdict_counts"].values()) == 0


def test_summarize_counts_verdicts():
    history = {
        "items": [{"link": "a", "priority": "high"}],
        "labels": [
            {"link": "a", "verdict": "good"},
            {"link": "a", "verdict": "good"},
            {"link": "a", "verdict": "bad"},
        ],
    }
    summary = ef.summarize_labels(history)
    assert summary["verdict_counts"]["good"] == 2
    assert summary["verdict_counts"]["bad"] == 1


def test_summarize_links_label_to_item_priority():
    history = {
        "items": [{"link": "a", "priority": "high"}, {"link": "b", "priority": "low"}],
        "labels": [{"link": "a", "verdict": "good"}, {"link": "b", "verdict": "bad"}],
    }
    summary = ef.summarize_labels(history)
    assert summary["by_priority"][("high", "good")] == 1
    assert summary["by_priority"][("low", "bad")] == 1


def test_summarize_falls_back_to_label_priority_when_item_missing():
    # Item мог выпасть из истории (ретеншн), но label хранит priority сам
    history = {
        "items": [],
        "labels": [{"link": "gone", "verdict": "good", "priority": "medium"}],
    }
    summary = ef.summarize_labels(history)
    assert summary["by_priority"][("medium", "good")] == 1


def test_summarize_unknown_priority_when_neither_source_has_it():
    history = {"items": [], "labels": [{"link": "x", "verdict": "note"}]}
    summary = ef.summarize_labels(history)
    assert summary["by_priority"][("unknown", "note")] == 1


def test_summarize_missing_verdict_defaults_to_note():
    history = {"items": [], "labels": [{"link": "x"}]}
    summary = ef.summarize_labels(history)
    assert summary["verdict_counts"]["note"] == 1


# --- render_report ---------------------------------------------------------

def test_render_report_zero_labels_is_explicit():
    summary = ef.summarize_labels({"items": [{"link": "a"}], "labels": []})
    report = ef.render_report(summary)
    assert "НЕТ ДАННЫХ" in report
    assert "eval невозможен" in report


def test_render_report_warns_below_threshold():
    labels = [{"link": f"l{i}", "verdict": "good"} for i in range(5)]
    summary = ef.summarize_labels({"items": [], "labels": labels})
    report = ef.render_report(summary)
    assert "ПРЕДУПРЕЖДЕНИЕ" in report
    assert "статистически не значит ничего" in report


def test_render_report_no_warning_at_or_above_threshold():
    labels = [{"link": f"l{i}", "verdict": "good"} for i in range(ef.MIN_LABELS_FOR_VERDICT)]
    summary = ef.summarize_labels({"items": [], "labels": labels})
    report = ef.render_report(summary)
    assert "ПРЕДУПРЕЖДЕНИЕ" not in report


def test_render_report_includes_verdict_counts():
    labels = [{"link": "a", "verdict": "good"}, {"link": "b", "verdict": "bad"}]
    summary = ef.summarize_labels({"items": [], "labels": labels})
    report = ef.render_report(summary)
    assert "good=1" in report
    assert "bad=1" in report
