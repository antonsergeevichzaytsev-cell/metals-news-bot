"""Единый источник правды для каденции follow-up — раньше эти константы
и логика "лид мёртв?" существовали тремя независимыми копиями:
pipeline_sync.py, mission_control.py, weekly_check.py. На 28.07.2026 все
три были синхронны (CADENCE_MAX_SILENCE=21, DEAD_STATUSES одинаковы), но
это поддерживалось вручную, не структурно — правка одного файла без
синхронной правки остальных двух тихо разошлась бы, и никто бы не узнал
до продакшен-инцидента, ровно как с esc() 27.07.

Правило переноса: сюда идут константы и логика, ИДЕНТИЧНАЯ во всех
потребителях. due_for_followup (pipeline_sync) — специфичная логика
"пора слать письмо", используется только там, оставлена на месте.
watchdog в weekly_check.py использует is_dead косвенно (сравнивает
статус+silence_days тем же способом, что и is_dead) — вынесена сюда
как is_cadence_exhausted для явного переиспользования вместо
инлайн-копии условия.
"""

# Сколько дней тишины лид может прожить в статусе sent_no_reply /
# follow_up_overdue, прежде чем каденция считается исчерпанной.
CADENCE_MAX_SILENCE = 21

# Каденция follow-up: раньше этого срока долбить рано, позже —
# лид уже мёртв по CADENCE_MAX_SILENCE, черновик бессмыслен.
CADENCE_DUE_MIN = 4
CADENCE_FOLLOWUP_DAYS = "4-7"  # человекочитаемая форма для текстов уведомлений
CADENCE_MAX_TOUCHES = 3

# Статусы, при которых лид мёртв безусловно — не зависит от каденции.
DEAD_STATUSES = {"dead", "closed", "declined", "done", "channel_failed"}

# Статусы, для которых каденция (silence_days vs CADENCE_MAX_SILENCE)
# вообще имеет смысл проверять — вне этих статусов "молчание" не
# сигнал (won не умирает от тишины, reply_received тоже).
CADENCE_TRACKED_STATUSES = ("sent_no_reply", "follow_up_overdue")


def is_cadence_exhausted(status, silence_days):
    """True, если статус относится к отслеживаемым каденцией
    (CADENCE_TRACKED_STATUSES) И тишина превысила CADENCE_MAX_SILENCE.

    Это ровно то условие, что раньше было инлайн-скопировано в
    mission_control.is_dead() (третья ветка) и в weekly_check.py's
    zombies-детекции — теперь один источник для обоих.
    """
    return status in CADENCE_TRACKED_STATUSES and silence_days > CADENCE_MAX_SILENCE


def is_dead(lead):
    """Мёртв, если помечен мёртвым (DEAD_STATUSES) ИЛИ каденция исчерпана.
    Полученный ответ (reply_received) не умирает никогда от тишины —
    он и есть деньги, ждать реакции по нему можно сколько угодно.

    Перенесено из mission_control.py без изменения поведения — три
    ветки логики идентичны оригиналу, включая порядок проверок (won,
    unknown статусы и любой статус вне списков — не мёртв по умолчанию).
    """
    status = lead.get("status")
    if status in DEAD_STATUSES:
        return True
    if status == "reply_received":
        return False
    return is_cadence_exhausted(status, lead.get("silence_days", 0))
