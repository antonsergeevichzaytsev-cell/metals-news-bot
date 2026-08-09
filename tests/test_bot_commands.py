import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "12345")
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

import bot_commands as bc  # noqa: E402


def make_update(text, chat_id="12345"):
    return {"message": {"chat": {"id": int(chat_id)}, "text": text}}


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
