export type ArchiveFilter = "all" | "direct" | "potential";
export type PublicationRelevance = "DIRECT" | "POTENTIAL" | "IRRELEVANT";

export interface PublicationRecord {
  id: number;
  external_id: string;
  external_url: string;
  original_title: string;
  relevance: PublicationRelevance;
  current_status: string | null;
  source_updated_at: string | null;
  source_code: string;
  source_name: string;
}

export interface PublicationPage {
  items: PublicationRecord[];
  hasNext: boolean;
}

export interface PublicationRepository {
  listNews(page: number): Promise<PublicationPage>;
  listArchive(filter: ArchiveFilter, page: number): Promise<PublicationPage>;
  findById(id: number): Promise<PublicationRecord | null>;
}

export const PUBLICATIONS_PER_PAGE = 5;
export const MAX_PUBLICATION_PAGE = 9999;
const FETCH_LIMIT = PUBLICATIONS_PER_PAGE + 1;

function validatePage(page: number): void {
  if (!Number.isSafeInteger(page) || page < 0 || page > MAX_PUBLICATION_PAGE) {
    throw new RangeError("invalid publication page");
  }
}

function validateFilter(filter: ArchiveFilter): void {
  if (filter !== "all" && filter !== "direct" && filter !== "potential") {
    throw new RangeError("invalid publication filter");
  }
}

const SELECT_PUBLICATION = `
  SELECT p.id, p.external_id, p.external_url, p.original_title, p.relevance,
         p.current_status, p.source_updated_at, s.code AS source_code,
         s.name AS source_name
  FROM publications p
  JOIN sources s ON s.id = p.source_id
`;

export class D1PublicationRepository implements PublicationRepository {
  private readonly db: D1Database;

  constructor(db: D1Database) {
    this.db = db;
  }

  async listNews(page: number): Promise<PublicationPage> {
    validatePage(page);
    const offset = page * PUBLICATIONS_PER_PAGE;
    const result = await this.db.prepare(`${SELECT_PUBLICATION}
      WHERE p.source_complete = 1 AND p.relevance IN ('DIRECT', 'POTENTIAL')
      ORDER BY CASE WHEN p.source_updated_at IS NULL OR p.source_updated_at = '' THEN 1 ELSE 0 END,
               p.source_updated_at DESC, CASE p.relevance WHEN 'DIRECT' THEN 0 ELSE 1 END,
               s.code ASC, p.external_id ASC, p.id ASC
      LIMIT ? OFFSET ?`)
      .bind(FETCH_LIMIT, offset)
      .all<PublicationRecord>();
    const rows = result.results ?? [];
    return { items: rows.slice(0, PUBLICATIONS_PER_PAGE), hasNext: rows.length > PUBLICATIONS_PER_PAGE };
  }

  async listArchive(filter: ArchiveFilter, page: number): Promise<PublicationPage> {
    validateFilter(filter);
    validatePage(page);
    const relevanceClause = filter === "all" ? "p.relevance IN ('DIRECT', 'POTENTIAL')" : "p.relevance = ?";
    const params: unknown[] = filter === "all" ? [] : [filter.toUpperCase()];
    params.push(FETCH_LIMIT, page * PUBLICATIONS_PER_PAGE);
    const result = await this.db.prepare(`${SELECT_PUBLICATION}
      WHERE p.source_complete = 1 AND ${relevanceClause}
      ORDER BY CASE WHEN p.source_updated_at IS NULL OR p.source_updated_at = '' THEN 1 ELSE 0 END,
               p.source_updated_at DESC, s.code ASC, p.external_id ASC, p.id ASC
      LIMIT ? OFFSET ?`)
      .bind(...params)
      .all<PublicationRecord>();
    const rows = result.results ?? [];
    return { items: rows.slice(0, PUBLICATIONS_PER_PAGE), hasNext: rows.length > PUBLICATIONS_PER_PAGE };
  }

  async findById(id: number): Promise<PublicationRecord | null> {
    if (!Number.isSafeInteger(id) || id < 1) throw new RangeError("invalid publication id");
    return this.db.prepare(`${SELECT_PUBLICATION} WHERE p.id = ? AND p.source_complete = 1 AND p.relevance IN ('DIRECT', 'POTENTIAL') LIMIT 1`)
      .bind(id)
      .first<PublicationRecord>();
  }
}
