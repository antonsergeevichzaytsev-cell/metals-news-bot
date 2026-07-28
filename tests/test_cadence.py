"""Прямые тесты для cadence.py — единого источника правды, вынесенного
28.07.2026 из трёх независимых копий (pipeline_sync.py, mission_control.py,
weekly_check.py). Регрессия здесь ловится один раз, а не в трёх местах.
"""
import sys

sys.path.insert(0, "..")
import cadence as cd


# --- is_cadence_exhausted ------------------------------------------------

def test_cadence_exhausted_true_when_over_threshold():
    assert cd.is_cadence_exhausted("sent_no_reply", 22) is True


def test_cadence_exhausted_false_within_threshold():
    assert cd.is_cadence_exhausted("sent_no_reply", 20) is False


def test_cadence_exhausted_false_at_exact_boundary():
    # > MAX_SILENCE, не >=  — ровно 21 ещё не исчерпана
    assert cd.is_cadence_exhausted("sent_no_reply", 21) is False


def test_cadence_exhausted_follow_up_overdue_also_tracked():
    assert cd.is_cadence_exhausted("follow_up_overdue", 25) is True


def test_cadence_exhausted_false_for_untracked_status():
    # won, reply_received, dead и т.п. — каденция для них не считается,
    # даже если формально было бы за порогом
    assert cd.is_cadence_exhausted("won", 100) is False
    assert cd.is_cadence_exhausted("reply_received", 100) is False
    assert cd.is_cadence_exhausted("dead", 100) is False


# --- is_dead ---------------------------------------------------------------
# Регрессионный набор — идентичен test_mission_control.py::test_is_dead_*,
# так как is_dead физически перенесена сюда без изменения логики.

def test_is_dead_explicit_dead_statuses():
    for status in ("dead", "closed", "declined", "done", "channel_failed"):
        assert cd.is_dead({"status": status}) is True, f"failed on {status!r}"


def test_is_dead_reply_received_never_dies_from_silence():
    assert cd.is_dead({"status": "reply_received", "silence_days": 9999}) is False


def test_is_dead_cadence_exhausted_status():
    assert cd.is_dead({"status": "sent_no_reply", "silence_days": 22}) is True


def test_is_dead_within_cadence_window():
    assert cd.is_dead({"status": "sent_no_reply", "silence_days": 10}) is False


def test_is_dead_won_never_dies():
    assert cd.is_dead({"status": "won", "silence_days": 999}) is False


def test_is_dead_unknown_status_defaults_alive():
    assert cd.is_dead({"status": "some_future_status"}) is False


def test_is_dead_missing_status_field():
    assert cd.is_dead({}) is False


# --- константы: значения зафиксированы явно, чтобы случайная правка ------
# числа в cadence.py сразу подсвечивалась упавшим тестом, а не тихо
# протекала в три бота одновременно.

def test_cadence_constants_have_expected_values():
    assert cd.CADENCE_MAX_SILENCE == 21
    assert cd.CADENCE_MAX_TOUCHES == 3
    assert cd.CADENCE_DUE_MIN == 4
    assert cd.DEAD_STATUSES == {"dead", "closed", "declined", "done", "channel_failed"}
    assert cd.CADENCE_TRACKED_STATUSES == ("sent_no_reply", "follow_up_overdue")
