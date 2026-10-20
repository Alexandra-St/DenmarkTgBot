import {
  SnapshotIdentityError,
  snapshotIdentityEquals,
  validateSnapshotIdentity,
  validateSnapshotProgression,
  type SnapshotIdentity,
} from "./snapshot_state.ts";

type SourceCode = "folketinget" | "hoeringsportalen";
type Relevance = "DIRECT" | "POTENTIAL" | "IRRELEVANT";
type JsonObject = Record<string, unknown>;

interface IngestEnv { DB: D1Database; INGEST_API_TOKEN?: string }
interface Cursor { timestamp: string; id: number }
interface PublicationInput {
  source: SourceCode; external_id: string; title: string; url: string;
  relevance: Relevance; relevance_reason: string; ruleset_version: string;
  status: string | null; publication_type: string | null; published_at: string | null;
  source_updated_at: string | null; source_updated_at_raw: string | null;
  historical: boolean; notification_blocked: boolean; complete: boolean;
  diagnostics: unknown[]; source_metadata: JsonObject; normalized_snapshot: JsonObject;
}
interface SourceDeleteInput { operation: "SOURCE_DELETE"; source: "folketinget"; external_id: string }
type StagedInput = PublicationInput | SourceDeleteInput;
function isSourceDelete(value: StagedInput): value is SourceDeleteInput {
  return "operation" in value && value.operation === "SOURCE_DELETE";
}

const ODA_STREAMS = ["Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"] as const;
const HEARING_FEEDS = ["immigration_ministry", "immigration_category"] as const;
const MAX_BODY_BYTES = 256 * 1024;
const MAX_BATCH_RECORDS = 50;
const COMMIT_RECORDS = 20;
const MAX_RUN_BATCHES = 200;
const MAX_CLEANUP_RUNS = 20;
const DEFAULT_INGESTION_RETENTION_DAYS = 90;

const READINESS_TABLE_COLUMNS: Record<string, readonly string[]> = {
  publications: ["source_deleted"],
  sources: ["id", "code"],
  ingestion_runs: [
    "run_id", "source_id", "status", "mode", "state_kind", "proposed_checkpoints_json",
    "base_checkpoints_json", "checkpoint_writes", "base_snapshot_json", "target_snapshot_json",
    "snapshot_state_writes",
  ],
  source_states: ["source_id"],
  source_snapshot_states: ["source_id", "backup_sha256", "backup_finish_date", "schema_fingerprint", "generation", "last_run_id", "initialized_at", "updated_at"],
};
const SNAPSHOT_STATE_TRIGGERS = [
  "source_states_reject_snapshot_run_insert",
  "source_states_reject_snapshot_run_update",
  "source_states_reject_snapshot_run_delete",
  "source_snapshot_states_guard_insert",
  "source_snapshot_states_count_insert",
  "source_snapshot_states_guard_update",
  "source_snapshot_states_count_update",
  "source_snapshot_states_reject_delete",
  "ingestion_runs_guard_state_kind_commit",
] as const;

const json = (body: unknown, status = 200): Response => new Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
});

function tokenMatches(expected: string, supplied: string): boolean {
  const a = new TextEncoder().encode(expected);
  const b = new TextEncoder().encode(supplied);
  let mismatch = a.length ^ b.length;
  mismatch |= Number(a.length > 256 || b.length > 256);
  for (let index = 0; index < 256; index += 1) mismatch |= (a[index] ?? 0) ^ (b[index] ?? 0);
  return mismatch === 0;
}

export async function isIngestionRequestAuthorized(request: Request, env: IngestEnv): Promise<boolean> {
  const expected = env.INGEST_API_TOKEN;
  const match = /^Bearer ([^\s]+)$/.exec(request.headers.get("Authorization") ?? "");
  return Boolean(expected && match && tokenMatches(expected, match[1]));
}

class RequestError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string) { super(code); this.status = status; this.code = code; }
}

async function readJson(request: Request): Promise<unknown> {
  const length = request.headers.get("Content-Length");
  if (length && (!/^\d+$/.test(length) || Number(length) > MAX_BODY_BYTES)) throw new RequestError(413, "request_too_large");
  if (!request.body) throw new RequestError(400, "invalid_json");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    size += part.value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      throw new RequestError(413, "request_too_large");
    }
    chunks.push(part.value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown; }
  catch { throw new RequestError(400, "invalid_json"); }
}

function object(value: unknown): value is JsonObject { return typeof value === "object" && value !== null && !Array.isArray(value); }
function string(value: unknown, max: number, empty = false): value is string {
  return typeof value === "string" && value.length <= max && (empty || value.trim().length > 0);
}
function integer(value: unknown): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value >= 0; }
function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (object(value)) return "{" + Object.keys(value).sort().map((key) => JSON.stringify(key) + ":" + canonical(value[key])).join(",") + "}";
  return JSON.stringify(value) ?? "null";
}
async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
function utcTimestamp(value: unknown): value is string {
  return typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?(?:Z|[+-]\d\d:\d\d)$/.test(value) && !Number.isNaN(Date.parse(value));
}
function sourceTimestamp(value: unknown): value is string {
  return typeof value === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,7})?$/.test(value) && !Number.isNaN(Date.parse(value + "Z"));
}
function timestampParts(value: string): [string, string] {
  const match = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,7}))?$/.exec(value);
  return [match?.[1] ?? value, (match?.[2] ?? "").padEnd(7, "0")];
}
function compareCursor(a: Cursor, b: Cursor, source: SourceCode): number {
  let order: number;
  if (source === "hoeringsportalen") order = a.timestamp.localeCompare(b.timestamp);
  else {
    const x = timestampParts(a.timestamp);
    const y = timestampParts(b.timestamp);
    order = x[0].localeCompare(y[0]) || x[1].localeCompare(y[1]);
  }
  return order || a.id - b.id;
}
function parseCursor(value: unknown, source: SourceCode): Cursor | null {
  if (value === null) return null;
  if (!object(value) || !integer(value.id)) throw new RequestError(400, "invalid_checkpoint");
  const valid = source === "hoeringsportalen" ? utcTimestamp(value.timestamp) : sourceTimestamp(value.timestamp);
  if (!valid) throw new RequestError(400, "invalid_checkpoint");
  return { timestamp: value.timestamp as string, id: value.id };
}
function officialUrl(source: SourceCode, value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2048) return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && (source === "folketinget"
      ? url.hostname === "oda.ft.dk" && url.pathname.startsWith("/api/Sag(")
      : url.hostname === "hoeringsportalen.dk" && /^\/Hearing\/Details\/\d+\/?$/i.test(url.pathname));
  } catch { return false; }
}

