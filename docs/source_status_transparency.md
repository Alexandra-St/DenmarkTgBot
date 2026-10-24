# Source status shown in About

The beta currently has two production data sources: Høringsportalen and Folketinget. The About screen reports freshness from committed source state already stored in D1. Høringsportalen has two independent feed streams in `source_states.last_success_at`; both must have valid success timestamps, and the older timestamp is used conservatively. Folketinget uses `source_snapshot_states.updated_at`, which advances with a committed snapshot. Missing state, null/invalid timestamps, or a database read error render as **Unknown**; a read failure is not treated as **Stale**.

A committed update within 24 hours is **Active**; an older update is **Stale**. Høringsportalen runs three times daily, while Folketinget's snapshot workflow runs once daily; 24 hours matches one full daily Folketinget interval and permits multiple Høringsportalen opportunities. The status reflects committed data only and does not claim that a schedule is enabled or operational. Nyidanmark and Retsinformation are planned post-beta sources.

About shows only the localized status label, not internal timestamps, run IDs, or infrastructure details. No timestamp is synthesized when metadata is absent. No migration is required.
