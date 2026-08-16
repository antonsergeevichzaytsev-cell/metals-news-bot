"""Общий модуль retry/backoff для HTTP, IMAP и SMTP — используется всеми ботами.

Принцип: drop-in замена голых вызовов, поведение при исчерпании попыток
не меняется (то же исключение улетает наверх, как и раньше). Ретраятся
только переходные ошибки (5xx, timeout, connection reset) — 4xx (Bad
credentials, 403 и т.п.) ретраить бессмысленно, они не пройдут и на
второй раз, только тратят время и шумят в логах.

История: до 28.07.2026 ни один бот не переживал временный сбой
Gmail/Telegram/DeepSeek — любой transient error валил весь прогон,
следующая попытка только по расписанию (до 24ч простоя на некоторых
ботах). Retry здесь не чинит логические баги (для этого — static_check.yml
и разметка), только сетевую хрупкость.

smtp_send_retry (16.08.2026) добавлен для bot_commands.cmd_weekly — до
этого GMAIL_USER/GMAIL_APP_PASSWORD использовались только для чтения
(imap_connect_retry в inbox.py/mission_control.py), не для отправки.
"""
import imaplib
import random
import time
import urllib.error
import urllib.request


RETRIABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def urlopen_retry(req, timeout=20, max_attempts=3, base_delay=2):
    """Drop-in замена urllib.request.urlopen(req, timeout=...).

    Ретраит только переходные ошибки:
    - HTTPError с кодом из RETRIABLE_HTTP_CODES (429/5xx)
    - URLError (timeout, connection refused/reset, DNS blip)

    НЕ ретраит:
    - HTTPError с 4xx кроме 429 (401/403/404 — повтор не поможет)

    После исчерпания попыток пробрасывает последнее исключение —
    вызывающий код ловит его точно так же, как ловил бы от голого
    urlopen. Ничего в существующих try/except менять не нужно.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code not in RETRIABLE_HTTP_CODES or attempt == max_attempts:
                raise
        except urllib.error.URLError as e:
            last_exc = e
            if attempt == max_attempts:
                raise
        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
        print(f"  ! retry {attempt}/{max_attempts} after {type(last_exc).__name__}: "
              f"{last_exc} — waiting {delay:.1f}s")
        time.sleep(delay)
    raise last_exc  # недостижимо на практике, страховка от логической дыры


def imap_connect_retry(host, port, user, password, max_attempts=3, base_delay=3):
    """Drop-in замена:
        M = imaplib.IMAP4_SSL(host, port, timeout=30)
        M.login(user, password)

    Ретраит IMAP4.error и OSError (сетевые обрывы) на connect+login.
    НЕ ретраит на login, если ошибка похожа на неверный пароль/логин —
    это не транзиент, повтор только тратит время и может словить
    временный бан за брутфорс-подобное поведение у Gmail.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            M = imaplib.IMAP4_SSL(host, port, timeout=30)
            M.login(user, password)
            return M
        except imaplib.IMAP4.error as e:
            last_exc = e
            msg = str(e).lower()
            if "invalid credentials" in msg or "authenticationfailed" in msg:
                raise  # не транзиент — повтор не поможет
            if attempt == max_attempts:
                raise
        except OSError as e:
            last_exc = e
            if attempt == max_attempts:
                raise
        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
        print(f"  ! IMAP retry {attempt}/{max_attempts} after {type(last_exc).__name__}: "
              f"{last_exc} — waiting {delay:.1f}s")
        time.sleep(delay)
    raise last_exc


def smtp_send_retry(host, port, user, password, msg, max_attempts=3, base_delay=3):
    """Симметрично imap_connect_retry, но для отправки: коннект+login+send
    в одном вызове, не выдаёт хендл наружу (SMTP-сессия короткоживущая,
    в отличие от IMAP, который держат открытым для нескольких операций).

    msg — готовый email.message.EmailMessage или MIMEMultipart с уже
    выставленными From/To/Subject; эта функция только доставляет.
    """
    import smtplib

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
            return
        except smtplib.SMTPAuthenticationError:
            raise  # не транзиент — повтор не поможет
        except (smtplib.SMTPException, OSError) as e:
            last_exc = e
            if attempt == max_attempts:
                raise
        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
        print(f"  ! SMTP retry {attempt}/{max_attempts} after {type(last_exc).__name__}: "
              f"{last_exc} — waiting {delay:.1f}s")
        time.sleep(delay)
    raise last_exc
