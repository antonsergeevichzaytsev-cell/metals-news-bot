"""Тесты для net.py — retry/backoff обёрток над urlopen и IMAP-connect.

Никаких реальных сетевых вызовов: всё через unittest.mock. Проверяем три
сценария на каждой функции: восстановление после транзиентной ошибки,
быстрый отказ на постоянной ошибке (без лишних попыток), честное
исключение после исчерпания всех попыток.
"""
import imaplib
import sys
import urllib.error
from unittest import mock

import pytest

sys.path.insert(0, "..")
import net


# --- urlopen_retry -----------------------------------------------------

def test_urlopen_retry_recovers_after_transient_503():
    calls = {"n": 0}

    def fake_urlopen(req, timeout=20):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)
        return "SUCCESS"

    with mock.patch("net.urllib.request.urlopen", side_effect=fake_urlopen), \
         mock.patch("net.time.sleep", return_value=None):
        result = net.urlopen_retry("fake_req", max_attempts=3, base_delay=0.01)

    assert result == "SUCCESS"
    assert calls["n"] == 2


def test_urlopen_retry_fails_fast_on_404():
    calls = {"n": 0}

    def fake_urlopen(req, timeout=20):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)

    with mock.patch("net.urllib.request.urlopen", side_effect=fake_urlopen), \
         mock.patch("net.time.sleep", return_value=None):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            net.urlopen_retry("fake_req", max_attempts=3, base_delay=0.01)

    assert exc_info.value.code == 404
    assert calls["n"] == 1  # ни одной лишней попытки на постоянной ошибке


def test_urlopen_retry_raises_after_exhausting_attempts():
    calls = {"n": 0}

    def fake_urlopen(req, timeout=20):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)

    with mock.patch("net.urllib.request.urlopen", side_effect=fake_urlopen), \
         mock.patch("net.time.sleep", return_value=None):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            net.urlopen_retry("fake_req", max_attempts=3, base_delay=0.01)

    assert exc_info.value.code == 503
    assert calls["n"] == 3  # все попытки использованы, не меньше и не больше


def test_urlopen_retry_recovers_after_url_error():
    calls = {"n": 0}

    def fake_urlopen(req, timeout=20):
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.URLError("timed out")
        return "SUCCESS"

    with mock.patch("net.urllib.request.urlopen", side_effect=fake_urlopen), \
         mock.patch("net.time.sleep", return_value=None):
        result = net.urlopen_retry("fake_req", max_attempts=3, base_delay=0.01)

    assert result == "SUCCESS"
    assert calls["n"] == 2


# --- imap_connect_retry --------------------------------------------------

def test_imap_connect_retry_fails_fast_on_bad_credentials():
    calls = {"n": 0}

    class FakeM:
        def login(self, u, p):
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    def fake_imap4ssl(host, port, timeout=30):
        calls["n"] += 1
        return FakeM()

    with mock.patch("net.imaplib.IMAP4_SSL", side_effect=fake_imap4ssl), \
         mock.patch("net.time.sleep", return_value=None):
        with pytest.raises(imaplib.IMAP4.error):
            net.imap_connect_retry("imap.gmail.com", 993, "u", "p", max_attempts=3, base_delay=0.01)

    assert calls["n"] == 1  # неверный пароль — не транзиент, повтор бессмысленен


def test_imap_connect_retry_recovers_after_transient_oserror():
    calls = {"n": 0}

    class FakeM:
        def login(self, u, p):
            return True

    def fake_imap4ssl(host, port, timeout=30):
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("Connection reset by peer")
        return FakeM()

    with mock.patch("net.imaplib.IMAP4_SSL", side_effect=fake_imap4ssl), \
         mock.patch("net.time.sleep", return_value=None):
        M = net.imap_connect_retry("imap.gmail.com", 993, "u", "p", max_attempts=3, base_delay=0.01)

    assert M is not None
    assert calls["n"] == 2


def test_imap_connect_retry_raises_after_exhausting_attempts():
    def fake_imap4ssl(host, port, timeout=30):
        raise OSError("Connection reset by peer")

    with mock.patch("net.imaplib.IMAP4_SSL", side_effect=fake_imap4ssl), \
         mock.patch("net.time.sleep", return_value=None):
        with pytest.raises(OSError):
            net.imap_connect_retry("imap.gmail.com", 993, "u", "p", max_attempts=3, base_delay=0.01)