function relationships(value: unknown, kind: "documents" | "stages" | "votes"): JsonObject[] {
  if (!Array.isArray(value) || value.length > 1000) throw new RequestError(400, "invalid_snapshot");
  const rows: JsonObject[] = [];
  const ids = new Set<string>();
  for (const item of value) {
    if (!object(item) || !integer(item.id)) throw new RequestError(400, "invalid_snapshot");
    if (kind === "documents" && item.link_id !== undefined && !integer(item.link_id)) throw new RequestError(400, "invalid_snapshot");
    if (kind === "votes" && item.stage_id !== undefined && item.stage_id !== null && !integer(item.stage_id)) throw new RequestError(400, "invalid_snapshot");
    for (const field of ["type_id", "status_id", "number"]) {
      if (item[field] !== undefined && item[field] !== null && !integer(item[field])) throw new RequestError(400, "invalid_snapshot");
    }
    for (const field of ["date_raw", "released_at_raw", "document_released_at_raw", "source_updated_at_raw", "link_source_updated_at_raw", "conclusion", "comment"]) {
      if (item[field] !== undefined && item[field] !== null && !string(item[field], 4000, true)) throw new RequestError(400, "invalid_snapshot");
    }
    for (const field of ["updated_at_utc", "link_updated_at_utc"]) {
      if (item[field] !== undefined && item[field] !== null && !utcTimestamp(item[field])) throw new RequestError(400, "invalid_snapshot");
    }
    if (item.approved !== undefined && item.approved !== null && typeof item.approved !== "boolean") throw new RequestError(400, "invalid_snapshot");
    if (item.title !== undefined && item.title !== null && !string(item.title, 4000, true)) throw new RequestError(400, "invalid_snapshot");
    const key = kind === "documents" ? String(item.id) + ":" + String(item.link_id ?? "") : String(item.id);
    if (ids.has(key)) throw new RequestError(400, "duplicate_relationship_id");
    ids.add(key);
    rows.push({ ...item });
  }
  rows.sort((a, b) => Number(a.id) - Number(b.id) || Number(a.link_id ?? 0) - Number(b.link_id ?? 0));
  return rows;
}

function validatePublication(raw: unknown, source: SourceCode, mode: "historical" | "incremental"): PublicationInput {
  if (!object(raw)) throw new RequestError(400, "invalid_publication");
  if (raw.source !== source || typeof raw.external_id !== "string" || !/^\d{1,20}$/.test(raw.external_id)) throw new RequestError(400, "invalid_publication_id");
  if (!string(raw.title, 4000) || !officialUrl(source, raw.url)) throw new RequestError(400, "invalid_publication_fields");
  if (raw.relevance !== "DIRECT" && raw.relevance !== "POTENTIAL" && raw.relevance !== "IRRELEVANT") throw new RequestError(400, "invalid_relevance");
  if (!string(raw.relevance_reason, 2000, true) || !string(raw.ruleset_version, 32) || !/^\d+\.\d+$/.test(raw.ruleset_version)) throw new RequestError(400, "invalid_classification");
  if (raw.status !== null && !string(raw.status, 500, true)) throw new RequestError(400, "invalid_status");
  if (raw.publication_type !== null && !string(raw.publication_type, 200, true)) throw new RequestError(400, "invalid_publication_type");
  if (raw.published_at !== null && !utcTimestamp(raw.published_at)) throw new RequestError(400, "invalid_published_at");
  if (raw.source_updated_at !== null && !utcTimestamp(raw.source_updated_at)) throw new RequestError(400, "invalid_source_updated_at");
  if (raw.source_updated_at_raw !== null && !string(raw.source_updated_at_raw, 64, true)) throw new RequestError(400, "invalid_source_updated_at_raw");
  if (typeof raw.historical !== "boolean" || raw.historical !== (mode === "historical")) throw new RequestError(400, "invalid_historical_flag");
  if (typeof raw.notification_blocked !== "boolean" || typeof raw.complete !== "boolean") throw new RequestError(400, "invalid_completeness");
  if (!Array.isArray(raw.diagnostics) || raw.diagnostics.length > 100 || !object(raw.source_metadata)) throw new RequestError(400, "invalid_source_metadata");
  if (!object(raw.normalized_snapshot) || raw.normalized_snapshot.title !== raw.title) throw new RequestError(400, "invalid_snapshot");
  const documents = relationships(raw.normalized_snapshot.documents, "documents");
  const stages = relationships(raw.normalized_snapshot.stages, "stages");
  const votes = relationships(raw.normalized_snapshot.votes, "votes");
  if (source === "hoeringsportalen" && (documents.length || stages.length || votes.length)) throw new RequestError(400, "invalid_snapshot");
  if (raw.normalized_snapshot.content_text !== undefined && raw.normalized_snapshot.content_text !== null && !string(raw.normalized_snapshot.content_text, 100_000, true)) throw new RequestError(400, "invalid_snapshot");
  const snapshot: JsonObject = { title: raw.title, content_text: raw.normalized_snapshot.content_text ?? null, status: raw.status, publication_type: raw.publication_type, relevance: raw.relevance, documents, stages, votes };
  if (canonical(raw.source_metadata).length > 64 * 1024 || canonical(raw.diagnostics).length > 64 * 1024 || canonical(snapshot).length > 128 * 1024) throw new RequestError(413, "publication_too_large");
  return {
    source, external_id: raw.external_id, title: raw.title, url: raw.url, relevance: raw.relevance,
    relevance_reason: raw.relevance_reason, ruleset_version: raw.ruleset_version,
    status: raw.status as string | null, publication_type: raw.publication_type as string | null,
    published_at: raw.published_at as string | null, source_updated_at: raw.source_updated_at as string | null,
    source_updated_at_raw: raw.source_updated_at_raw as string | null, historical: raw.historical,
    notification_blocked: raw.notification_blocked, complete: raw.complete, diagnostics: raw.diagnostics,
    source_metadata: raw.source_metadata, normalized_snapshot: snapshot,
  };
}

function validateSourceDelete(raw: unknown, source: SourceCode, mode: "historical" | "incremental", stateKind: "checkpoint" | "snapshot"): SourceDeleteInput {
  if (!object(raw) || Object.keys(raw).sort().join(",") !== "external_id,operation,source"
      || raw.operation !== "SOURCE_DELETE" || source !== "folketinget" || mode !== "incremental" || stateKind !== "snapshot"
      || raw.source !== "folketinget" || typeof raw.external_id !== "string"
      || !/^\d{1,20}$/.test(raw.external_id)) {
    throw new RequestError(400, "invalid_source_deletion");
  }
  return { operation: "SOURCE_DELETE", source: "folketinget", external_id: raw.external_id };
}

function validateStagedInput(raw: unknown, source: SourceCode, mode: "historical" | "incremental", stateKind: "checkpoint" | "snapshot"): StagedInput {
  if (object(raw) && Object.hasOwn(raw, "operation")) return validateSourceDelete(raw, source, mode, stateKind);
  return validatePublication(raw, source, mode);
}

