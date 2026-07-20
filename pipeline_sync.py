#!/usr/bin/env python3
"""Pipeline sync v5 — лиды из SENT, живые ответы, черновики follow-up, счёт диспатчей."""
import imaplib, email, json, os, re, hashlib, sys, time
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
import urllib.request, urllib.parse

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")  # опционально: без него — грубая эвристика имени

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
CADENCE_DUE_MIN = 4      # раньше 4 дн долбить рано
CADENCE_MAX_SILENCE = 21  # позже лид мёртв, черновик бессмыслен

# --- Диспатчи платформ: ГЛАВНАЯ метрика Strategy v3.1 -------------------------
# От неё зависит reset 09.08 (cash flow не восстановился -> vahta в primary).
# До сих пор это была галочка, которую бот задавал Антону. Теперь считаем сами.
DISPATCH_WINDOW_HOURS = 96  # синк бежит Пн-Пт 10:00-18:30 -> окно должно крыть выходные

PLATFORM_MARKERS = {
    "glgroup": "GLG", "glginsights": "GLG", "glg.it": "GLG",
    "guidepoint": "Guidepoint",
    "alphasights": "AlphaSights",
    "dialectica": "Dialectica",
    "prosapient": "ProSapient",
    "atheneum": "Atheneum",
    "tegus": "Tegus",
    "glasford": "Glasford",
    "capvision": "Capvision",
    "thirdbridge": "Third Bridge", "third-bridge": "Third Bridge",
}

# Классификация грубая и честная: три корзины, ни одна не молчит.
# Если классификатор ошибается — Антон это увидит в отчёте, а не примет на веру.
DISPATCH_HINTS = ("project", "consultation", "expert call", "opportunity", "advisor",
                  "new request", "invitation", "screening", "engagement", "survey",
                  "проект", "консультац", "звонок", "запрос")
NOT_DISPATCH_HINTS = ("newsletter", "webinar", "payment", "invoice", "receipt",
                      "password", "terms of", "privacy", "unsubscribe", "profile",
                      "welcome", "рассылк", "вебинар")

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


DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

COMPANY_NAME_SYS_PROMPT = (
    "Given an email domain and an email subject line, output the real-world company "
    "or organisation name as it would appear in a Google News search — the way press "
    "and industry outlets actually write it, not a literal transcription of the domain. "
    "Prefer the name in its most common public form; keep Cyrillic names in Cyrillic "
    "if the company is Russian/CIS, keep Latin names in Latin otherwise. "
    "Reply ONLY with valid JSON: {\"name\": str}. If you cannot confidently identify "
    "a company (e.g. domain looks like a personal or generic mailbox), reply {\"name\": \"\"}."
)


def guess_company_name_crude(domain):
    """Тот же грубый фоллбэк, что в account_watch.py — держим синхронно вручную,
    это ~3 строки, не стоит городить общий модуль ради них."""
    root = domain.split(".")[0]
    root = re.sub(r"[-_]", " ", root)
    return root.strip().title()


def guess_company_name(domain, subject):
    """Вызывается РЕДКО — только при заведении нового лида, не на каждый синк.
    Если DeepSeek недоступен или не уверен — падаем на грубую эвристику,
    никогда не блокируем создание лида из-за этого вызова."""
    if not DEEPSEEK_KEY:
        return guess_company_name_crude(domain)
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": COMPANY_NAME_SYS_PROMPT},
            {"role": "user", "content": f"DOMAIN: {domain}\nSUBJECT: {subject[:150]}"},
        ],
        "temperature": 0.1,
        "max_tokens": 60,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_URL, data=data,
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8"))
        verdict = json.loads(resp["choices"][0]["message"]["content"])
        name = (verdict.get("name") or "").strip()
        return name if name else guess_company_name_crude(domain)
    except Exception as e:
        print(f"  ! company name guess failed for {domain}: {e}", file=sys.stderr)
        return guess_company_name_crude(domain)


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
                lead.setdefault("to_addr", addr)
                lead["next_action"] = (
                    f"Касание {lead['touches']}/{CADENCE_MAX_TOUCHES}. "
                    f"Следующее через {CADENCE_FOLLOWUP_DAYS} дн, потом dead."
                )
                touched.append({"topic": lead.get("topic", "")[:60], "domain": d,
                                "touches": lead["touches"], "was": was})
            else:
                root = re.sub(r"[^a-z0-9]", "", d.split(".")[0])[:20] or "x"
                company_name = guess_company_name(d, subject)
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
                    "to_addr": addr,
                    "company_name": company_name,
                    "touches": 1,
                    "next_action": f"Касание 1/{CADENCE_MAX_TOUCHES}. Следующее через {CADENCE_FOLLOWUP_DAYS} дн, потом dead.",
                    "notes": f"Авто-заведён из SENT {today}. Кому: {addr}",
                }
                pipeline["leads"].append(lead)
                created.append({"topic": subject[:60], "domain": d, "to": addr})
    return created, touched


