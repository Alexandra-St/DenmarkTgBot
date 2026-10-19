# Folketinget snapshot candidate extraction (offline core)

This phase adds an offline core only. It does not read OData or SQL Server, call Worker/D1, classify for ingestion, or send notifications.

`tools/folketinget_candidate_extraction.py` consumes the aggregate snapshot diff plus a read-only provider interface. The current provider is deterministic and in-memory for synthetic fixtures. A future SQL Server adapter must implement `get_by_ids` and `find_by_field`; the latter must return the complete matching relation set.

The private manifest iterator yields changed keys only inside the extraction path. Canonical keys are accepted only in the current one-column integer `id` format. Added and semantic changes are resolved through official IDs, then the full case graph is normalized with `collectors.folketinget_models.normalize_many` and assembled with `assemble_cases`. Candidate payloads follow the existing `LegislativeCase` model and carry `source="folketinget"`, `historical=false`, and `notify_eligible=null`.

Technical-only changes and unchanged rows do not produce candidates. Removals are counted in aggregate and add `removed_change_requires_old_snapshot_hydration`; extraction is incomplete because v1 manifests cannot recover the removed row's former relationships. Missing rows, unresolved relations, or normalization/assembly diagnostics also make the bundle incomplete. More than 5,000 unique affected cases raises a safe fail-closed category.

Only `candidate_summary()` is suitable for logs: it contains aggregate diff counts, affected/hydrated counts, diagnostic code counts, and completeness. Do not log candidate bundles or canonical keys. This core is not connected to the relevance/ingestion pipeline yet.
