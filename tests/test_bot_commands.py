import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

import bot_commands as bc  # noqa: E402


def make_update(text, chat_id="test"):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


# --- Authorization -----------------------------------------------------------

def test_authorized_chat_dispatches():
    sent = []
    with patch.dict(bc.COMMANDS, {"/help": lambda arg: sent.append("help")}):
        bc.handle_update(make_update("/help"))
    assert sent == ["help"]


def test_unauthorized_chat_ignored():
    with patch.object(bc, "tg_send") as mock_send:
        bc.handle_update(make_update("/help", chat_id="99999"))
        mock_send.assert_not_called()


def test_no_message_in_update_ignored():
    with patch.object(bc, "tg_send") as mock_send:
        bc.handle_update({"callback_query": {"data": "x"}})
        mock_send.assert_not_called()


def test_non_command_text_ignored():
    with patch.object(bc, "tg_send") as mock_send:
        bc.handle_update(make_update("just chatting, not a command"))
        mock_send.assert_not_called()


def test_unknown_command_replies_with_hint():
    with patch.object(bc, "tg_send") as mock_send:
        bc.handle_update(make_update("/nonexistent"))
        mock_send.assert_called_once()
        assert "Неизвестная команда" in mock_send.call_args[0][0]


# --- Command parsing -----------------------------------------------------

def test_command_with_botname_suffix_stripped():
    # Group chats append @botname to commands: /help@antonmining_bot
    calls = []
    with patch.dict(bc.COMMANDS, {"/help": lambda arg: calls.append(arg)}):
        bc.handle_update(make_update("/help@antonmining_bot"))
        assert calls == [""]


def test_command_arg_split_correctly():
    calls = []
    with patch.dict(bc.COMMANDS, {"/company": lambda arg: calls.append(arg)}):
        bc.handle_update(make_update("/company Almalyk MMC"))
        assert calls == ["Almalyk MMC"]


# --- cmd_company -----------------------------------------------------------

def test_cmd_company_empty_arg_prompts_format():
    with patch.object(bc, "tg_send") as mock_send:
        bc.cmd_company("")
        mock_send.assert_called_once()
        assert "Формат" in mock_send.call_args[0][0]


def test_cmd_company_no_matches():
    with patch.object(bc, "load_json", return_value={"items": []}):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_company("Nonexistent Co")
            assert "ничего" in mock_send.call_args[0][0]


def test_cmd_company_filters_by_recency_and_name():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).isoformat()
    old = (now - timedelta(days=10)).isoformat()
    items = {
        "items": [
            {"ts": recent, "company": "Almalyk MMC", "title": "Recent news", "link": "http://x", "why": "", "priority": "high"},
            {"ts": old, "company": "Almalyk MMC", "title": "Old news", "link": "http://y", "why": "", "priority": "low"},
            {"ts": recent, "company": "Other Co", "title": "Unrelated", "link": "http://z", "why": "", "priority": "low"},
        ]
    }
    with patch.object(bc, "load_json", return_value=items):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_company("Almalyk")
            text = mock_send.call_args[0][0]
            assert "Recent news" in text
            assert "Old news" not in text  # outside 7-day window
            assert "Unrelated" not in text  # different company


# --- cmd_why -----------------------------------------------------------

def test_cmd_why_non_numeric_arg():
    with patch.object(bc, "tg_send") as mock_send:
        bc.cmd_why("abc")
        assert "Формат" in mock_send.call_args[0][0]


def test_cmd_why_no_last_digest():
    with patch.object(bc, "load_json", return_value={"items": []}):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_why("1")
            assert "Нет данных" in mock_send.call_args[0][0]


def test_cmd_why_out_of_range():
    with patch.object(bc, "load_json", return_value={"items": [{"title": "x", "link": "y"}]}):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_why("5")
            assert "вне диапазона" in mock_send.call_args[0][0]


def test_cmd_why_valid_index_with_deep():
    data = {
        "items": [
            {
                "title": "Test headline",
                "link": "http://example.com",
                "company": "TestCo",
                "why": "matters because X",
                "deep": {"what": "A happened", "who": "TestCo affected", "action": "check Y"},
            }
        ]
    }
    with patch.object(bc, "load_json", return_value=data):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_why("1")
            text = mock_send.call_args[0][0]
            assert "Test headline" in text
            assert "A happened" in text
            assert "check Y" in text


def test_cmd_why_valid_index_without_deep():
    data = {"items": [{"title": "Test", "link": "http://x", "why": "reason"}]}
    with patch.object(bc, "load_json", return_value=data):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_why("1")
            text = mock_send.call_args[0][0]
            assert "углублённого разбора нет" in text


# --- cmd_status --------------------------------------------------------

def test_cmd_status_no_state():
    with patch.object(bc, "load_json", return_value={}):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_status()
            assert "Нет данных" in mock_send.call_args[0][0]


def test_cmd_status_with_last_run():
    state = {
        "last_run": {
            "ts": "2026-08-09T00:00:00Z",
            "raw": 100,
            "candidates": 20,
            "enriched": 12,
            "feeds_broken": 2,
            "feeds_total": 30,
            "broken": {"feed-a": "timeout"},
        }
    }
    with patch.object(bc, "load_json", return_value=state):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_status()
            text = mock_send.call_args[0][0]
            assert "2026-08-09T00:00:00Z" in text
            assert "feed-a" in text


