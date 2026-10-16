# DenmarkTgBot

A free, open-source Telegram bot project for monitoring official Danish legislative updates that may affect Ukrainians under temporary protection.

**Intended audience:** Ukrainians under temporary protection in Denmark.

## Current status

Phase 1.0.1 establishes a minimal Cloudflare Worker foundation. The only implemented route is `GET /health`. Telegram integration, data collection, notifications, and persistence are planned and are not implemented.

## Technology stack

- TypeScript and Cloudflare Workers
- Cloudflare D1 (planned)
- Python collectors and GitHub Actions scheduling (planned)
- Cloudflare Cron Triggers and Telegram Bot API (planned)

## Local development

```sh
cd worker
npm install
npm run dev
npm run typecheck
```

The local Worker serves `GET /health` at `http://localhost:8787/health`.

## Official data sources

- [Folketinget Open Data (ODA)](https://oda.ft.dk/) — [documentation](https://www.ft.dk/da/dokumenter/aabne_data)
- [Høringsportalen](https://hoeringsportalen.dk/) — [ATOM feeds](https://hoeringsportalen.dk/Syndication)

See [docs/source_research.md](docs/source_research.md) for the source research report.
