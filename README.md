# Metals & Mining digest bot

Дважды в день (Пн–Пт, 08:00 и 18:00 MSK) GitHub Actions запускает `digest.py`,
который тянет новости из Google News RSS, фильтрует по ключевикам, прогоняет
через DeepSeek для строки "почему важно", и постит дайджест в Telegram.

Стоимость: $0 за GitHub Actions (публичный репо), ~$0.02/мес за DeepSeek.

## Стек

- `digest.py` — основной скрипт. Pure Python stdlib, никаких pip-зависимостей.
- `feeds.txt` — 14 Google News RSS-запросов по темам.
- `keywords.txt` — ~90 ключевых слов (metals, companies, geo, CapEx, LME).
- `state.json` — последние 1000 GUIDs для дедупликации.
- `.github/workflows/digest.yml` — расписание и orchestration.

## Setup

### 1. Secrets

Settings → Secrets and variables → Actions → New repository secret:
- `TELEGRAM_BOT_TOKEN` — токен от @BotFather для @antonmining_bot
- `TELEGRAM_CHAT_ID` — `849676420`
- `DEEPSEEK_API_KEY` — ключ с https://platform.deepseek.com

### 2. Запустить вручную

Actions → "Metals & Mining digest" → Run workflow.

## Как работает фильтрация

1. **Источники.** 14 Google News RSS-запросов по темам (aluminium, copper, nickel, Russia/CIS, M&A, lithium, тарифы).
2. **Возраст.** Откидываются новости старше 24 часов.
3. **Ключевики.** Substring match, case-insensitive.
4. **Дедуп.** Не повторяется то, что уже было в `state.json`.
5. **AI-фильтр.** DeepSeek выносит SKIP для нерелевантных (политика без металлов, крипта, общий бизнес).
6. **Лимит.** Максимум 10 новостей за запуск.

Без DEEPSEEK_API_KEY — работает без AI, просто заголовки.

## Тюнинг

- **Темы:** `feeds.txt`. Google News RSS: `https://news.google.com/rss/search?q=ЗАПРОС+when:2d&hl=en-US&gl=US&ceid=US:en`
- **Ключевики:** `keywords.txt`.
- **Расписание:** cron в `.github/workflows/digest.yml` (UTC).
- **Кап:** `MAX_ITEMS_PER_RUN` в `digest.py`.
- **Возраст:** `MAX_AGE_HOURS` в `digest.py`.
- **AI-промпт:** `DEEPSEEK_SYSTEM` и `DEEPSEEK_USER_TMPL` в `digest.py`.