# --- cmd_orbit -----------------------------------------------------------

def test_cmd_orbit_no_matches():
    with patch.object(bc, "load_json", return_value={"items": []}):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_orbit("")
            assert "ничего не найдено" in mock_send.call_args[0][0]


def test_cmd_orbit_filters_by_uzcopper_tag_and_recency():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    items = {
        "items": [
            {"ts": recent, "uzcopper": True, "title": "In orbit recent", "link": "http://a", "priority": "high"},
            {"ts": old, "uzcopper": True, "title": "In orbit old", "link": "http://b", "priority": "low"},
            {"ts": recent, "uzcopper": False, "title": "Not in orbit", "link": "http://c", "priority": "low"},
        ]
    }
    with patch.object(bc, "load_json", return_value=items):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_orbit("7")
            text = mock_send.call_args[0][0]
            assert "In orbit recent" in text
            assert "In orbit old" not in text  # outside default window
            assert "Not in orbit" not in text  # not tagged


def test_cmd_orbit_custom_days_argument():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    ts15 = (now - timedelta(days=15)).isoformat()
    items = {"items": [{"ts": ts15, "uzcopper": True, "title": "15 days old", "link": "http://x", "priority": "low"}]}
    with patch.object(bc, "load_json", return_value=items):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_orbit("20")
            assert "15 days old" in mock_send.call_args[0][0]


def test_cmd_orbit_days_argument_capped_at_30():
    calls = []
    with patch.object(bc, "load_json", side_effect=lambda *a, **k: calls.append(1) or {"items": []}):
        with patch.object(bc, "tg_send"):
            bc.cmd_orbit("999")  # should not crash, should clamp internally
    assert len(calls) == 1


# --- cmd_search ----------------------------------------------------------

def test_cmd_search_empty_arg_prompts_format():
    with patch.object(bc, "tg_send") as mock_send:
        bc.cmd_search("")
        assert "Формат" in mock_send.call_args[0][0]


def test_cmd_search_no_matches():
    with patch.object(bc, "load_json", return_value={"items": []}):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_search("nonexistent term")
            assert "не найдено" in mock_send.call_args[0][0]


def test_cmd_search_matches_across_fields():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    items = {
        "items": [
            {"ts": recent, "title": "Smelter restart in Chile", "desc": "", "why": "", "company": "", "link": "http://a", "priority": "high"},
            {"ts": recent, "title": "Unrelated copper news", "desc": "mentions smelter briefly", "why": "", "company": "", "link": "http://b", "priority": "low"},
            {"ts": old, "title": "Old smelter news", "desc": "", "why": "", "company": "", "link": "http://c", "priority": "low"},
        ]
    }
    with patch.object(bc, "load_json", return_value=items):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_search("smelter")
            text = mock_send.call_args[0][0]
            assert "Smelter restart in Chile" in text
            assert "Unrelated copper news" in text  # matched via desc field
            assert "Old smelter news" not in text  # outside 14-day window


def test_cmd_search_case_insensitive():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    items = {"items": [{"ts": now, "title": "COPPER Prices Rise", "desc": "", "why": "", "company": "", "link": "http://a", "priority": "low"}]}
    with patch.object(bc, "load_json", return_value=items):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_search("copper")
            assert "COPPER Prices Rise" in mock_send.call_args[0][0]


# --- cmd_feeds -------------------------------------------------------------

def test_cmd_feeds_no_state():
    with patch.object(bc, "load_json", return_value={}):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_feeds()
            assert "Нет данных" in mock_send.call_args[0][0]


def test_cmd_feeds_all_healthy():
    state = {"last_run": {"feeds_total": 35, "broken": {}}}
    with patch.object(bc, "load_json", return_value=state):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_feeds()
            text = mock_send.call_args[0][0]
            assert "35/35" in text
            assert "Все источники рабочие" in text


def test_cmd_feeds_some_broken():
    state = {"last_run": {"feeds_total": 35, "broken": {"feed-x": "0 items", "feed-y": "HTTP 403"}}}
    with patch.object(bc, "load_json", return_value=state):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_feeds()
            text = mock_send.call_args[0][0]
            assert "33/35" in text
            assert "feed-x" in text
            assert "feed-y" in text


# --- cmd_deep --------------------------------------------------------------

def test_cmd_deep_non_numeric_arg():
    with patch.object(bc, "tg_send") as mock_send:
        bc.cmd_deep("abc")
        assert "Формат" in mock_send.call_args[0][0]


def test_cmd_deep_no_last_digest():
    with patch.object(bc, "load_json", return_value={"items": []}):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_deep("1")
            assert "Нет данных" in mock_send.call_args[0][0]


def test_cmd_deep_out_of_range():
    with patch.object(bc, "load_json", return_value={"items": [{"title": "x"}]}):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_deep("9")
            assert "вне диапазона" in mock_send.call_args[0][0]