function failure(error: unknown): Response {
  if (error instanceof RequestError) return json({ error: error.code }, error.status);
  return json({ error: "internal_error" }, 500);
}
async function getSourceId(db: D1Database, source: SourceCode): Promise<number> {
  const row = await db.prepare("SELECT id FROM sources WHERE code = ? AND enabled = 1").bind(source).first<{ id: number }>();
  if (!row) throw new RequestError(400, "unknown_source");
  return row.id;
}
async function readStates(db: D1Database, id: number): Promise<{ base: Record<string, Cursor | null>; rows: Array<{ stream_key: string; last_success_at: string | null }> }> {
  const result = await db.prepare("SELECT stream_key, checkpoint, last_success_at FROM source_states WHERE source_id = ? ORDER BY stream_key").bind(id).all<{ stream_key: string; checkpoint: string | null; last_success_at: string | null }>();
  const base: Record<string, Cursor | null> = {};
  for (const row of result.results ?? []) {
    let value: unknown = null;
    if (row.checkpoint !== null) { try { value = JSON.parse(row.checkpoint) as unknown; } catch { throw new RequestError(500, "source_state_corrupt"); } }
    base[row.stream_key] = value === null ? null : parseCursor(value, row.stream_key.startsWith("immigration_") ? "hoeringsportalen" : "folketinget");
  }
  return { base, rows: result.results ?? [] };
}
function validateCheckpoints(value: unknown, source: SourceCode, base: Record<string, Cursor | null>): Record<string, Cursor | null> {
  if (!object(value)) throw new RequestError(400, "invalid_checkpoint");
  const keys: readonly string[] = source === "folketinget" ? ODA_STREAMS : HEARING_FEEDS;
  if (Object.keys(value).length !== keys.length || keys.some((key) => !(key in value))) throw new RequestError(400, "invalid_checkpoint_streams");
  const proposed: Record<string, Cursor | null> = {};
  for (const key of keys) {
    const next = parseCursor(value[key], source);
    const previous = base[key] ?? null;
    if (previous && (!next || compareCursor(next, previous, source) < 0)) throw new RequestError(409, "checkpoint_regression");
    proposed[key] = next;
  }
  return proposed;
}

async function ingest(request: Request, env: IngestEnv): Promise<Response> {
  if (!await isIngestionRequestAuthorized(request, env)) return json({ error: "unauthorized" }, 401);
  try {
    const body = await readJson(request);
    if (!object(body) || !string(body.action, 20)) throw new RequestError(400, "invalid_request");
    if (body.action === "start") return await startRun(body, env.DB);
    if (body.action === "batch") return await submitBatch(body, env.DB);
    if (body.action === "commit") return await commitRun(body, env.DB);
    if (body.action === "abort") return await abortRun(body, env.DB);
    if (body.action === "cleanup") return await cleanupStaging(body, env.DB);
    throw new RequestError(400, "invalid_action");
  } catch (error) { return failure(error); }
}

async function sourceStates(request: Request, env: IngestEnv): Promise<Response> {
  if (!await isIngestionRequestAuthorized(request, env)) return json({ error: "unauthorized" }, 401);
  try {
    const source = new URL(request.url).searchParams.get("source");
    if (source !== "folketinget" && source !== "hoeringsportalen") throw new RequestError(400, "invalid_source");
    const id = await getSourceId(env.DB, source);
    const result = await readStates(env.DB, id);
    return json({ source, streams: result.rows.map((row) => ({ stream: row.stream_key, checkpoint: result.base[row.stream_key] ?? null, last_success_at: row.last_success_at })) });
  } catch (error) { return failure(error); }
}

async function startRun(body: JsonObject, db: D1Database): Promise<Response> {
  if (typeof body.run_id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(body.run_id)) throw new RequestError(400, "invalid_run_id");
  if (body.source !== "folketinget" && body.source !== "hoeringsportalen") throw new RequestError(400, "unknown_source");
  const source = body.source;
  if (body.mode !== "historical" && body.mode !== "incremental") throw new RequestError(400, "invalid_mode");
  if (!integer(body.expected_batches) || body.expected_batches < 1 || body.expected_batches > MAX_RUN_BATCHES) throw new RequestError(400, "invalid_batch_count");
  if (body.collection_complete !== true) throw new RequestError(409, "incomplete_collection");
  if (body.state_kind !== undefined && body.state_kind !== "checkpoint" && body.state_kind !== "snapshot") throw new RequestError(400, "invalid_state_kind");
  if (body.state_kind === "snapshot") return await startSnapshotRun(body, db);
  const id = await getSourceId(db, source);
  const current = await readStates(db, id);
  const streamKeys: readonly string[] = source === "folketinget" ? ODA_STREAMS : HEARING_FEEDS;
  const base = Object.fromEntries(streamKeys.map((key) => [key, current.base[key] ?? null]));
  const proposed = validateCheckpoints(body.proposed_checkpoints, source, base);
  const baseline = await db.prepare("SELECT source_id FROM source_ingestion_baselines WHERE source_id = ?").bind(id).first<{ source_id: number }>();
  if (body.mode === "incremental" && !baseline) throw new RequestError(409, "historical_baseline_required");
  const startJson = canonical({ run_id: body.run_id, source, mode: body.mode, expected_batches: body.expected_batches, collection_complete: true, proposed_checkpoints: proposed });
  const startHash = await sha256(startJson);
  const oldRun = await db.prepare("SELECT status, start_request_hash, expected_batches FROM ingestion_runs WHERE run_id = ?").bind(body.run_id).first<{ status: string; start_request_hash: string; expected_batches: number }>();
  if (oldRun) {
    if (oldRun.start_request_hash !== startHash) throw new RequestError(409, "run_id_conflict");
    return json({ accepted: true, run_id: body.run_id, status: oldRun.status, expected_batches: oldRun.expected_batches, idempotent: true });
  }
  const now = new Date().toISOString();
  try {
    await db.prepare("INSERT INTO ingestion_runs (run_id, source_id, mode, expected_batches, collection_complete, status, proposed_checkpoints_json, base_checkpoints_json, start_request_hash, created_at, updated_at) VALUES (?, ?, ?, ?, 1, 'OPEN', ?, ?, ?, ?, ?)")
      .bind(body.run_id, id, body.mode, body.expected_batches, canonical(proposed), canonical(base), startHash, now, now).run();
  } catch { throw new RequestError(409, "source_run_active_or_run_conflict"); }
  return json({ accepted: true, run_id: body.run_id, status: "OPEN", expected_batches: body.expected_batches, idempotent: false }, 201);
}

function snapshotIdentityFromJson(value: string): SnapshotIdentity {
  try { return validateSnapshotIdentity(JSON.parse(value) as unknown); }
  catch { throw new RequestError(500, "snapshot_state_corrupt"); }
}

function snapshotIdentityError(error: unknown): never {
  if (error instanceof SnapshotIdentityError) throw new RequestError(error.code === "snapshot_identity_invalid" ? 400 : 409, error.code);
  throw error;
}

