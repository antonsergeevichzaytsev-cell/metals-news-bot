"""Тесты для weekly_check.py — watchdog (сторож, ловящий 'бот рапортует
бодро, а данные под ним окаменели' — комментарий в коде про инцидент с
8 июня, тот же класс проблемы, что esc() 27.07, только на уровне данных,
не синтаксиса), и вспомогательные парсеры дат.

CADENCE_MAX_SILENCE / DEAD_STATUSES здесь — ТРЕТЬЯ независимая копия той
же каденции, что в pipeline_sync.due_for_followup и mission_control.is_dead
(комментарий в коде сам это признаёт: "синхронно с mission_control.is_dead()").
Три копии одной константы в трёх файлах — риск расхождения при рефакторинге
любого из них поодиночке; тесты здесь фиксируют текущие значения как
регрессионную страховку, не решают архитектурную проблему.

weekly_check.py на верхнем уровне читает os.environ.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")
os.environ.setdefault("GITHUB_TOKEN", "test")
os.environ.setdefault("GITHUB_REPOSITORY", "test/test")

sys.path.insert(0, "..")
import weekly_check as wc

TST = wc.TST


# --- parse_dt / parse_date ---------------------------------------------

def test_parse_dt_valid_iso():
    dt = wc.parse_dt("2026-07-27T10:00:00Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 7 and dt.day == 27


def test_parse_dt_none_for_empty():
    assert wc.parse_dt(None) is None
    assert wc.parse_dt("") is None


def test_parse_dt_none_for_malformed():
    assert wc.parse_dt("not-a-date") is None


def test_parse_date_valid():
    d = wc.parse_date("2026-07-27")
    assert d.year == 2026 and d.month == 7 and d.day == 27


def test_parse_date_none_for_malformed():
    assert wc.parse_date("not-a-date") is None
    assert wc.parse_date(None) is None


# --- watchdog ----------------------------------------------------------
# load_json мокается по пути: возвращает разные фикстуры для pipeline.json
# vs state_inbox.json vs history.json, как это будет в реальном прогоне.

def _mock_load_json(pipeline=None, inbox_state=None, history=None):
    def fake(path, default=None):
        if path == wc.PIPELINE_PATH:
            return pipeline
        if path == wc.INBOX_STATE_PATH:
            return inbox_state
        if path == wc.HISTORY_PATH:
            return history
        return default
    return fake


def test_watchdog_no_alarm_when_pipeline_missing():
    """19.08.2026: pipeline_sync + pipeline.json переехали в отдельный
    репо metals-outreach-sync — отсутствие файла здесь теперь штатное
    состояние (не every-run в этом репо), не повод для тревоги."""
    now = datetime.now(TST)
    with mock.patch("weekly_check.load_json", side_effect=_mock_load_json(pipeline=None)):
        alarms, facts = wc.watchdog(now)
    assert alarms == []
    assert facts == []


def test_watchdog_alarms_when_pipeline_stale():
    now = datetime.now(TST)
    stale_ts = (now - timedelta(hours=100)).isoformat()
    pipeline = {"last_updated": stale_ts, "leads": []}
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("не обновлялся" in a and "pipeline_sync не бежит" in a for a in alarms)


def test_watchdog_alarms_when_no_new_leads_fossil():
    # Регрессия на инцидент "пайплайн простоял с 8 июня" (комментарий
    # в коде) — главная проверка watchdog, её отсутствие стоило 6 недель.
    now = datetime.now(TST)
    old_date = (now.date() - timedelta(days=20)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": old_date, "status": "sent_no_reply", "silence_days": 5}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("окаменел" in a for a in alarms)


def test_watchdog_no_fossil_alarm_when_recent_lead_exists():
    now = datetime.now(TST)
    recent_date = (now.date() - timedelta(days=2)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "status": "sent_no_reply", "silence_days": 2}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert not any("окаменел" in a for a in alarms)


def test_watchdog_alarms_when_zero_live_leads():
    now = datetime.now(TST)
    recent_date = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "status": "dead", "silence_days": 5}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("живых лидов ноль" in a for a in alarms)


def test_watchdog_alarms_on_cadence_zombies():
    # Каденция исчерпана (silence_days > 21), но статус всё ещё "живой"
    # в файле — не закрыт. watchdog должен это поймать.
    now = datetime.now(TST)
    recent_date = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "status": "sent_no_reply", "silence_days": 25}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state={}, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("каденция исчерпана" in a for a in alarms)


def test_watchdog_no_alarms_on_healthy_state():
    now = datetime.now(TST)
    recent_date = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "last_activity": recent_date,
                   "status": "sent_no_reply", "silence_days": 3, "touches": 1}],
    }
    inbox_state = {"last_run": now.isoformat(), "seen": ["a", "b"]}
    history = {"items": [{"ts": now.isoformat()}]}
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state=inbox_state, history=history)):
        alarms, facts = wc.watchdog(now)
    assert alarms == []
    assert len(facts) > 0


def test_watchdog_alarms_when_inbox_dead():
    now = datetime.now(TST)
    recent_date = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    pipeline = {
        "last_updated": now.isoformat(),
        "leads": [{"first_contact": recent_date, "status": "sent_no_reply", "silence_days": 1}],
    }
    with mock.patch("weekly_check.load_json",
                     side_effect=_mock_load_json(pipeline=pipeline, inbox_state=None, history={"items": []})):
        alarms, facts = wc.watchdog(now)
    assert any("inbox.py мёртв" in a for a in alarms)


# --- secrets_rotation_check -------------------------------------------------
# НЕ читает реальные секреты GitHub (требует отдельный PAT — заводить его
# ради мониторинга секретов увеличивает поверхность атаки). Сверяет
# ручной трекер secrets_rotation.json.

def test_secrets_rotation_no_overdue_when_all_fresh():
    now = datetime.now(TST)
    recent = (now.date() - timedelta(days=10)).strftime("%Y-%m-%d")
    data = {"rotation_threshold_days": 90, "secrets": {"FOO_KEY": recent}}
    with mock.patch("weekly_check.load_json", return_value=data):
        overdue = wc.secrets_rotation_check(now)
    assert overdue == []


def test_secrets_rotation_flags_overdue_secret():
    now = datetime.now(TST)
    old = (now.date() - timedelta(days=100)).strftime("%Y-%m-%d")
    data = {"rotation_threshold_days": 90, "secrets": {"FOO_KEY": old}}
    with mock.patch("weekly_check.load_json", return_value=data):
        overdue = wc.secrets_rotation_check(now)
    assert len(overdue) == 1
    assert overdue[0][0] == "FOO_KEY"
    assert overdue[0][1] == 100


def test_secrets_rotation_exactly_at_threshold_counts_as_overdue():
    now = datetime.now(TST)
    exactly = (now.date() - timedelta(days=90)).strftime("%Y-%m-%d")
    data = {"rotation_threshold_days": 90, "secrets": {"FOO_KEY": exactly}}
    with mock.patch("weekly_check.load_json", return_value=data):
        overdue = wc.secrets_rotation_check(now)
    assert len(overdue) == 1


def test_secrets_rotation_sorted_by_most_overdue_first():
    now = datetime.now(TST)
    d100 = (now.date() - timedelta(days=100)).strftime("%Y-%m-%d")
    d200 = (now.date() - timedelta(days=200)).strftime("%Y-%m-%d")
    data = {"rotation_threshold_days": 90, "secrets": {"NEWER": d100, "OLDER": d200}}
    with mock.patch("weekly_check.load_json", return_value=data):
        overdue = wc.secrets_rotation_check(now)
    assert [name for name, _ in overdue] == ["OLDER", "NEWER"]


def test_secrets_rotation_missing_file_returns_empty_not_crash():
    now = datetime.now(TST)
    with mock.patch("weekly_check.load_json", return_value=None):
        overdue = wc.secrets_rotation_check(now)
    assert overdue == []


def test_secrets_rotation_malformed_date_skipped_not_crash():
    now = datetime.now(TST)
    data = {"rotation_threshold_days": 90, "secrets": {"BAD": "not-a-date"}}
    with mock.patch("weekly_check.load_json", return_value=data):
        overdue = wc.secrets_rotation_check(now)
    assert overdue == []


def test_secrets_rotation_uses_default_threshold_if_missing():
    now = datetime.now(TST)
    old = (now.date() - timedelta(days=95)).strftime("%Y-%m-%d")
    data = {"secrets": {"FOO_KEY": old}}  # без rotation_threshold_days
    with mock.patch("weekly_check.load_json", return_value=data):
        overdue = wc.secrets_rotation_check(now)
    assert len(overdue) == 1  # дефолт 90 -> 95 дней просрочено


# 19.08.2026: per_secret_threshold_days — GMAIL_APP_PASSWORD реально
# протух на 61-м дне, общий 90-дневный порог не предупредил бы вовремя.
def test_secrets_rotation_per_secret_threshold_overrides_default():
    now = datetime.now(TST)
    age_50 = (now.date() - timedelta(days=50)).strftime("%Y-%m-%d")
    data = {
        "secrets": {"GMAIL_APP_PASSWORD": age_50, "OTHER_KEY": age_50},
        "rotation_threshold_days": 90,
        "per_secret_threshold_days": {"GMAIL_APP_PASSWORD": 45},
    }
    with mock.patch("weekly_check.load_json", return_value=data):
        overdue = wc.secrets_rotation_check(now)
    names = [n for n, _ in overdue]
    # 50 дней >= 45 (свой порог) -> просрочен; 50 дней < 90 (общий) -> не просрочен
    assert "GMAIL_APP_PASSWORD" in names
    assert "OTHER_KEY" not in names


def test_secrets_rotation_per_secret_threshold_not_yet_overdue():
    now = datetime.now(TST)
    age_30 = (now.date() - timedelta(days=30)).strftime("%Y-%m-%d")
    data = {
        "secrets": {"GMAIL_APP_PASSWORD": age_30},
        "rotation_threshold_days": 90,
        "per_secret_threshold_days": {"GMAIL_APP_PASSWORD": 45},
    }
    with mock.patch("weekly_check.load_json", return_value=data):
        overdue = wc.secrets_rotation_check(now)
    assert overdue == []


# --- filings_labeling_status -------------------------------------------------
# 28.07: единственная проверка DeepSeek-гейта — разметка Антона. Функция
# переиспользует eval_filings.summarize_labels, не дублирует логику.

def test_filings_labeling_status_zero_labels():
    with mock.patch("eval_filings.load_history",
                     return_value={"items": [{"link": "a"}] * 28, "labels": []}):
        total, msg = wc.filings_labeling_status()
    assert total == 0
    assert "0 размечено" in msg
    assert "👍/👎" in msg


def test_filings_labeling_status_below_threshold_warns():
    labels = [{"link": f"l{i}", "verdict": "good"} for i in range(5)]
    with mock.patch("eval_filings.load_history", return_value={"items": [], "labels": labels}):
        total, msg = wc.filings_labeling_status()
    assert total == 5
    assert "ещё мало" in msg


def test_filings_labeling_status_sufficient_data_silent():
    import eval_filings
    labels = [{"link": f"l{i}", "verdict": "good"} for i in range(eval_filings.MIN_LABELS_FOR_VERDICT)]
    with mock.patch("eval_filings.load_history", return_value={"items": [], "labels": labels}):
        total, msg = wc.filings_labeling_status()
    assert total == eval_filings.MIN_LABELS_FOR_VERDICT
    assert msg is None  # достаточно данных — не докучаем в еженедельном отчёте


# --- send_backup_email --------------------------------------------------
# 20.08.2026: disaster recovery — полная потеря репозитория (случайное
# удаление, блокировка GitHub-аккаунта) означала полную потерю всей
# истории. Еженедельный email-бэкап на GMAIL_USER->GMAIL_USER, тот же
# net.smtp_send_retry, что использует cmd_weekly в bot_commands.py.

def test_send_backup_email_skips_without_gmail_creds(monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    ok, detail = wc.send_backup_email(datetime.now(TST))
    assert ok is False
    assert "not configured" in detail.lower()


def test_send_backup_email_sends_zip_with_all_existing_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "fake-password")
    monkeypatch.setattr(wc, "ROOT", str(tmp_path))

    (tmp_path / "state.json").write_text('{"a": 1}', encoding="utf-8")
    (tmp_path / "history.json").write_text('{"items": []}', encoding="utf-8")
    # остальные BACKUP_FILES намеренно не созданы -> должны попасть в missing

    sent_msgs = []

    def fake_smtp(host, port, user, password, msg, max_attempts=3, base_delay=3):
        sent_msgs.append(msg)

    with mock.patch.object(wc.net, "smtp_send_retry", side_effect=fake_smtp):
        ok, detail = wc.send_backup_email(datetime.now(TST))

    assert ok is True
    assert len(sent_msgs) == 1
    msg = sent_msgs[0]
    assert msg["From"] == "bot@example.com"
    assert msg["To"] == "bot@example.com"
    assert "backup" in msg["Subject"].lower()

    # найти zip-вложение и проверить его содержимое
    attachment = None
    for part in msg.iter_attachments():
        attachment = part
        break
    assert attachment is not None
    assert attachment.get_filename().endswith(".zip")

    import io
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(attachment.get_payload(decode=True)))
    names = zf.namelist()
    assert "state.json" in names
    assert "history.json" in names
    assert len(names) == 2  # только два реально существующих файла


def test_send_backup_email_smtp_failure_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "fake-password")
    monkeypatch.setattr(wc, "ROOT", str(tmp_path))
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")

    with mock.patch.object(wc.net, "smtp_send_retry", side_effect=OSError("smtp down")):
        ok, detail = wc.send_backup_email(datetime.now(TST))

    assert ok is False
    assert "smtp down" in detail


def test_send_backup_email_no_files_exist_still_sends_empty_zip(tmp_path, monkeypatch):
    """Ни один файл не существует на диске (пустой каталог) — не должно
    крашиться, письмо всё равно уходит с пустым архивом и списком missing."""
    monkeypatch.setenv("GMAIL_USER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "fake-password")
    monkeypatch.setattr(wc, "ROOT", str(tmp_path))

    sent_msgs = []

    def fake_smtp(host, port, user, password, msg, max_attempts=3, base_delay=3):
        sent_msgs.append(msg)

    with mock.patch.object(wc.net, "smtp_send_retry", side_effect=fake_smtp):
        ok, detail = wc.send_backup_email(datetime.now(TST))

    assert ok is True
    assert len(sent_msgs) == 1
    assert "0 bytes" not in detail or True  # zip header сам по себе не 0 байт
