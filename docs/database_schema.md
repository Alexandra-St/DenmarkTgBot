# Локальная схема D1

Миграция `worker/migrations/0001_initial_schema.sql` создаёт основу хранения пользователей, источников, публикаций, истории изменений, уведомлений и курсоров опроса.

| Таблица | Назначение и ограничения |
| --- | --- |
| `users` | `telegram_id` как PK; `language` — `uk`/`ru`; флаги уведомлений — `0`/`1`. |
| `sources` | Справочник источников с уникальным `code`; миграция добавляет `folketinget` и `hoeringsportalen`. |
| `publications` | Снимок публикации; уникальность `(source_id, external_id)`, внешний ID сохраняется как текст. `relevance`: `DIRECT`, `POTENTIAL`, `IRRELEVANT`. |
| `publication_events` | История событий с уникальным `event_key`; `notify_eligible=0` позволяет импортировать историю без уведомлений. |
| `notifications` | Очередь доставки с уникальностью `(telegram_id, event_id)` и статусами `PENDING`, `SENT`, `FAILED`. |
| `source_states` | Независимый checkpoint на каждую пару `(source_id, stream_key)`. |

Все временные значения — `TEXT` в UTC ISO 8601. `published_at`, `source_updated_at`, `occurred_at`, `last_success_at` могут быть `NULL`, если источник не предоставил дату. `created_at`, `updated_at`, `discovered_at` и `detected_at` задаются вызывающим кодом; триггеры не меняют время автоматически. Исходные заголовки и URL хранятся без перевода или перезаписи. Внешние ключи ограничивают удаление связанной истории.

Индексы поддерживают выборки по источнику, релевантности и дате публикации, историю событий по публикации/дате, очередь ожидающих уведомлений и состояние источников. Ограничения `UNIQUE` имеют собственные индексы и повторно не индексируются.

## Локальная проверка миграций

Wrangler использует локальное хранилище по умолчанию для локальных D1 команд. В `worker/wrangler.jsonc` значение `database_id: "local"` — явный локальный placeholder, не Cloudflare ID.

```sh
cd worker
npx wrangler d1 migrations apply denmarktgbot-db --local
npx wrangler d1 execute denmarktgbot-db --local --command "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
```

Не запускайте эти команды с `--remote`. Для будущих изменений добавляйте следующий SQL-файл с новой версией в `worker/migrations/`; уже применённую миграцию не редактируйте. `.wrangler/` и файлы SQLite исключены из Git.