async function startSnapshotRun(body: JsonObject, db: D1Database): Promise<Response> {
  if (body.source !== "folketinget") throw new RequestError(400, "snapshot_source_unsupported");
  if (Object.hasOwn(body, "proposed_checkpoints")) throw new RequestError(400, "snapshot_checkpoints_not_allowed");
  if (body.mode !== "historical" && body.mode !== "incremental") throw new RequestError(400, "invalid_mode");
  if (!integer(body.expected_batches) || body.expected_batches < 1 || body.expected_batches > MAX_RUN_BATCHES) throw new RequestError(400, "invalid_batch_count");
  if (body.collection_complete !== true) throw new RequestError(409, "incomplete_collection");

  let target: SnapshotIdentity;
  try { target = validateSnapshotIdentity(body.target_snapshot); }
  catch (error) { snapshotIdentityError(error); }
  let base: SnapshotIdentity | null = null;
  if (body.mode === "historical") {
    if (body.base_snapshot !== null) throw new RequestError(400, "snapshot_base_must_be_null");
  } else {
    if (body.base_snapshot === null || body.base_snapshot === undefined) throw new RequestError(400, "snapshot_base_required");
    try { base = validateSnapshotIdentity(body.base_snapshot); }
    catch (error) { snapshotIdentityError(error); }
  }

  const normalizedBase = base;
  const startHash = await sha256(canonical({
    run_id: body.run_id,
    source: body.source,
    mode: body.mode,
    state_kind: "snapshot",
    expected_batches: body.expected_batches,
    collection_complete: true,
    base_snapshot: normalizedBase,
    target_snapshot: target,
  }));
  const oldRun = await db.prepare("SELECT status, start_request_hash, expected_batches, state_kind FROM ingestion_runs WHERE run_id = ?")
    .bind(body.run_id).first<{ status: string; start_request_hash: string; expected_batches: number; state_kind: string }>();
  if (oldRun) {
    if (oldRun.start_request_hash !== startHash || oldRun.state_kind !== "snapshot") throw new RequestError(409, "run_id_conflict");
    return json({ accepted: true, run_id: body.run_id, status: oldRun.status, expected_batches: oldRun.expected_batches, state_kind: "snapshot", idempotent: true });
  }

  const sourceId = await getSourceId(db, "folketinget");
  const current = await db.prepare("SELECT backup_sha256, backup_finish_date, schema_fingerprint, generation FROM source_snapshot_states WHERE source_id = ?")
    .bind(sourceId).first<{ backup_sha256: string; backup_finish_date: string; schema_fingerprint: string; generation: number }>();
  const baseline = await db.prepare("SELECT source_id FROM source_ingestion_baselines WHERE source_id = ?").bind(sourceId).first<{ source_id: number }>();
  if (body.mode === "historical") {
    if (current) throw new RequestError(409, "snapshot_state_already_initialized");
  } else {
    if (!current) throw new RequestError(409, "snapshot_state_not_initialized");
    if (!baseline) throw new RequestError(409, "historical_baseline_required");
    const saved = {
      backup_sha256: current.backup_sha256,
      backup_finish_date: current.backup_finish_date,
      schema_fingerprint: current.schema_fingerprint,
    };
    if (!base || !snapshotIdentityEquals(base, saved)) throw new RequestError(409, "snapshot_base_stale");
    try {
      if (validateSnapshotProgression(saved, target) === "same") throw new RequestError(409, "snapshot_already_current");
    } catch (error) {
      if (error instanceof RequestError) throw error;
      snapshotIdentityError(error);
    }
  }

  const now = new Date().toISOString();
  try {
    await db.prepare("INSERT INTO ingestion_runs (run_id, source_id, mode, expected_batches, collection_complete, status, proposed_checkpoints_json, base_checkpoints_json, start_request_hash, created_at, updated_at, state_kind, base_snapshot_json, target_snapshot_json) VALUES (?, ?, ?, ?, 1, 'OPEN', '{}', '{}', ?, ?, ?, 'snapshot', ?, ?)")
      .bind(body.run_id, sourceId, body.mode, body.expected_batches, startHash, now, now, normalizedBase === null ? null : canonical(normalizedBase), canonical(target)).run();
  } catch { throw new RequestError(409, "source_run_active_or_run_conflict"); }
  return json({ accepted: true, run_id: body.run_id, status: "OPEN", expected_batches: body.expected_batches, state_kind: "snapshot", idempotent: false }, 201);
}

async function submitBatch(body: JsonObject, db: D1Database): Promise<Response> {
  if (typeof body.run_id !== "string" || !Array.isArray(body.records) || body.records.length > MAX_BATCH_RECORDS || !integer(body.batch_index)) throw new RequestError(400, "invalid_batch");
  const run = await db.prepare("SELECT r.source_id, r.mode, r.state_kind, r.status, r.expected_batches, s.code AS source FROM ingestion_runs r JOIN sources s ON s.id = r.source_id WHERE r.run_id = ?").bind(body.run_id)
    .first<{ source_id: number; mode: "historical" | "incremental"; state_kind: "checkpoint" | "snapshot"; status: string; expected_batches: number; source: SourceCode }>();
  if (!run) throw new RequestError(404, "run_not_found");
  if (run.status !== "OPEN") throw new RequestError(409, "run_not_open");
  if (body.batch_index >= run.expected_batches) throw new RequestError(400, "invalid_batch_index");
  const pubs = body.records.map((item) => validateStagedInput(item, run.source, run.mode, run.state_kind));
  const inBatch = new Set<string>();
  for (const pub of pubs) { if (inBatch.has(pub.external_id)) throw new RequestError(400, "duplicate_publication_in_batch"); inBatch.add(pub.external_id); }
  const payloadHash = await sha256(canonical(pubs));
  const existingBatch = await db.prepare("SELECT payload_hash, record_count FROM ingestion_batches WHERE run_id = ? AND batch_index = ?").bind(body.run_id, body.batch_index).first<{ payload_hash: string; record_count: number }>();
  if (existingBatch) {
    if (existingBatch.payload_hash !== payloadHash) throw new RequestError(409, "batch_idempotency_conflict");
    return json({ accepted: true, run_id: body.run_id, batch_index: body.batch_index, accepted_records: existingBatch.record_count, idempotent: true });
  }
  const statements: D1PreparedStatement[] = [
    db.prepare("INSERT INTO ingestion_batches (run_id, batch_index, payload_hash, record_count, accepted_at) VALUES (?, ?, ?, ?, ?)")
      .bind(body.run_id, body.batch_index, payloadHash, pubs.length, new Date().toISOString()),
  ];
  let newlyStaged = 0;
  for (const pub of pubs) {
    const encoded = canonical(pub);
    const recordHash = await sha256(encoded);
    const prior = await db.prepare("SELECT payload_hash FROM ingestion_staging WHERE run_id = ? AND source_id = ? AND external_id = ?")
      .bind(body.run_id, run.source_id, pub.external_id).first<{ payload_hash: string }>();
    if (prior && prior.payload_hash !== recordHash) throw new RequestError(409, "publication_batch_conflict");
    if (!prior) {
      newlyStaged += 1;
      statements.push(db.prepare("INSERT INTO ingestion_staging (run_id, source_id, external_id, publication_json, payload_hash, source_complete) VALUES (?, ?, ?, ?, ?, ?)")
        .bind(body.run_id, run.source_id, pub.external_id, encoded, recordHash, "operation" in pub || pub.complete ? 1 : 0));
    }
  }
  statements.push(db.prepare("UPDATE ingestion_runs SET accepted_count = accepted_count + ?, updated_at = ? WHERE run_id = ? AND status = 'OPEN'")
    .bind(newlyStaged, new Date().toISOString(), body.run_id));
  try { await db.batch(statements); } catch { throw new RequestError(409, "batch_conflict"); }
  return json({ accepted: true, run_id: body.run_id, batch_index: body.batch_index, accepted_records: newlyStaged, idempotent: false }, 201);
}

