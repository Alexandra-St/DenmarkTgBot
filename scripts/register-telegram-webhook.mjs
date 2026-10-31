import { stdin, stdout } from 'node:process';

const WEBHOOK_URL = 'https://denmarktgbot.alexandra-strona.workers.dev/telegram/webhook';
const ALLOWED_UPDATES = ['message', 'callback_query'];

function promptSecret(label) {
  if (!stdin.isTTY || typeof stdin.setRawMode !== 'function') {
    throw new Error('Run this helper in an interactive terminal.');
  }

  return new Promise((resolve, reject) => {
    let value = '';
    stdout.write(label);
    stdin.setRawMode(true);
    stdin.resume();

    const finish = (error) => {
      stdin.off('data', onData);
      stdin.setRawMode(false);
      stdout.write('\n');
      if (error) reject(error);
      else resolve(value);
    };

    const onData = (chunk) => {
      for (const char of chunk.toString('utf8')) {
        if (char === '\u0003') return finish(new Error('Cancelled.'));
        if (char === '\r' || char === '\n') return finish();
        if (char === '\u007f' || char === '\b') value = value.slice(0, -1);
        else if (char >= ' ' && char <= '~') value += char;
      }
    };

    stdin.on('data', onData);
  });
}

async function telegramRequest(token, method, payload) {
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: payload ? 'POST' : 'GET',
    headers: payload ? { 'content-type': 'application/json' } : undefined,
    body: payload ? JSON.stringify(payload) : undefined,
    redirect: 'error',
  });
  const result = await response.json().catch(() => null);
  if (!response.ok || result?.ok !== true) throw new Error('Telegram API request failed.');
  return result.result;
}

try {
  const token = await promptSecret('Enter the current Telegram bot token (hidden): ');
  const secret = await promptSecret('Enter TELEGRAM_WEBHOOK_SECRET (hidden): ');

  if (!token || !secret || secret.length > 256 || !/^[A-Za-z0-9_-]+$/.test(secret) || token === secret) {
    throw new Error('Input validation failed.');
  }

  await telegramRequest(token, 'setWebhook', {
    url: WEBHOOK_URL,
    secret_token: secret,
    allowed_updates: ALLOWED_UPDATES,
  });

  const info = await telegramRequest(token, 'getWebhookInfo');
  const actualUpdates = [...(info.allowed_updates ?? [])].sort();
  const expectedUpdates = [...ALLOWED_UPDATES].sort();
  if (info.url !== WEBHOOK_URL || JSON.stringify(actualUpdates) !== JSON.stringify(expectedUpdates)) {
    throw new Error('Webhook verification failed.');
  }

  stdout.write('Webhook registration confirmed; configured updates verified.\n');
} catch {
  stdout.write('Webhook registration or verification failed. No credentials were displayed.\n');
  process.exitCode = 1;
}