# --- Черновики follow-up ------------------------------------------------------
# ГРАНИЦА, КОТОРУЮ НЕ ПЕРЕХОДИМ: скрипт НИКОГДА не отправляет письмо.
# Только кладёт в Черновики через IMAP APPEND. Отправляет всегда Антон руками.
# Причина простая: отправка необратима, а бот сегодня уже врал шесть недель подряд.

RU_TLD = (".ru", ".kz", ".kg", ".tj", ".uz", ".by", ".am", ".az")


def addr_of_lead(lead):
    if lead.get("to_addr"):
        return lead["to_addr"]
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", json.dumps(lead, ensure_ascii=False))
    return m.group(0).lower() if m else None


def is_ru_target(addr):
    d = domain_of(addr)
    return any(d.endswith(t) for t in RU_TLD)


def due_for_followup(lead):
    """Созрел по каденции: 4-7 дн тишины, касаний меньше трёх, ещё не труп."""
    if lead.get("status") != "sent_no_reply":
        return False
    s = lead.get("silence_days", 0)
    return CADENCE_DUE_MIN <= s <= CADENCE_MAX_SILENCE and lead.get("touches", 1) < CADENCE_MAX_TOUCHES


def make_draft(lead, addr):
    """Скелет в голосе Антона: без воды, один вопрос, лёгкий выход для адресата.
    Последнее касание прямо говорит, что оно последнее — так каденция
    перестаёт быть счётчиком в файле и становится словами в письме."""
    touch = lead.get("touches", 1) + 1
    last = touch >= CADENCE_MAX_TOUCHES
    topic = lead.get("topic", "").strip()
    subj = topic if topic.lower().startswith("re:") else f"Re: {topic}"

    if is_ru_target(addr):
        body = f"Здравствуйте,\n\nВозвращаюсь к письму от {lead.get('last_activity', '')} — {topic}.\n\n"
        body += ("Это моё последнее письмо по теме. Не ответите — пойму, что неактуально, "
                 "и больше беспокоить не буду.\n\n") if last else \
                ("Если сейчас не актуально — скажите прямо, я закрою вопрос и не буду писать снова.\n"
                 "Если актуально — пришлю одну страницу с тем, как бы к этому подошёл.\n\n")
        body += "С уважением,\nАнтон Зайцев"
    else:
        body = f"Hello,\n\nFollowing up on my note from {lead.get('last_activity', '')} regarding {topic}.\n\n"
        body += ("This is my last note on this. If I don't hear back, I'll assume it's not relevant "
                 "and won't follow up again.\n\n") if last else \
                ("If this isn't a priority right now, just say so — I'll close it out and won't write again.\n"
                 "If it is, I'll send a one-pager on how I'd approach it.\n\n")
        body += "Best regards,\nAnton Zaytsev"
    return subj, body


def find_drafts_folder(M):
    typ, data = M.list()
    if typ != "OK" or not data:
        return None
    for raw in data:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if "\\Drafts" in line:
            m = re.search(r'"([^"]+)"\s*$', line)
            if m:
                return m.group(1)
    return None


def put_drafts(pipeline, state):
    """Кладёт по одному черновику на каждое созревшее касание. Один раз на касание."""
    drafted = state.setdefault("drafted", {})
    due = [l for l in pipeline["leads"] if due_for_followup(l)]
    todo = []
    for lead in due:
        key = f"{lead['id']}:{lead.get('touches', 1)}"
        if key in drafted:
            continue
        addr = addr_of_lead(lead)
        if not addr:
            continue
        todo.append((lead, addr, key))
    if not todo:
        return []

    made = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        M.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        folder = find_drafts_folder(M)
        if not folder:
            print("Drafts folder not found", file=sys.stderr)
            M.logout()
            return []
        for lead, addr, key in todo:
            subj, body = make_draft(lead, addr)
            msg = EmailMessage()
            msg["From"] = GMAIL_USER
            msg["To"] = addr
            msg["Subject"] = subj
            msg.set_content(body)
            try:
                M.append(f'"{folder}"', "\\Draft", None, msg.as_bytes())
                drafted[key] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                made.append({"topic": lead.get("topic", "")[:50], "addr": addr,
                             "touch": lead.get("touches", 1) + 1,
                             "silence": lead.get("silence_days", 0)})
            except Exception as e:
                print(f"draft append failed for {addr}: {e}", file=sys.stderr)
        try: M.logout()
        except Exception: pass
    except Exception as e:
        print(f"Drafts error: {e}", file=sys.stderr)
    return made