function eventChanges(previous: JsonObject | null, incoming: PublicationInput): Array<{ type: string; previousStatus: string | null; newStatus: string | null; snapshot: JsonObject }> {
  const next = incoming.normalized_snapshot;
  if (!previous) return [{ type: "FIRST_SEEN", previousStatus: null, newStatus: incoming.status, snapshot: { current: next } }];
  const changes: Array<{ type: string; previousStatus: string | null; newStatus: string | null; snapshot: JsonObject }> = [];
  const compare = (type: string, before: unknown, after: unknown, oldStatus: string | null = null, newStatus: string | null = null) => {
    if (canonical(before) !== canonical(after)) changes.push({ type, previousStatus: oldStatus, newStatus, snapshot: { previous: before ?? null, current: after ?? null } });
  };
  const oldStatus = typeof previous.status === "string" ? previous.status : null;
  const newStatus = typeof next.status === "string" ? next.status : null;
  const semanticRows = (value: unknown, fields: string[]): JsonObject[] => Array.isArray(value)
    ? value.filter(object).map((item) => Object.fromEntries(fields.filter((field) => field in item).map((field) => [field, item[field]])))
    : [];
  compare("TITLE_CHANGED", previous.title, next.title);
  compare("CONTENT_CHANGED", previous.content_text, next.content_text);
  compare("STATUS_CHANGED", previous.status, next.status, oldStatus, newStatus);
  compare("PUBLICATION_TYPE_CHANGED", previous.publication_type, next.publication_type);
  // Preserve source timestamps in normalized snapshots, but compare only content fields.
  compare("DOCUMENTS_CHANGED",
    semanticRows(previous.documents, ["id", "link_id", "title", "date_raw", "released_at_raw", "document_released_at_raw"]),
    semanticRows(next.documents, ["id", "link_id", "title", "date_raw", "released_at_raw", "document_released_at_raw"]));
  compare("STAGES_CHANGED",
    semanticRows(previous.stages, ["id", "title", "date_raw", "type_id", "status_id"]),
    semanticRows(next.stages, ["id", "title", "date_raw", "type_id", "status_id"]));
  compare("VOTES_CHANGED",
    semanticRows(previous.votes, ["id", "stage_id", "number", "approved", "conclusion", "comment"]),
    semanticRows(next.votes, ["id", "stage_id", "number", "approved", "conclusion", "comment"]));
  compare("RECLASSIFIED", previous.relevance, next.relevance);
  return changes;
}

async function processStaged(db: D1Database, runId: string, sourceId: number, mode: "historical" | "incremental", staged: { external_id: string; publication_json: string }): Promise<void> {
  const incoming = JSON.parse(staged.publication_json) as StagedInput;
  if (isSourceDelete(incoming)) {
    const now = new Date().toISOString();
    await db.batch([
      db.prepare("UPDATE publications SET source_deleted = 1, updated_at = ? WHERE source_id = ? AND external_id = ? AND source_deleted = 0 AND EXISTS (SELECT 1 FROM ingestion_staging WHERE run_id = ? AND source_id = ? AND external_id = ? AND processed = 0)")
        .bind(now, sourceId, incoming.external_id, runId, sourceId, incoming.external_id),
      db.prepare("UPDATE ingestion_staging SET processed = 1 WHERE run_id = ? AND source_id = ? AND external_id = ? AND processed = 0")
        .bind(runId, sourceId, incoming.external_id),
    ]);
    return;
  }
  const old = await db.prepare("SELECT id, normalized_snapshot_json, source_complete FROM publications WHERE source_id = ? AND external_id = ?")
    .bind(sourceId, incoming.external_id).first<{ id: number; normalized_snapshot_json: string; source_complete: number }>();
  const now = new Date().toISOString();
  if (!incoming.complete) {
    const statements: D1PreparedStatement[] = [];
    if (!old) {
      const related = canonical({ documents: incoming.normalized_snapshot.documents, stages: incoming.normalized_snapshot.stages, votes: incoming.normalized_snapshot.votes });
      statements.push(db.prepare("INSERT INTO publications (source_id, external_id, external_url, original_title, publication_type, relevance, relevance_reason, current_status, published_at, source_updated_at, discovered_at, updated_at, source_metadata_json, normalized_snapshot_json, relationship_snapshot_json, ruleset_version, source_complete, historical, diagnostics_json) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ? WHERE EXISTS (SELECT 1 FROM ingestion_staging WHERE run_id = ? AND source_id = ? AND external_id = ? AND processed = 0)")
        .bind(sourceId, incoming.external_id, incoming.url, incoming.title, incoming.publication_type, incoming.relevance, incoming.relevance_reason, incoming.status, incoming.published_at, incoming.source_updated_at, now, now, canonical(incoming.source_metadata), canonical(incoming.normalized_snapshot), related, incoming.ruleset_version, incoming.historical ? 1 : 0, canonical(incoming.diagnostics), runId, sourceId, incoming.external_id));
    }
    statements.push(
      db.prepare("UPDATE ingestion_staging SET processed = 1 WHERE run_id = ? AND source_id = ? AND external_id = ? AND processed = 0").bind(runId, sourceId, incoming.external_id),
    );
    await db.batch(statements);
    return;
  }

  let oldSnapshot: JsonObject | null = null;
  if (old?.source_complete === 1) {
    try { oldSnapshot = JSON.parse(old.normalized_snapshot_json) as JsonObject; }
    catch { throw new RequestError(500, "stored_snapshot_corrupt"); }
  }
  const changes = eventChanges(oldSnapshot, incoming);
  const meaningful = !old || old.source_complete !== 1 || changes.length > 0;
  const normalizedJson = canonical(incoming.normalized_snapshot);
  const relationshipsJson = canonical({
    documents: incoming.normalized_snapshot.documents,
    stages: incoming.normalized_snapshot.stages,
    votes: incoming.normalized_snapshot.votes,
  });
  const writePublication = old
    ? db.prepare("UPDATE publications SET external_url = ?, original_title = ?, publication_type = ?, relevance = ?, relevance_reason = ?, current_status = ?, published_at = ?, source_updated_at = ?, updated_at = CASE WHEN ? = 1 THEN ? ELSE updated_at END, source_metadata_json = ?, normalized_snapshot_json = ?, relationship_snapshot_json = ?, ruleset_version = ?, source_complete = 1, historical = ?, diagnostics_json = ?, source_deleted = 0 WHERE id = ? AND EXISTS (SELECT 1 FROM ingestion_staging WHERE run_id = ? AND source_id = ? AND external_id = ? AND processed = 0)")
      .bind(incoming.url, incoming.title, incoming.publication_type, incoming.relevance, incoming.relevance_reason, incoming.status, incoming.published_at, incoming.source_updated_at, meaningful ? 1 : 0, now, canonical(incoming.source_metadata), normalizedJson, relationshipsJson, incoming.ruleset_version, incoming.historical ? 1 : 0, canonical(incoming.diagnostics), old.id, runId, sourceId, incoming.external_id)
    : db.prepare("INSERT INTO publications (source_id, external_id, external_url, original_title, publication_type, relevance, relevance_reason, current_status, published_at, source_updated_at, discovered_at, updated_at, source_metadata_json, normalized_snapshot_json, relationship_snapshot_json, ruleset_version, source_complete, historical, diagnostics_json) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ? WHERE EXISTS (SELECT 1 FROM ingestion_staging WHERE run_id = ? AND source_id = ? AND external_id = ? AND processed = 0)")
      .bind(sourceId, incoming.external_id, incoming.url, incoming.title, incoming.publication_type, incoming.relevance, incoming.relevance_reason, incoming.status, incoming.published_at, incoming.source_updated_at, now, now, canonical(incoming.source_metadata), normalizedJson, relationshipsJson, incoming.ruleset_version, incoming.historical ? 1 : 0, canonical(incoming.diagnostics), runId, sourceId, incoming.external_id);
  const statements: D1PreparedStatement[] = [writePublication];
  const canNotify = mode === "incremental" && !incoming.historical && incoming.complete &&
    !incoming.notification_blocked && incoming.relevance !== "IRRELEVANT";
  for (const change of changes) {
    const eventNotifyEligible = canNotify && !(incoming.source === "hoeringsportalen" && change.type === "FIRST_SEEN");
    const fingerprint = await sha256(canonical({ type: change.type, snapshot: change.snapshot }));
    const eventKey = "ingest:" + runId + ":" + sourceId + ":" + incoming.external_id + ":" + change.type + ":" + fingerprint;
    statements.push(db.prepare("INSERT INTO publication_events (publication_id, event_key, event_type, previous_status, new_status, occurred_at, detected_at, notify_eligible, change_snapshot_json) SELECT id, ?, ?, ?, ?, NULL, ?, ?, ? FROM publications WHERE source_id = ? AND external_id = ? AND EXISTS (SELECT 1 FROM ingestion_staging WHERE run_id = ? AND source_id = ? AND external_id = ? AND processed = 0) ON CONFLICT (event_key) DO NOTHING")
      .bind(eventKey, change.type, change.previousStatus, change.newStatus, now, eventNotifyEligible ? 1 : 0, canonical(change.snapshot), sourceId, incoming.external_id, runId, sourceId, incoming.external_id));
  }
  statements.push(
    db.prepare("UPDATE ingestion_staging SET processed = 1 WHERE run_id = ? AND source_id = ? AND external_id = ? AND processed = 0").bind(runId, sourceId, incoming.external_id),
  );
  // Every write is gated on the staged row remaining pending. Competing commit batches serialize in SQLite;
  // only the winner changes publications/events and the stage transition trigger counts the record once.
  await db.batch(statements);
}

