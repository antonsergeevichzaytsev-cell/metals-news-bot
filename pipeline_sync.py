#!/usr/bin/env python3
"""Pipeline sync v3 — заводит лиды из SENT, ловит живые ответы, глушит автоматику."""
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
SENT_WINDOW_HOURS = 48  # шире INBOX: письмо вечером/в пятницу не должно потеряться.
                        # Дедуп по Message-ID делает повторный проход безопасным.
MAX_FETCH = 200

# Каденция — единственный источник правды, дублируется в mission_control.is_dead()
CADENCE_FOLLOWUP_DAYS = "4-7"
CADENCE_MAX_TOUCHES = 3

# Домены, на которые Антон пишет не по делу. Лид из них не заводим никогда.
NO_TRACK_DOMAINS = {
    "gmail.com", "googlemail.com", "mail.ru", "inbox.ru", "list.ru", "bk.ru",
    "yandex.ru", "yandex.com", "ya.ru", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "me.com", "proton.me", "protonmail.com", "rambler.ru",
    "google.com", "apple.com", "microsoft.com", "telegram.org", "github.com",
    "amazon.com", "notion.so", "slack.com", "zoom.us", "dropbox.com",
}

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
    # сначала точное совпадение по to_domain — так матчатся авто-заведённые лиды
    for lead in pipeline["leads"]:
        if lead.get("to_domain") and sender_domain.endswith(lead["to_domain"]):
            return lead
    sender_lower = sender_domain.lower()
    matched_lower = matched_domain.lower()
    for lead in pipeline["leads"]:
        # весь лид целиком: домен может жить в next_action или id, а не только в notes
        searchable = json.dumps(lead, ensure_ascii=False).lower()
        if matched_lower in searchable or sender_lower in searchable:
            return lead
        root = matched_lower.split(".")[0]
        if len(root) >= 4 and root in searchable:
            return lead
    return None


# --- SENT: отсюда лиды и появляются ------------------------------------------

def tracked_domains(pipeline, base_domains):
    """База из файла + домены всех лидов, включая мёртвые.
    Мёртвые тоже слушаем: ответ после смерти воскрешает лид, а не теряется.
    Файл менять не можем (его не коммитит воркфлоу), поэтому список — производный."""
    out = set(base_domains)
    for lead in pipeline["leads"]:
        if lead.get("to_domain"):
            out.add(lead["to_domain"])
    return sorted(out)


def find_sent_folder(M):
    """Имя папки Отправленные зависит от локали ящика — ищем по флагу \\Sent."""
    typ, data = M.list()
    if typ != "OK" or not data:
        return None
    for raw in data:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if "\\Sent" in line:
            m = re.search(r'"([^"]+)"\s*$', line)
            if m:
                return m.group(1)
            parts = line.split()
            if parts:
                return parts[-1].strip('"')
    return None


def fetch_sent(window_hours):
    M = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
    M.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    folder = find_sent_folder(M)
    if not folder:
        print("SENT folder not found — пропускаю заведение лидов", file=sys.stderr)
        try: M.logout()
        except Exception: pass
        return []
    typ, _ = M.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        print(f"cannot select {folder}", file=sys.stderr)
        try: M.logout()
        except Exception: pass
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'(SINCE "{since}")')
    msgs = []
    if typ == "OK" and data and data[0]:
        for num in data[0].split()[-MAX_FETCH:]:
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


def recipients_of(msg):
    out = []
    for hdr in ("To", "Cc"):
        for chunk in (msg.get(hdr, "") or "").split(","):
            addr = extract_email(chunk)
            if addr:
                out.append(addr)
    return out


def is_trackable_recipient(addr, own_domain):
    """ВАЖНО: тут НЕЛЬЗЯ звать is_auto_notification — это эвристика для ВХОДЯЩИХ.
    Она режет info@/office@/alerts@, а Антон именно на приёмные и пишет:
    большинство его outreach — info@/office@/priemnaya@. Для исходящих
    отсекаем только адреса, на которые физически нельзя написать."""
    if not addr or "@" not in addr:
        return False
    d = domain_of(addr)
    if d in NO_TRACK_DOMAINS or d == own_domain:
        return False
    local = addr.split("@", 1)[0].lower()
    for s in ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
              "mailer-daemon", "postmaster", "bounce"):
        if s in local:
            return False
    return True


