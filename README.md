# DenmarkTgBot

DenmarkTgBot is a free information bot that tracks official Danish publications that may affect Ukrainians under temporary protection in Denmark.

**Status:** Public Beta

**Telegram:** [@DenmarkForUABot](https://t.me/DenmarkForUABot)

**Languages:** Ukrainian and Russian

## Sources

Active beta sources:

- [Høringsportalen](https://hoeringsportalen.dk/) — official Danish consultation publications and documents.
- [Folketinget](https://www.ft.dk/) — parliamentary publications and proceedings.

Planned after beta:

- Nyidanmark
- Retsinformation

## Relevance and notifications

Publications are classified as:

- **DIRECT** — directly concerns Ukrainians under temporary protection in Denmark.
- **POTENTIAL** — may be relevant depending on the subject and circumstances.
- **IRRELEVANT** — does not meet the bot’s relevance criteria.

Users can browse available publications in News and Archive, filter by category, and set independent notification preferences for DIRECT and POTENTIAL updates in Settings.

## How it works

- **Cloudflare Worker** serves the Telegram bot and application endpoints.
- **Cloudflare D1** stores source publications, source state, user language, and notification preferences.
- **Python collectors** retrieve and prepare official source data.
- **GitHub Actions** runs the scheduled collector workflows.
- **Telegram Bot API** delivers the localized bot interface and notifications.

## Information and privacy

DenmarkTgBot provides general information, not legal advice. Source monitoring is limited and may not include every relevant update. Always check the linked official source and seek qualified advice for decisions about your situation.

The bot stores a Telegram identifier, language choice, and notification preferences to provide its service.

## Development

Requirements: Node.js 22.6 or newer and Python 3.

Run the Worker locally and its checks:

```sh
cd worker
npm install
npm run dev
npm run typecheck
npm test
```

Run the Python test suite from the repository root:

```sh
python3 -m pip install -r collectors/requirements.txt
python3 -m unittest discover -s tests
```

Current verified test baseline:

| Suite | Tests |
| --- | ---: |
| Worker | 160 |
| Python | 532 |
