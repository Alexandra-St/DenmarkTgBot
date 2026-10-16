# DenmarkTgBot

A free, open-source Telegram bot project for monitoring official Danish legislative updates that may affect Ukrainians under temporary protection.

**Intended audience:** Ukrainians under temporary protection in Denmark.

## Current status

Phase 1.0.3 adds Telegram onboarding, language selection, a localized menu, About, and notification settings. User preferences use the existing local/Cloudflare D1 schema. Source monitoring, News, Archive, and notification delivery are not implemented; the bot is not deployed and no production webhook is configured.

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

## Official data sources

- [Folketinget Open Data (ODA)](https://oda.ft.dk/) — [documentation](https://www.ft.dk/da/dokumenter/aabne_data)
- [Høringsportalen](https://hoeringsportalen.dk/) — [ATOM feeds](https://hoeringsportalen.dk/Syndication)

See [docs/source_research.md](docs/source_research.md) for the source research report.
