# News and Archive UI

Telegram News and Archive read from the existing `publications` and `sources` tables through `D1PublicationRepository`. The repository is read-only and uses bound parameters. It does not update ingestion state, events, notifications, or user settings.

Only complete publications classified `DIRECT` or `POTENTIAL` are visible; `IRRELEVANT` and incomplete source snapshots are excluded. News shows up to five records per page, newest `source_updated_at` first, with relevance and source-code tie breakers. Its text explains that the ordering uses the source update date, so a listed publication may not be new or recently published. Archive describes the relevant publications currently saved in the bot database and provides all, DIRECT, and POTENTIAL filters. Both screens read the same stored publications; News is not a separate breaking-news feed. Stable ID tie breakers make page order deterministic. A missing or invalid source date sorts after dated records.

List rows open a detail view with the original Danish title, source, available current status and source update date, bot relevance, and a legal disclaimer. An official-source button is shown only for HTTPS URLs whose host matches the publication source allowlist. Invalid links are omitted. Text is rendered as plain Telegram text and clipped to stay within message limits.

Populated and empty lists state in Ukrainian or Russian that automatic monitoring and notifications are not enabled. Archive also explains 🟢 DIRECT and 🟡 POTENTIAL classifications and the filters. The About view identifies Ukrainians under temporary protection in Denmark as the audience, names Høringsportalen and Folketinget, explains current source coverage and stored account preferences, and states that the bot is free, automatic notifications are disabled, coverage may be incomplete, and its information is not legal advice.

The About source-status section uses the earlier of the two Høringsportalen feed checkpoints as a conservative successful-check time: active within 24 hours, stale after that, and unknown for missing or invalid timestamps. Folketinget is shown as inactive while no source status exists. This is not a claim that scheduled collection will always run. Source-status dates are localized to Denmark time. A publication may disappear from a list if its stored row is removed or becomes incomplete, in which case stale detail callbacks are handled safely.

The 2026-10-16 beta preflight and its deployment and two-user test checklists are in [beta_release_preflight.md](beta_release_preflight.md).
