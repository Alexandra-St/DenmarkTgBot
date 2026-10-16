# Source status shown in About

The About screen reports source freshness from D1's `source_states.last_success_at`, which is advanced only as part of a confirmed committed ingestion. Høringsportalen has two independent feed streams; both must have valid success timestamps. The earlier timestamp is shown conservatively, so the displayed time represents the latest point at which both feeds are known to have completed. Missing rows, null/invalid dates, or a database read error render as **unknown**.

A success within 24 hours is labelled active; an older success is labelled stale. The threshold allows for the configured three daily collection opportunities. This status reflects recent committed data only. It does not claim the GitHub Actions schedule is currently enabled or operational. Folketinget is always shown as not yet actively monitored; no incomplete Folketinget records or counts are shown.

Dates are localized for Ukrainian or Russian and displayed in the Denmark time zone (`Europe/Copenhagen`). No timestamp is synthesized when metadata is absent. No migration is required.