async function commitRun(body: JsonObject, db: D1Database): Promise<Response> {
  if (typeof body.run_id !== "string") throw new RequestError(400, "invalid_run_id");
  const run = await db.prepare("SELECT run_id, source_id, mode, expected_batches, status, proposed_checkpoints_json, base_checkpoints_json, had_incomplete, state_kind, base_snapshot_json, target_snapshot_json FROM ingestion_runs WHERE run_id = ?").bind(body.run_id)
    .first<{ run_id: string; source_id: number; mode: "historical" | "incremental"; expected_batches: number; status: string; proposed_checkpoints_json: string; base_checkpoints_json: string; had_incomplete: number; state_kind: "checkpoint" | "snapshot"; base_snapshot_json: string | null; target_snapshot_json: string | null }>();
  if (!run) throw new RequestError(404, "run_not_found");
  if (run.status === "COMMITTED" || run.status === "BLOCKED") {
    if (run.state_kind === "checkpoint") return json({ run_id: run.run_id, status: run.status, processed_records: 0, checkpoints_advanced: run.status === "COMMITTED", idempotent: true });
    if (run.status === "BLOCKED") return json({ run_id: run.run_id, status: run.status, processed_records: 0, checkpoints_advanced: false, snapshot_advanced: false, idempotent: true });
    if (run.target_snapshot_json === null) throw new RequestError(500, "snapshot_run_state_corrupt");
    const target = snapshotIdentityFromJson(run.target_snapshot_json);
    const generation = await db.prepare("SELECT COUNT(*) AS generation FROM ingestion_runs WHERE source_id = ? AND state_kind = 'snapshot' AND status = 'COMMITTED' AND json_extract(target_snapshot_json, '$.backup_finish_date') <= ?")
      .bind(run.source_id, target.backup_finish_date).first<{ generation: number }>();
    const saved = await db.prepare("SELECT generation FROM source_snapshot_states WHERE source_id = ?").bind(run.source_id).first<{ generation: number }>();
    if (!generation || generation.generation < 1 || !saved || saved.generation < generation.generation) throw new RequestError(500, "snapshot_commit_state_missing");
    return json({ run_id: run.run_id, status: run.status, processed_records: 0, checkpoints_advanced: false, snapshot_advanced: true, snapshot_generation: generation.generation, idempotent: true });
  }
  if (run.status !== "OPEN" && run.status !== "COMMITTING") throw new RequestError(409, "run_not_committable");
  const count = await db.prepare("SELECT COUNT(*) AS count FROM ingestion_batches WHERE run_id = ?").bind(run.run_id).first<{ count: number }>();
  if (count?.count !== run.expected_batches) throw new RequestError(409, "batches_incomplete");
  if (run.status === "OPEN") await db.prepare("UPDATE ingestion_runs SET status = 'COMMITTING', updated_at = ? WHERE run_id = ? AND status = 'OPEN'")
    .bind(new Date().toISOString(), run.run_id).run();
  const pending = await db.prepare("SELECT external_id, publication_json FROM ingestion_staging WHERE run_id = ? AND processed = 0 ORDER BY external_id LIMIT ?")
    .bind(run.run_id, COMMIT_RECORDS).all<{ external_id: string; publication_json: string }>();
  let processed = 0;
  for (const item of pending.results ?? []) {
    await processStaged(db, run.run_id, run.source_id, run.mode, item);
    processed += 1;
  }
  const remaining = await db.prepare("SELECT COUNT(*) AS count FROM ingestion_staging WHERE run_id = ? AND processed = 0").bind(run.run_id).first<{ count: number }>();
  if ((remaining?.count ?? 0) > 0) return json({
    run_id: run.run_id, status: "COMMITTING", processed_records: processed, remaining_records: remaining?.count ?? 0,
    checkpoints_advanced: false, ...(run.state_kind === "snapshot" ? { snapshot_advanced: false } : {}),
  });
  const state = await db.prepare("SELECT had_incomplete FROM ingestion_runs WHERE run_id = ?").bind(run.run_id).first<{ had_incomplete: number }>();
  if (state?.had_incomplete) {
    const now = new Date().toISOString();
    await db.prepare("UPDATE ingestion_runs SET status = 'BLOCKED', error_code = 'incomplete_publication', completed_at = ?, updated_at = ? WHERE run_id = ? AND status = 'COMMITTING'")
      .bind(now, now, run.run_id).run();
    return json({ run_id: run.run_id, status: "BLOCKED", processed_records: processed, checkpoints_advanced: false, ...(run.state_kind === "snapshot" ? { snapshot_advanced: false } : {}), error: "incomplete_publication" });
  }
  if (run.state_kind === "snapshot") {
    if (run.target_snapshot_json === null) throw new RequestError(500, "snapshot_run_state_corrupt");
    const target = snapshotIdentityFromJson(run.target_snapshot_json);
    const now = new Date().toISOString();
    const statements: D1PreparedStatement[] = [];
    if (run.mode === "historical") {
      if (run.base_snapshot_json !== null) throw new RequestError(500, "snapshot_run_state_corrupt");
      // The existing historical-baseline gate remains authoritative; preserve any earlier baseline.
      statements.push(db.prepare("INSERT INTO source_ingestion_baselines (source_id, baseline_run_id, initialized_at) VALUES (?, ?, ?) ON CONFLICT (source_id) DO NOTHING")
        .bind(run.source_id, run.run_id, now));
      statements.push(db.prepare("INSERT INTO source_snapshot_states (source_id, backup_sha256, backup_finish_date, schema_fingerprint, generation, last_run_id, initialized_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)")
        .bind(run.source_id, target.backup_sha256, target.backup_finish_date, target.schema_fingerprint, run.run_id, now, now));
    } else {
      if (run.base_snapshot_json === null) throw new RequestError(500, "snapshot_run_state_corrupt");
      const base = snapshotIdentityFromJson(run.base_snapshot_json);
      statements.push(db.prepare("UPDATE source_snapshot_states SET backup_sha256 = ?, backup_finish_date = ?, schema_fingerprint = ?, generation = generation + 1, last_run_id = ?, updated_at = ? WHERE source_id = ? AND backup_sha256 = ? AND backup_finish_date = ? AND schema_fingerprint = ?")
        .bind(target.backup_sha256, target.backup_finish_date, target.schema_fingerprint, run.run_id, now, run.source_id, base.backup_sha256, base.backup_finish_date, base.schema_fingerprint));
    }
    statements.push(db.prepare("UPDATE ingestion_runs SET status = 'COMMITTED', completed_at = ?, updated_at = ? WHERE run_id = ? AND status = 'COMMITTING' AND had_incomplete = 0")
      .bind(now, now, run.run_id));
    try { await db.batch(statements); }
    catch { throw new RequestError(409, "snapshot_commit_conflict"); }
    const committed = await db.prepare("SELECT status, snapshot_state_writes FROM ingestion_runs WHERE run_id = ?").bind(run.run_id)
      .first<{ status: string; snapshot_state_writes: number }>();
    const saved = await db.prepare("SELECT generation, backup_sha256, backup_finish_date, schema_fingerprint, last_run_id FROM source_snapshot_states WHERE source_id = ?").bind(run.source_id)
      .first<{ generation: number; backup_sha256: string; backup_finish_date: string; schema_fingerprint: string; last_run_id: string }>();
    if (committed?.status !== "COMMITTED" || committed.snapshot_state_writes !== 1 || !saved || saved.last_run_id !== run.run_id ||
      saved.backup_sha256 !== target.backup_sha256 || saved.backup_finish_date !== target.backup_finish_date || saved.schema_fingerprint !== target.schema_fingerprint) {
      throw new RequestError(409, "snapshot_commit_conflict");
    }
    return json({ run_id: run.run_id, status: "COMMITTED", processed_records: processed, checkpoints_advanced: false, snapshot_advanced: true, snapshot_generation: saved.generation });
  }
  const proposed = JSON.parse(run.proposed_checkpoints_json) as Record<string, Cursor | null>;
  const base = JSON.parse(run.base_checkpoints_json) as Record<string, Cursor | null>;
  const current = await readStates(db, run.source_id);
  for (const key of Object.keys(proposed)) {
    if (canonical(current.base[key] ?? null) !== canonical(base[key] ?? null)) {
      const now = new Date().toISOString();
      await db.prepare("UPDATE ingestion_runs SET status = 'BLOCKED', error_code = 'checkpoint_conflict', completed_at = ?, updated_at = ? WHERE run_id = ?")
        .bind(now, now, run.run_id).run();
      throw new RequestError(409, "checkpoint_conflict");
    }
  }
  const now = new Date().toISOString();
  const statements: D1PreparedStatement[] = [];
  for (const [key, value] of Object.entries(proposed)) {
    statements.push(db.prepare("INSERT INTO source_states (source_id, stream_key, checkpoint, last_success_at, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (source_id, stream_key) DO UPDATE SET checkpoint = excluded.checkpoint, last_success_at = excluded.last_success_at, updated_at = excluded.updated_at WHERE source_states.checkpoint IS ?")
      .bind(run.source_id, key, value === null ? null : canonical(value), now, now, base[key] == null ? null : canonical(base[key])));
  }
  if (run.mode === "historical") statements.push(db.prepare("INSERT INTO source_ingestion_baselines (source_id, baseline_run_id, initialized_at) VALUES (?, ?, ?) ON CONFLICT (source_id) DO NOTHING")
    .bind(run.source_id, run.run_id, now));
  statements.push(db.prepare("UPDATE ingestion_runs SET status = 'COMMITTED', completed_at = ?, updated_at = ? WHERE run_id = ? AND status = 'COMMITTING' AND had_incomplete = 0")
    .bind(now, now, run.run_id));
  await db.batch(statements);
  const finalState = await db.prepare("SELECT status FROM ingestion_runs WHERE run_id = ?").bind(run.run_id).first<{ status: string }>();
  if (finalState?.status !== "COMMITTED") throw new RequestError(409, "checkpoint_commit_failed");
  return json({ run_id: run.run_id, status: "COMMITTED", processed_records: processed, checkpoints_advanced: true });
}