def test_cmd_deep_already_has_analysis():
    data = {"items": [{"title": "x", "deep": {"what": "already done"}}]}
    with patch.object(bc, "load_json", return_value=data):
        with patch.object(bc, "tg_send") as mock_send:
            bc.cmd_deep("1")
            assert "уже есть разбор" in mock_send.call_args[0][0]
            assert "/why 1" in mock_send.call_args[0][0]


def test_cmd_deep_calls_deepseek_and_sends_result():
    import types
    data = {"items": [{"title": "New smelter news", "link": "http://x", "desc": "d", "why": "w", "company": "c"}]}
    fake_digest = types.ModuleType("digest")
    fake_digest.find_similar_history = lambda *a, **k: []
    fake_digest.deepseek_deep_analysis = lambda *a, **k: {
        "what": "smelter opened", "who": "local producers", "action": "verify capacity",
        "trend": "escalates prior capacity concerns",
    }
    with patch.object(bc, "load_json", return_value=data):
        with patch.dict("sys.modules", {"digest": fake_digest}):
            with patch.object(bc, "tg_send") as mock_send:
                bc.cmd_deep("1")
                calls = [c[0][0] for c in mock_send.call_args_list]
                combined = "\n".join(calls)
                assert "smelter opened" in combined
                assert "verify capacity" in combined
                assert "escalates prior capacity concerns" in combined


def test_cmd_deep_deepseek_returns_none():
    import types
    data = {"items": [{"title": "x", "link": "http://x", "desc": "", "why": "", "company": ""}]}
    fake_digest = types.ModuleType("digest")
    fake_digest.find_similar_history = lambda *a, **k: []
    fake_digest.deepseek_deep_analysis = lambda *a, **k: None
    with patch.object(bc, "load_json", return_value=data):
        with patch.dict("sys.modules", {"digest": fake_digest}):
            with patch.object(bc, "tg_send") as mock_send:
                bc.cmd_deep("1")
                calls = [c[0][0] for c in mock_send.call_args_list]
                assert any("не ответил" in c for c in calls)


# --- cmd_weekly --------------------------------------------------------

def test_cmd_weekly_no_gmail_creds():
    import os as os_mod
    with patch.dict(os_mod.environ, {}, clear=False):
        old_user = os_mod.environ.pop("GMAIL_USER", None)
        old_pass = os_mod.environ.pop("GMAIL_APP_PASSWORD", None)
        try:
            with patch.object(bc, "tg_send") as mock_send:
                bc.cmd_weekly("")
                assert "недоступна" in mock_send.call_args[0][0]
        finally:
            if old_user is not None:
                os_mod.environ["GMAIL_USER"] = old_user
            if old_pass is not None:
                os_mod.environ["GMAIL_APP_PASSWORD"] = old_pass


def test_cmd_weekly_empty_history():
    with patch.dict(os.environ, {"GMAIL_USER": "u@x.com", "GMAIL_APP_PASSWORD": "p"}):
        with patch.object(bc, "load_json", return_value={"items": []}):
            with patch.object(bc, "tg_send") as mock_send:
                bc.cmd_weekly("")
                assert "нечего собрать" in mock_send.call_args[0][0]


def test_cmd_weekly_sends_email_with_correct_counts():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=10)).isoformat()
    items = {
        "items": [
            {"ts": recent, "priority": "high", "uzcopper": False, "title": "High item", "link": "http://a", "why": "w"},
            {"ts": recent, "priority": "low", "uzcopper": True, "title": "Orbit item", "link": "http://b", "why": ""},
            {"ts": old, "priority": "high", "uzcopper": True, "title": "Too old", "link": "http://c", "why": ""},
        ]
    }
    with patch.dict(os.environ, {"GMAIL_USER": "me@x.com", "GMAIL_APP_PASSWORD": "p"}):
        with patch.object(bc, "load_json", return_value=items):
            with patch.object(bc, "tg_send") as mock_send:
                with patch.object(bc.net, "smtp_send_retry") as mock_smtp:
                    bc.cmd_weekly("")
                    mock_smtp.assert_called_once()
                    # verify the email message content
                    sent_msg = mock_smtp.call_args[0][4]
                    body = sent_msg.get_content()
                    assert "High item" in body
                    assert "Orbit item" in body
                    assert "Too old" not in body  # outside 7-day window
                    final_text = mock_send.call_args_list[-1][0][0]
                    assert "1 high-priority" in final_text
                    assert "1 в UZCOPPER" in final_text


def test_cmd_weekly_smtp_failure_reports_error():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    items = {"items": [{"ts": now, "priority": "high", "uzcopper": False, "title": "x", "link": "http://a", "why": ""}]}
    with patch.dict(os.environ, {"GMAIL_USER": "me@x.com", "GMAIL_APP_PASSWORD": "p"}):
        with patch.object(bc, "load_json", return_value=items):
            with patch.object(bc, "tg_send") as mock_send:
                with patch.object(bc.net, "smtp_send_retry", side_effect=OSError("network down")):
                    bc.cmd_weekly("")
                    final_text = mock_send.call_args_list[-1][0][0]
                    assert "Не удалось отправить" in final_text
