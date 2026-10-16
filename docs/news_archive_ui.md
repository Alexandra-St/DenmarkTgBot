# News and Archive UI

Telegram News and Archive read from the existing `publications` and `sources` tables through `D1PublicationRepository`. The repository is read-only and uses bound parameters. It does not update ingestion state, events, notifications, or user settings.

Only complete publications classified `DIRECT` or `POTENTIAL` are visible; `IRRELEVANT` and incomplete source snapshots are excluded. News shows up to five records per page, newest `source_updated_at` first, with relevance and source-code tie breakers. Archive provides all, DIRECT, and POTENTIAL filters and uses the same newest-first date ordering. Stable ID tie breakers make page order deterministic. A missing or invalid source date sorts after dated records.

List rows open a detail view with the original Danish title, source, available current status and source update date, bot relevance, and a legal disclaimer. An official-source button is shown only for HTTPS URLs whose host matches the publication source allowlist. Invalid links are omitted. Text is rendered as plain Telegram text and clipped to stay within message limits.

Callback payloads are parsed against bounded page, filter, and numeric ID formats and limited to Telegram's 64-byte callback-data maximum. Invalid or stale callbacks return a localized message and a safe navigation button. Empty lists are handled in Ukrainian and Russian. The About view states that a Høringsportalen baseline is stored, Folketinget collection is pending, monitoring and notifications are off, and coverage may be incomplete.

The screens do not imply that stored publications are newly published: they display the current available baseline. Automatic collection remains off. Dates are shown only when the stored source timestamp contains an explicit timezone; timestamps are rendered as calendar dates in UTC. A publication may disappear from a list if its stored row is removed or becomes incomplete, in which case stale detail callbacks are handled safely.
