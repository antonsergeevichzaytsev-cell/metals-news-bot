#!/usr/bin/env python3
"""
Inbox consolidator for 8 expert platforms.
Reads Gmail via IMAP, groups by platform, classifies urgent items, posts to Telegram.
"""
import imaplib
import email
import json
import os
import re
import hashlib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
import urllib.request
import urllib.parse

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

STATE_PATH = "state_inbox.json"
PLATFORMS_PATH = "platforms.json"
WINDOW_HOURS = 12
MAX_FETCH = 200


def load_platforms():
    with open(PLATFORMS_PATH) as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": [], "urgent_seen": []}


def save_state(state):
    state["seen"] = state["seen"][-500:]
    state["urgent_seen"] = state["urgent_seen"][-200:]
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def decode_subject(raw):
    if not raw:
        return ""
    parts = decode_header(raw)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                text = text.decode(enc or "utf-8", errors="replace")
            except (LookupError, TypeError):
                text = text.decode("utf-8", errors="replace")
        out += text
    return " ".join(out.split()).strip()


def extract_email(addr):
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", addr or "")
    return m.group(0).lower() if m else ""


def domain_of(addr):
    return addr.split("@", 1)[1].lower() if "@" in addr else ""


def match_platform(sender_domain, cfg):
    if not sender_domain:
        return None
    for p in cfg["platforms"]:
        for d in p["domains"]:
            d = d.lower()
            if sender_domain == d or sender_domain.endswith("." + d):
                return p["name"]
    return None


def is_urgent(subject, keywords):
    s = subject.lower()
    return any(kw in s for kw in keywords)


def msg_id_hash(msg):
    mid = (msg.get("Message-ID") or "").strip()
    if mid:
        return hashlib.sha1(mid.encode()).hexdigest()[:16]
    raw = (msg.get("Subject", "") + msg.get("From", "") + msg.get("Date", "")).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def fetch_emails(window_hours):
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    M.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    M.select("INBOX", readonly=True)
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'(SINCE "{since}")')
    msgs = []
    if typ == "OK" and data and data[0]:
        ids = data[0].split()
        for num in ids[-MAX_FETCH:]:
            typ, msg_data = M.fetch(num, "(BODY.PEEK[HEADER])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            try:
                msgs.append(email.message_from_bytes(msg_data[0][1]))
            except Exception:
                continue
    try:
        M.close()
    except Exception:
        pass
    try:
        M.logout()
    except Exception:
        pass
    return msgs


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print(f"TG error: {e}", file=sys.stderr)
        return False


def msk_time():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")


def main():
    cfg = load_platforms()
    state = load_state()
    seen = set(state["seen"])
    urgent_seen = set(state["urgent_seen"])
    msgs = fetch_emails(WINDOW_HOURS)
    print(f"Fetched {len(msgs)} message headers (window {WINDOW_HOURS}h)")

    by_platform = {p["name"]: [] for p in cfg["platforms"]}
    new_urgent = []
    total_new = 0

    for msg in msgs:
        sender_email = extract_email(msg.get("From", ""))
        sender_domain = domain_of(sender_email)
        platform = match_platform(sender_domain, cfg)
        if not platform:
            continue
        mid = msg_id_hash(msg)
        if mid in seen:
            continue
        subject = decode_subject(msg.get("Subject", ""))
        urgent = is_urgent(subject, cfg["urgent_keywords"])
        item = {
            "id": mid,
            "platform": platform,
            "subject": (subject[:140] if subject else "(no subject)"),
            "sender": sender_email,
            "urgent": urgent,
        }
        by_platform[platform].append(item)
        seen.add(mid)
        total_new += 1
        if urgent and mid not in urgent_seen:
            new_urgent.append(item)
            urgent_seen.add(mid)

    lines = [f"📥 <b>Inbox {msk_time()} MSK</b>"]
    lines.append("─" * 18)
    any_content = False
    for p in cfg["platforms"]:
        name = p["name"]
        items = by_platform[name]
        count = len(items)
        if count > 0:
            any_content = True
            icon = "🔥" if any(i["urgent"] for i in items) else "•"
            lines.append(f"{icon} <b>{esc(name)}</b> ({count})")
            for it in items[:3]:
                u = " ⚡" if it["urgent"] else ""
                lines.append(f"    {esc(it['subject'])}{u}")
            if count > 3:
                lines.append(f"    +{count - 3} more")
    if not any_content:
        lines.append("✓ Нет новых писем с платформ")
    lines.append("─" * 18)
    lines.append(f"Total new: <b>{total_new}</b>")
    lines.append('<a href="https://mail.google.com/mail/u/0/#inbox">Open Gmail</a>')
    summary = "\n".join(lines)

    for it in new_urgent:
        urgent_text = (
            f"⚡ <b>URGENT — {esc(it['platform'])}</b>\n"
            f"<b>{esc(it['subject'])}</b>\n"
            f"<i>From: {esc(it['sender'])}</i>\n\n"
            f'<a href="https://mail.google.com/mail/u/0/#inbox">Open Gmail</a>'
        )
        tg_send(urgent_text)
        time.sleep(0.5)

    tg_send(summary)

    state["seen"] = list(seen)
    state["urgent_seen"] = list(urgent_seen)
    save_state(state)
    print(f"Done. Total new: {total_new}, urgent: {len(new_urgent)}")


if __name__ == "__main__":
    main()
