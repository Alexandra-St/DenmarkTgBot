# Høringsportalen ATOM collector

This one-shot, standard-library Python collector reads two official feeds and writes normalized JSON to stdout. It runs independently of the Telegram Worker and does not persist data.

## Feeds

- `immigration_ministry`: <https://hoeringsportalen.dk/Syndication/HearingsByAuthorityFeed?authorityId=1745>
- `immigration_category`: <https://hoeringsportalen.dk/Syndication/HearingsByFormAreaFeed?formAreaId=37>

The feed list is configured in `collectors/hoeringsportalen.py` and can be extended by adding another `FeedConfig`.

## Run

From the repository root:

```sh
python3 -m collectors.main
```

JSON is written to stdout; concise diagnostics and feed outcomes go to stderr. A failed feed is reported in the JSON while successful-feed entries are retained. The process exits non-zero if any feed failed. Requests use HTTPS, a 15-second timeout, and at most two retries for transient network, HTTP 429, or HTTP 5xx failures.

## Output

The top-level object contains `publications` and `feeds`. Each publication includes `external_id`, `source`, `title`, `url`, `updated_at`, `author`, `content_text`, and `feed_codes`. Unavailable optional values are JSON `null`; timestamps are normalized to UTC. The `feeds` object reports each feed's status and counts, plus a summary.

Entries are deduplicated by the stable ID extracted from `entry/id` when it matches the Høringsportalen details URI; otherwise the complete original URI is retained as the ID. For duplicates, the newest `updated_at` supplies publication fields and all feed codes are retained. Results are sorted by update time descending, with stable ID ordering for ties.

## Limitations

- Each feed contains at most 25 recently updated consultations; historical completeness is not guaranteed.
- ATOM `updated` is the last known source update time, not necessarily the first publication time. The feed does not guarantee an `entry/published` value.
- Feed membership does not establish direct legal relevance to Ukrainians.
- The collector reads only ATOM entries and their embedded text. It does not fetch detail pages or PDF attachments.
- The collector does not classify relevance, persist results, schedule polling, or send notifications. It does not monitor all Danish legislative changes.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

All collector tests use local XML fixtures and mocked HTTP responses; they require no internet access.