# --- Счёт диспатчей -----------------------------------------------------------

def platform_of(domain):
    for marker, name in PLATFORM_MARKERS.items():
        if marker in domain:
            return name
    return None


def classify_platform_mail(subject):
    s = (subject or "").lower()
    for h in NOT_DISPATCH_HINTS:
        if h in s:
            return "other"
    for h in DISPATCH_HINTS:
        if h in s:
            return "dispatch"
    return "unknown"


def msg_date(msg):
    try:
        return parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def count_dispatches(msgs, state):
    """ВАЖНО: тут НЕ зовём is_auto_notification.
    Диспатчи приходят с noreply@glgroup.com и подобных — та эвристика выбросила бы
    ровно главную метрику Антона. Ровно та же ошибка, что резала info@ в исходящих.
    Считаем ВСЮ почту с доменов платформ и раскладываем на три корзины."""
    log = state.setdefault("dispatches", {})
    new = []
    for msg in msgs:
        sender = extract_email(msg.get("From", ""))
        plat = platform_of(domain_of(sender))
        if not plat:
            continue
        h = msg_id_hash(msg)
        if h in log:
            continue
        subject = decode_subject(msg.get("Subject", "")) or "(no subject)"
        rec = {"date": msg_date(msg), "platform": plat,
               "kind": classify_platform_mail(subject), "subject": subject[:120]}
        log[h] = rec
        new.append(rec)
    if len(log) > 300:
        for k in sorted(log, key=lambda k: log[k].get("date", ""))[:len(log) - 300]:
            del log[k]
    return new


def dispatches_last_days(state, days=7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = [r for r in state.get("dispatches", {}).values() if r.get("date", "") >= cutoff]
    return out


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

    # 3) диспатчи: отдельный проход, окно шире (выходные), дедуп свой
    try:
        wide = fetch_emails(DISPATCH_WINDOW_HOURS)
        new_dispatches = count_dispatches(wide, state)
    except Exception as e:
        print(f"Dispatch scan error: {e}", file=sys.stderr)
        new_dispatches = []

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
    drafts = put_drafts(pipeline, state)
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

    if drafts:
        lines = ["✍️ <b>Черновики follow-up готовы</b>", "<i>Лежат в Черновиках. Открой, поправь, отправь — сам я не отправляю.</i>", ""]
        for d in drafts:
            tag = " · ПОСЛЕДНЕЕ" if d["touch"] >= CADENCE_MAX_TOUCHES else ""
            lines.append(f"• Касание {d['touch']}/{CADENCE_MAX_TOUCHES}{tag} · молчат {d['silence']} дн\n  <i>{esc(d['addr'])}</i> — {esc(d['topic'])}")
        lines.append('\n<a href="https://mail.google.com/mail/u/0/#drafts">Открыть Черновики</a>')
        tg_send("\n".join(lines))

    fresh = [d for d in new_dispatches if d["kind"] == "dispatch"]
    if fresh:
        week = [d for d in dispatches_last_days(state, 7) if d["kind"] == "dispatch"]
        lines = ["🎯 <b>ДИСПАТЧ ПЛАТФОРМЫ</b>", "<i>Главная метрика Strategy v3.1</i>", ""]
        for d in fresh[:5]:
            lines.append(f"• <b>{esc(d['platform'])}</b> · {esc(d['date'])}\n  {esc(d['subject'])}")
        lines.append(f"\n<b>За 7 дней: {len(week)}</b>")
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
          f"auto suppressed: {len(auto_notifications)}, leads created: {len(created)}, "
          f"touched: {len(touched)}, drafts: {len(drafts)}, dispatches: {len(new_dispatches)}")

    # Сигнал воркфлоу: появились ли новые лиды в этом прогоне. account_watch
    # бежит по расписанию (13:15/17:15 MSK) — без этого свежий лид попадёт
    # под дозор только на следующий плановый прогон, а не сразу.
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        try:
            with open(gh_output, "a") as f:
                f.write(f"new_leads={len(created)}\n")
        except Exception as e:
            print(f"  ! could not write GITHUB_OUTPUT: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
