export interface SourceStatusRepository {
  lastSuccessfulHoeringsportalenRun(): Promise<string | null>;
  lastSuccessfulFolketingetRun(): Promise<string | null>;
}

const HEARING_STREAMS = ["immigration_ministry", "immigration_category"] as const;
const SOURCE_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i;

export class D1SourceStatusRepository implements SourceStatusRepository {
  private readonly db: D1Database;

  constructor(db: D1Database) {
    this.db = db;
  }

  async lastSuccessfulHoeringsportalenRun(): Promise<string | null> {
    const result = await this.db.prepare(`
      SELECT stream_key, last_success_at
      FROM source_states
      WHERE source_id = (SELECT id FROM sources WHERE code = 'hoeringsportalen')
        AND stream_key IN (?, ?)
    `).bind(...HEARING_STREAMS).all<{ stream_key: string; last_success_at: string | null }>();
    const byStream = new Map((result.results ?? []).map((row) => [row.stream_key, row.last_success_at]));
    const timestamps = HEARING_STREAMS.map((stream) => byStream.get(stream));
    if (timestamps.some((value) => typeof value !== "string" || !SOURCE_TIMESTAMP.test(value) || !Number.isFinite(Date.parse(value)))) return null;
    // Both feeds are committed atomically; the earlier value is a conservative
    // marker for the latest point at which both streams were confirmed.
    return timestamps.map((value) => value as string)
      .reduce((earliest, value) => Date.parse(value) < Date.parse(earliest) ? value : earliest);
  }

  async lastSuccessfulFolketingetRun(): Promise<string | null> {
    const result = await this.db.prepare(`
      SELECT ss.updated_at
      FROM source_snapshot_states ss
      JOIN sources s ON s.id = ss.source_id
      WHERE s.code = 'folketinget'
    `).first<{ updated_at: string | null }>();
    const timestamp = result?.updated_at;
    return typeof timestamp === "string" && SOURCE_TIMESTAMP.test(timestamp)
      && Number.isFinite(Date.parse(timestamp)) ? timestamp : null;
  }
}
