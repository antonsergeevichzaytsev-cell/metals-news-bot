/**
 * metals-news-bot webhook relay.
 *
 * fetch(): Telegram шлёт сюда POST при каждом новом сообщении
 * (настроено через setWebhook). Пересылает тело апдейта в GitHub через
 * repository_dispatch — command.yml читает его из client_payload
 * напрямую (TELEGRAM_UPDATE_JSON), без повторного getUpdates.
 *
 * Паттерн взят 1:1 из fitness-bot/cloudflare-worker/worker.js — тот же
 * фикс от 28.07.2026 применим здесь: getUpdates и активный webhook
 * взаимоисключающи в Telegram API, повторный опрос не увидел бы апдейт,
 * который уже ушёл через webhook.
 *
 * В отличие от fitness-bot, здесь нет scheduled() — этому боту не нужен
 * проактивный таймер, только реакция на входящие команды.
 */

const GITHUB_OWNER = "antonsergeevichzaytsev-cell";
const GITHUB_REPO = "metals-news-bot";

async function dispatchToGitHub(env, eventType, clientPayload) {
  const dispatchUrl = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/dispatches`;
  const resp = await fetch(dispatchUrl, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "metals-news-bot-worker",
    },
    body: JSON.stringify({ event_type: eventType, client_payload: clientPayload }),
  });
  if (!resp.ok) {
    const body = await resp.text();
    console.log(`repository_dispatch (${eventType}) failed: ${resp.status} ${body}`);
  }
  return resp;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    const receivedSecret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (receivedSecret !== env.TELEGRAM_SECRET_TOKEN) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch (e) {
      console.log(`failed to parse Telegram update body: ${e}`);
      return new Response("ok", { status: 200 });
    }

    // Telegram ретраит webhook при не-2xx ответе — намеренно всегда
    // возвращаем 200, даже если dispatchToGitHub залогировал сбой
    // внутри себя, чтобы Telegram не забомбардировал повторами один
    // неудачный webhook. Ошибка видна в Worker logs.
    await dispatchToGitHub(env, "telegram_update", { update });
    return new Response("ok", { status: 200 });
  },
};
