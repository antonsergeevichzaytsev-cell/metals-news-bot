# Деплой Cloudflare Worker — вручную через дашборд

API Cloudflare недоступен из среды, где пишется этот код (egress
allowlist блокирует api.cloudflare.com) — деплой делается через
браузер, не автоматизирован. Тот же процесс, что уже сделан для
fitness-bot (см. `fitness-bot/cloudflare-worker/DEPLOY.md`), просто
другой репозиторий и другой набор секретов.

## 1. Создать Worker

1. dash.cloudflare.com -> в левом меню **Workers & Pages**
2. **Create application** -> **Create Worker**
3. Имя: `metals-news-bot-webhook` (или любое) -> **Deploy** (сначала
   деплоится дефолтный "Hello World", это нормально — код заменим
   следующим шагом)

## 2. Вставить код

1. Открой только что созданный Worker -> **Edit code** (или "Quick edit")
2. Удали весь дефолтный код
3. Вставь целиком содержимое `cloudflare-worker/worker.js` из этого
   репозитория
4. **Deploy** (кнопка сверху)

## 3. Секреты Worker'а

Worker -> **Settings** -> **Variables and Secrets** -> **Add variable**:

- `GITHUB_TOKEN` — Personal Access Token с правом `repo` (можно тот же
  токен, что уже используется для fitness-bot Worker'а, если права
  достаточно широкие — либо отдельный, не принципиально), тип **Secret**
  (encrypt), не Text
- `TELEGRAM_SECRET_TOKEN` — любая случайная строка (например
  сгенерированная `openssl rand -hex 20`), тип **Secret**. НЕ используй
  ту же строку, что у fitness-bot Worker'а — это два разных бота с
  разными токенами, секреты держим раздельно. Та же строка понадобится
  на шаге 4 при регистрации webhook в Telegram — они должны совпадать
  буквально.

**Save and deploy** после добавления обеих переменных.

## 4. Зарегистрировать webhook в Telegram

Открой в браузере (замени `<WORKER_URL>` на реальный URL Worker'а,
Cloudflare покажет его после деплоя — вида
`https://metals-news-bot-webhook.<твой-субдомен>.workers.dev`, и
`<SECRET>` на ту же строку, что в TELEGRAM_SECRET_TOKEN). Используй
токен бота `@antonmining_bot` (тот же TELEGRAM_BOT_TOKEN, что уже в
секретах GitHub этого репозитория):

```
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=<WORKER_URL>&secret_token=<SECRET>
```

Должно вернуть `{"ok":true,"result":true,"description":"Webhook was set"}`.

Проверить, что webhook встал: открой

```
https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo
```

`url` должен быть равен `<WORKER_URL>`, `pending_update_count` — 0
(если недавно ничего не писали боту).

## Ротация секретов Worker'а

**19.08.2026:** `GITHUB_TOKEN` в секретах этого Worker'а не имеет
зафиксированной даты создания — тот же класс риска, что реально
сработал с `GMAIL_APP_PASSWORD` в основном репо (Google молча
инвалидировал его на 61-й день без предупреждения, бот тихо ломался
несколько дней, пока не заметили по failure-статистике Actions).
GitHub PAT ведёт себя так же: может истечь по дате expiration или
быть отозван, а Worker узнает об этом только по факту сбоя
`dispatchToGitHub` — то есть когда Telegram-команды бота перестанут
отвечать.

Последнее известное подтверждение, что токен рабочий: 16.08.2026,
перерегистрация webhook на правильный Cloudflare URL
(`metals-news-bot-webhook.anton-sergeevich-zaytsev.workers.dev`),
`/prices` ответил живьём через реальный Telegram.

**При следующей ручной проверке или пересоздании токена** — впиши
дату здесь, по аналогии с `secrets_rotation.json` в основном репо:

```
GITHUB_TOKEN создан/обновлён: <ГГГГ-ММ-ДД> (заполнить вручную)
```

Это не автоматический алерт (Worker не пишет обратно в repo, не
может сам себя проверить) — просто напоминание при следующем
открытии этого файла, чтобы не потерять след, как было с Gmail.

## Как это работает дальше

Telegram -> POST на Worker при каждом сообщении -> Worker проверяет
secret_token -> дёргает `repository_dispatch` (event_type
`telegram_update`) на этот репозиторий -> запускается `command.yml` ->
`bot_commands.py` читает тело апдейта прямо из client_payload (env
`TELEGRAM_UPDATE_JSON`), НЕ делает повторный запрос к Telegram ->
обрабатывает команду (`/digest`, `/company`, `/why`, `/status`, `/help`).

Если что-то в цепочке сломается — GitHub Actions run просто не
появится после сообщения в Telegram. Проверять: Actions -> Telegram
command -> есть ли новый run с недавним временем.

## Проверка после деплоя

Напиши боту `@antonmining_bot` команду `/help` — должен прийти список
команд в течение нескольких секунд. Если тишина — смотри Actions на
GitHub (появился ли run вообще) и Worker Logs на Cloudflare (дошёл ли
запрос и что ответил GitHub API).