def process_sent(pipeline, msgs, seen):
    """Заводит лид на КАЖДОЕ исходящее письмо на новый домен и считает касания.
    Ложный лид не страшен: без ответа он умрёт по каденции за 21 день сам."""
    own_domain = domain_of(GMAIL_USER.lower())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    created, touched = [], []

    for msg in msgs:
        mid = msg_id_hash(msg)
        if mid in seen:
            continue
        subject = decode_subject(msg.get("Subject", "")) or "(no subject)"
        rcpts = [a for a in recipients_of(msg) if is_trackable_recipient(a, own_domain)]
        if not rcpts:
            continue
        seen.add(mid)

        done_domains = set()
        for addr in rcpts:
            d = domain_of(addr)
            if d in done_domains:
                continue
            done_domains.add(d)

            lead = find_lead_by_domain(pipeline, d, d)
            if lead:
                # он ответил на их ответ -> мяч у них, тревога снимается сама
                was = lead.get("status", "")
                if was in ("dead", "closed", "channel_failed", "declined"):
                    lead["touches"] = 1  # осознанно вернулся -> новая кампания
                    for k in ("closed_date", "closed_reason"):
                        lead.pop(k, None)
                else:
                    lead["touches"] = lead.get("touches", 1) + 1
                lead["status"] = "sent_no_reply"
                lead["last_activity"] = today
                lead["silence_days"] = 0
                lead.setdefault("to_domain", d)
                lead["next_action"] = (
                    f"Касание {lead['touches']}/{CADENCE_MAX_TOUCHES}. "
                    f"Следующее через {CADENCE_FOLLOWUP_DAYS} дн, потом dead."
                )
                touched.append({"topic": lead.get("topic", "")[:60], "domain": d,
                                "touches": lead["touches"], "was": was})
            else:
                root = re.sub(r"[^a-z0-9]", "", d.split(".")[0])[:20] or "x"
                lead = {
                    "id": f"outreach_auto_{root}",
                    "channel": "direct_outreach",
                    "type": "partnership",
                    "topic": subject[:100],
                    "status": "sent_no_reply",
                    "value_usd": None,
                    "first_contact": today,
                    "last_activity": today,
                    "silence_days": 0,
                    "to_domain": d,
                    "touches": 1,
                    "next_action": f"Касание 1/{CADENCE_MAX_TOUCHES}. Следующее через {CADENCE_FOLLOWUP_DAYS} дн, потом dead.",
                    "notes": f"Авто-заведён из SENT {today}. Кому: {addr}",
                }
                pipeline["leads"].append(lead)
                created.append({"topic": subject[:60], "domain": d, "to": addr})
    return created, touched


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
    state = load_state()
    seen = set(state["seen"])

    # 1) SENT первым: лид должен существовать ДО того, как придёт ответ на него,
    #    иначе ответ упадёт в "new human contact?" и снова потеряется.
    try:
        sent_msgs = fetch_sent(SENT_WINDOW_HOURS)
        print(f"Fetched {len(sent_msgs)} sent headers")
        created, touched = process_sent(pipeline, sent_msgs, seen)
    except Exception as e:
        print(f"SENT error: {e}", file=sys.stderr)
        created, touched = [], []

    # 2) домены считаем ПОСЛЕ заведения — свежий лид сразу под наблюдением
    domains = tracked_domains(pipeline, load_outreach_domains())
    msgs = fetch_emails(WINDOW_HOURS)
    print(f"Fetched {len(msgs)} inbox headers, tracking {len(domains)} domains")

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

    if created or touched:
        lines = ["🆕 <b>PIPELINE — из отправленных</b>"]
        for c in created[:8]:
            lines.append(f"• Заведён: <i>{esc(c['domain'])}</i> — {esc(c['topic'])}")
        if len(created) > 8:
            lines.append(f"+{len(created) - 8} ещё заведено")
        for t in touched[:8]:
            note = " (воскрешён)" if t["was"] in ("dead", "closed", "channel_failed", "declined") else ""
            lines.append(f"• Касание {t['touches']}/{CADENCE_MAX_TOUCHES}{note}: <i>{esc(t['domain'])}</i> — {esc(t['topic'])}")
        over = [t for t in touched if t["touches"] > CADENCE_MAX_TOUCHES]
        if over:
            lines.append(f"\n⚠️ Каденция превышена у {len(over)}: больше {CADENCE_MAX_TOUCHES} касаний — это уже не настойчивость.")
        tg_send("\n".join(lines))

    if new_other:
        lines = ["📬 <b>Tracked domain — new human contact?</b>"]
        for o in new_other[:5]:
            lines.append(f"• <i>{esc(o['domain'])}</i>: {esc(o['subject'])}")
        if len(new_other) > 5:
            lines.append(f"+{len(new_other) - 5} more")
        lines.append("\nUpdate pipeline.json if relevant.")
        tg_send("\n".join(lines))

    print(f"Done. Real replies: {len(new_replies)}, new contacts: {len(new_other)}, "
          f"auto suppressed: {len(auto_notifications)}, leads created: {len(created)}, touched: {len(touched)}")


if __name__ == "__main__":
    main()