async function abortRun(body: JsonObject, db: D1Database): Promise<Response> {
  if (typeof body.run_id !== "string") throw new RequestError(400, "invalid_run_id");
  const run = await db.prepare("SELECT status FROM ingestion_runs WHERE run_id = ?").bind(body.run_id).first<{ status: string }>();
  if (!run) throw new RequestError(404, "run_not_found");
  if (run.status === "ABORTED") return json({ run_id: body.run_id, status: "ABORTED", idempotent: true });
  if (run.status !== "OPEN") throw new RequestError(409, "run_cannot_be_aborted");
  const now = new Date().toISOString();
  await db.prepare("UPDATE ingestion_runs SET status = 'ABORTED', error_code = 'client_aborted', completed_at = ?, updated_at = ? WHERE run_id = ? AND status = 'OPEN'")
    .bind(now, now, body.run_id).run();
  return json({ run_id: body.run_id, status: "ABORTED", checkpoints_advanced: false });
}

async function cleanupStaging(body: JsonObject, db: D1Database): Promise<Response> {
  const retentionDays = body.retention_days === undefined ? DEFAULT_INGESTION_RETENTION_DAYS : body.retention_days;
  if (!integer(retentionDays) || retentionDays < 1 || retentionDays > 3650) throw new RequestError(400, "invalid_retention_days");
  if (body.dry_run !== undefined && typeof body.dry_run !== "boolean") throw new RequestError(400, "invalid_cleanup_mode");
  const dryRun = body.dry_run !== false;
  if (!dryRun && body.confirm !== "DELETE_STAGING") throw new RequestError(400, "cleanup_confirmation_required");
  const cutoff = new Date(Date.now() - retentionDays * 24 * 60 * 60 * 1000).toISOString();
  const summary = await db.prepare(`
    SELECT COUNT(*) AS runs,
      COALESCE(SUM((SELECT COUNT(*) FROM ingestion_staging st WHERE st.run_id = r.run_id)), 0) AS staged_records,
      COALESCE(SUM((SELECT COUNT(*) FROM ingestion_batches b WHERE b.run_id = r.run_id)), 0) AS batches
    FROM ingestion_runs r
    WHERE r.status IN ('COMMITTED', 'ABORTED') AND r.completed_at IS NOT NULL AND r.completed_at <= ?
      AND (EXISTS (SELECT 1 FROM ingestion_staging st WHERE st.run_id = r.run_id)
        OR EXISTS (SELECT 1 FROM ingestion_batches b WHERE b.run_id = r.run_id))
  `).bind(cutoff).first<{ runs: number; staged_records: number; batches: number }>();
  if (dryRun) return json({
    dry_run: true, retention_days: retentionDays, cutoff, eligible_runs: summary?.runs ?? 0,
    staged_records: summary?.staged_records ?? 0, batches: summary?.batches ?? 0,
    deletion_limit_runs: MAX_CLEANUP_RUNS, deleted_runs: 0,
  });

  const candidates = await db.prepare("SELECT run_id FROM ingestion_runs r WHERE r.status IN ('COMMITTED', 'ABORTED') AND r.completed_at IS NOT NULL AND r.completed_at <= ? AND (EXISTS (SELECT 1 FROM ingestion_staging st WHERE st.run_id = r.run_id) OR EXISTS (SELECT 1 FROM ingestion_batches b WHERE b.run_id = r.run_id)) ORDER BY r.completed_at, r.run_id LIMIT ?")
    .bind(cutoff, MAX_CLEANUP_RUNS).all<{ run_id: string }>();
  let deletedRuns = 0;
  for (const candidate of candidates.results ?? []) {
    const guard = "EXISTS (SELECT 1 FROM ingestion_runs r WHERE r.run_id = ? AND r.status IN ('COMMITTED', 'ABORTED') AND r.completed_at IS NOT NULL AND r.completed_at <= ?)";
    await db.batch([
      db.prepare(`DELETE FROM ingestion_staging WHERE run_id = ? AND ${guard}`).bind(candidate.run_id, candidate.run_id, cutoff),
      db.prepare(`DELETE FROM ingestion_batches WHERE run_id = ? AND ${guard}`).bind(candidate.run_id, candidate.run_id, cutoff),
    ]);
    deletedRuns += 1;
  }
  const remaining = await db.prepare("SELECT COUNT(*) AS count FROM ingestion_runs r WHERE r.status IN ('COMMITTED', 'ABORTED') AND r.completed_at IS NOT NULL AND r.completed_at <= ? AND (EXISTS (SELECT 1 FROM ingestion_staging st WHERE st.run_id = r.run_id) OR EXISTS (SELECT 1 FROM ingestion_batches b WHERE b.run_id = r.run_id))")
    .bind(cutoff).first<{ count: number }>();
  return json({
    dry_run: false, retention_days: retentionDays, cutoff, eligible_runs: summary?.runs ?? 0,
    deleted_runs: deletedRuns, remaining_eligible_runs: remaining?.count ?? 0,
    deletion_limit_runs: MAX_CLEANUP_RUNS,
  });
}

