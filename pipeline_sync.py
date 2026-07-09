#!/usr/bin/env python3
"""Pipeline sync v2 — detects real human replies, suppresses auto-notifications."""
import imaplib, email, json, os, re, hashlib, sys, time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
import urllib.request, urllib.parse

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

PIPELINE_PATH = "pipeline.json"
OUTREACH_DOMAINS_PATH = "outreach_domains.txt"
STATE_PATH = "state_pipeline_sync.json"
WINDOW_HOURS = 2
MAX_FETCH = 200

AUTO_NOTIFY_PREFIXES = [
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notification", "notifications", "notify", "notifier",
    "alerts", "alert", "info", "marketing", "newsletter", "newsletters",
    "news", "updates", "update", "system", "automated", "bot", "robot",
    "service", "services", "mailer", "mailing", "mail", "auto",
    "hrplatform", "hrbot", "hr", "team", "accounts", "billing",
    "digest", "feedback",
]


def is_auto_notification(sender_email):
    if "@" not in sender_email:
        return False
    local = sender_email.split("@", 1)[0].lower()
    for s in ("noreply", "no-reply", "donotreply", "do-not-reply", "notification", "automated", "robot", "mailer-daemon"):
        if s in local:
            return True
    if local in AUTO_NOTIFY_PREFIXES:
        return True
    for prefix in AUTO_NOTIFY_PREFIXES:
        for sep in ("-", "_", "."):
            if local.startswith(prefix + sep):
                return True
    return False


def load_outreach_domains():
    domains = []
    with open(OUTREACH_DOMAINS_PATH) as f:
        for line in f:
            line = line.strip().lower()
            if line and not line.startswith("#"):
                domains.append(line)
    return domains


def load_pipeline():
    with open(PIPELINE_PATH) as f:
        return json.load(f)


def save_pipeline(pipeline):
    pipeline["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(PIPELINE_PATH, "w") as f:
        json.dump(pipeline, f, indent=2, ensure_ascii=False)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": []}


def save_state(state):
    state["seen"] = state["seen"][-500:]
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


def domain_matches(sender_domain, domains):
    for d in domains:
        if sender_domain == d or sender_domain.endswith("." + d):
            return d
    return None


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
    try: M.close()
    except Exception: pass
    try: M.logout()
    except Exception: pass
    return msgs


def find_lead_by_domain(pipeline, sender_domain, matched_domain):
    sender_lower = sender_domain.lower()
    matched_lower = matched_domain.lower()
    for lead in pipeline["leads"]:
        searchable = (lead.get("notes", "") + " " + lead.get("topic", "")).lower()
        if matched_lower in searchable or sender_lower in searchable:
            return lead
        root = matched_lower.split(".")[0]
        if len(root) >= 4 and root in searchable:
            return lead
    return None


def recompute_silence_days(pipeline):
    today = datetime.now(timezone.utc).date()
    for lead in pipeline["leads"]:
        try:
            last = datetime.strptime(lead["last_activity"], "%Y-%m-%d").date()
            lead["silence_days"] = (today - last).days
        except (ValueError, KeyError):
            pass


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print(f"TG error: {e}", file=sys.stderr)
        return False


def main():
    pipeline = load_pipeline()
    domains = load_outreach_domains()
    state = load_state()
    seen = set(state["seen"])
    msgs = fetch_emails(WINDOW_HOURS)
    print(f"Fetched {len(msgs)} headers")

    new_replies = []
    new_other = []
    auto_notifications = []

    for msg in msgs:
        sender_email = extract_email(msg.get("From", ""))
        sender_domain = domain_of(sender_email)
        matched = domain_matches(sender_domain, domains)
        if not matched:
            continue
        mid = msg_id_hash(msg)
        if mid in seen:
            continue
        seen.add(mid)
        subject = decode_subject(msg.get("Subject", ""))

        if is_auto_notification(sender_email):
            auto_notifications.append({"sender": sender_email, "subject": subject[:140] if subject else "(no subject)"})
            print(f"AUTO suppressed: {sender_email} | {subject[:60]}")
            continue

        lead = find_lead_by_domain(pipeline, sender_domain, matched)
        if lead:
            old_status = lead.get("status", "unknown")
            lead["last_activity"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            lead["silence_days"] = 0
            lead["status"] = "reply_received"
            new_replies.append({
                "lead_id": lead["id"],
                "topic": lead.get("topic", ""),
                "old_status": old_status,
                "sender": sender_email,
                "subject": subject[:140] if subject else "(no subject)",
            })
        else:
            new_other.append({"sender": sender_email, "domain": sender_domain, "subject": subject[:140] if subject else "(no subject)"})

    recompute_silence_days(pipeline)
    save_pipeline(pipeline)
    state["seen"] = list(seen)
    save_state(state)

    for r in new_replies:
        text = (
            f"🔥 <b>PIPELINE REPLY</b>\n"
            f"<b>{esc(r['topic'])}</b>\n"
            f"<i>From: {esc(r['sender'])}</i>\n"
            f"Subject: {esc(r['subject'])}\n\n"
            f"Status: <code>{esc(r['old_status'])}</code> → <code>reply_received</code>\n\n"
            f'<a href="https://mail.google.com/mail/u/0/#inbox">Reply in Gmail</a>'
        )
        tg_send(text)
        time.sleep(0.5)

    if new_other:
        lines = ["📬 <b>Tracked domain — new human contact?</b>"]
        for o in new_other[:5]:
            lines.append(f"• <i>{esc(o['domain'])}</i>: {esc(o['subject'])}")
        if len(new_other) > 5:
            lines.append(f"+{len(new_other) - 5} more")
        lines.append("\nUpdate pipeline.json if relevant.")
        tg_send("\n".join(lines))

    print(f"Done. Real replies: {len(new_replies)}, new contacts: {len(new_other)}, auto suppressed: {len(auto_notifications)}")


if __name__ == "__main__":
    main()
