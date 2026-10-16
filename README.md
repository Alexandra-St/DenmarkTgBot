# DenmarkTgBot

A free, open-source Telegram bot project for monitoring official Danish legislative updates that may affect Ukrainians under temporary protection.

**Intended audience:** Ukrainians under temporary protection in Denmark.

## Current status

Phase 1.0.4 prepares Telegram onboarding, language selection, a localized menu, About, and notification settings for production. User preferences use the existing D1 schema. Source monitoring, News, Archive, and notification delivery are not implemented. Production deployment and Telegram webhook registration are pending.

## Technology stack

- TypeScript and Cloudflare Workers
- Cloudflare D1
- Python collectors and GitHub Actions scheduling (planned)
- Cloudflare Cron Triggers and Telegram Bot API (planned)

## Local development

```sh
cd worker
npm install
npm run dev
npm run typecheck
npm test
```

The local Worker serves `GET /health` at `http://localhost:8787/health` and accepts authenticated updates at `POST /telegram/webhook`. Apply the existing schema locally with `npx wrangler d1 migrations apply denmarktgbot-db --local`.

To configure local credentials, copy `.dev.vars.example` to `.dev.vars` and set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET` there. Never commit `.dev.vars`. The webhook requires the `X-Telegram-Bot-Api-Secret-Token` header; without a configured secret it returns `503`, and a wrong value returns `401`. Automated tests replace the Telegram API with a mock and never contact Telegram. Do not send a real `/start` update to a locally running Worker unless you intend it to call the Telegram API.

The test suite runs on Node.js 22.6 or newer and covers onboarding, saved languages and preferences, callback navigation, webhook validation, and `/health`/404 behavior.

## Production deployment

Production is not yet configured. Use Cloudflare Workers Free and D1 Free; do not enable paid features. After authenticating Wrangler, inspect existing resources with `npx wrangler deployments list` and `npx wrangler d1 list`; reuse them if present. Create the D1 database only if missing, preferably with EU jurisdiction, then replace the local `database_id` in `worker/wrangler.jsonc` with the returned ID. Apply `npx wrangler d1 migrations apply denmarktgbot-db --remote`, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET` with interactive `npx wrangler secret put <NAME>` commands, then deploy with `npm run deploy`.

Register the Telegram webhook only after verifying the deployed `/health` route and D1 binding. Configure HTTPS `/telegram/webhook`, the same secret as `TELEGRAM_WEBHOOK_SECRET`, and allowed updates `message` and `callback_query`; then verify with Telegram `getWebhookInfo`. Never put secret values in this file, Git, command output, or chat.

After deployment, manually verify: `/start`; choose Ukrainian; open Settings; change both notification preferences independently; switch to Russian; return to the main menu; open About, News, and Archive; send `/start` again and confirm the language and preferences persisted. Acceptance remains pending until this manual check is confirmed. Do not expose Telegram IDs when checking D1.

## Official data sources

- [Folketinget Open Data (ODA)](https://oda.ft.dk/) — [documentation](https://www.ft.dk/da/dokumenter/aabne_data)
- [Høringsportalen](https://hoeringsportalen.dk/) — [ATOM feeds](https://hoeringsportalen.dk/Syndication)

See [docs/source_research.md](docs/source_research.md) for the source research report.