export async function handleIngestionRequest(request: Request, env: IngestEnv): Promise<Response> {
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  return ingest(request, env);
}
export async function handleSourceStatesRequest(request: Request, env: IngestEnv): Promise<Response> {
  if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405);
  return sourceStates(request, env);
}

type SnapshotReadiness = {
  source: "folketinget";
  schema_ready: boolean;
  snapshot_state_initialized: boolean | null;
  snapshot_generation: number | null;
  active_ingestion_run: boolean | null;
  worker_supports_snapshot_ingestion: true;
  historical_snapshot_start_allowed: boolean;
};

function snapshotReadiness(
  schemaReady: boolean,
  initialized: boolean | null = null,
  generation: number | null = null,
  active: boolean | null = null,
): SnapshotReadiness {
  return {
    source: "folketinget",
    schema_ready: schemaReady,
    snapshot_state_initialized: initialized,
    snapshot_generation: generation,
    active_ingestion_run: active,
    worker_supports_snapshot_ingestion: true,
    historical_snapshot_start_allowed: schemaReady && initialized === false && active === false,
  };
}

async function inspectSnapshotReadiness(db: D1Database): Promise<SnapshotReadiness> {
  try {
    const requiredTables = Object.keys(READINESS_TABLE_COLUMNS);
    const tablesResult = await db.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('publications', 'sources', 'ingestion_runs', 'source_states', 'source_snapshot_states')",
    ).all<{ name: string }>();
    const existingTables = new Set((tablesResult.results ?? []).map((row) => row.name));
    if (requiredTables.some((table) => !existingTables.has(table))) return snapshotReadiness(false);

    for (const [table, expectedColumns] of Object.entries(READINESS_TABLE_COLUMNS)) {
      const columnsResult = await db.prepare("SELECT name FROM pragma_table_info(?)").bind(table).all<{ name: string }>();
      const columns = new Set((columnsResult.results ?? []).map((row) => row.name));
      if (expectedColumns.some((column) => !columns.has(column))) return snapshotReadiness(false);
    }
    const triggerResult = await db.prepare(
      "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name IN (" + SNAPSHOT_STATE_TRIGGERS.map(() => "?").join(",") + ")",
    ).bind(...SNAPSHOT_STATE_TRIGGERS).all<{ name: string }>();
    const triggers = new Set((triggerResult.results ?? []).map((row) => row.name));
    if (SNAPSHOT_STATE_TRIGGERS.some((trigger) => !triggers.has(trigger))) return snapshotReadiness(false);

    const source = await db.prepare("SELECT id FROM sources WHERE code = ?").bind("folketinget").first<{ id: number }>();
    if (!source || !integer(source.id)) return snapshotReadiness(false);
    const state = await db.prepare(
      "SELECT generation FROM source_snapshot_states WHERE source_id = ?",
    ).bind(source.id).first<{ generation: number }>();
    if (state && !integer(state.generation)) return snapshotReadiness(false);
    const active = await db.prepare(
      "SELECT COUNT(*) AS count FROM ingestion_runs WHERE source_id = ? AND status IN ('OPEN', 'COMMITTING')",
    ).bind(source.id).first<{ count: number }>();
    if (!active || !integer(active.count)) return snapshotReadiness(false);
    return snapshotReadiness(true, Boolean(state), state?.generation ?? null, active.count > 0);
  } catch {
    // Metadata/query errors are intentionally reduced to a closed readiness result.
    return snapshotReadiness(false);
  }
}

export async function handleSnapshotReadinessRequest(request: Request, env: IngestEnv): Promise<Response> {
  if (!await isIngestionRequestAuthorized(request, env)) return json({ error: "unauthorized" }, 401);
  if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405);
  if (new URL(request.url).searchParams.get("source") !== "folketinget") return json({ error: "invalid_source" }, 400);
  return json(await inspectSnapshotReadiness(env.DB));
}
