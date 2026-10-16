import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { after, before, test } from "node:test";

const token = "local-test-ingestion-token-only";
const wrangler = new URL("../node_modules/wrangler/bin/wrangler.js", import.meta.url).pathname;
let server: ChildProcess;
let persistPath: string;
let baseUrl: string;
let serverPort: number;
let workerOutput = "";

async function request(path: string, method: string, body?: unknown, auth = true): Promise<{ status: number; body: any }> {
  let response: Response;
  try { response = await fetch(baseUrl + path, {
    method,
    headers: {
      ...(auth ? { Authorization: "Bearer " + token } : {}),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: typeof body === "string" ? body : JSON.stringify(body) }),
  }); } catch (error) { throw new Error("local request failed at " + path + ": " + workerOutput.slice(-2000), { cause: error }); }
  let result: any = null;
  try { result = await response.json(); } catch { /* empty response */ }
  return { status: response.status, body: result };
}

function cursors(id: number): Record<string, unknown> {
  return Object.fromEntries(["Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"].map((name) => [
    name, { timestamp: "2026-10-16T10:11:12.1234567", id },
  ]));
}
function publication(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const title = overrides.title ?? "Forslag om Ukrainesærloven";
  const status = overrides.status ?? "Fremsat";
  const snapshot = overrides.normalized_snapshot ?? {
    title, content_text: null, status, publication_type: "L 1", relevance: overrides.relevance ?? "DIRECT",
    documents: [{ id: 501, link_id: 701, title: "Lovudkast" }],
    stages: [{ id: 601, title: "Fremsat", date_raw: "2026-10-01", type_id: 1, status_id: 2 }],
    votes: [{ id: 801, stage_id: 601, number: 1, approved: true, conclusion: "Vedtaget", comment: null }],
  };
  return {
    source: "folketinget", external_id: "42", title, url: "https://oda.ft.dk/api/Sag(42)",
    relevance: overrides.relevance ?? "DIRECT", relevance_reason: "direct match", ruleset_version: "1.1",
    status, publication_type: "L 1", published_at: null, source_updated_at: null,
    source_updated_at_raw: "2026-10-16T10:11:12.1234567", historical: overrides.historical ?? false,
    notification_blocked: overrides.notification_blocked ?? false, complete: overrides.complete ?? true,
    diagnostics: overrides.diagnostics ?? [], source_metadata: { source_updated_at_raw: "2026-10-16T10:11:12.1234567", number: "L 1" },
    normalized_snapshot: snapshot,
    ...overrides,
  };
}

async function start(runId: string, options: { mode?: string; expected?: number; proposed?: Record<string, unknown>; source?: string } = {}) {
  return request("/internal/ingest", "POST", {
    action: "start", run_id: runId, source: options.source ?? "folketinget", mode: options.mode ?? "incremental",
    expected_batches: options.expected ?? 1, collection_complete: true, proposed_checkpoints: options.proposed ?? cursors(1),
  });
}
async function batch(runId: string, records: unknown[], index = 0) {
  return request("/internal/ingest", "POST", { action: "batch", run_id: runId, batch_index: index, records });
}
async function commit(runId: string) {
  return request("/internal/ingest", "POST", { action: "commit", run_id: runId });
}
async function stopWorker(): Promise<void> {
  if (server && server.exitCode === null) {
    await new Promise<void>((resolve) => { server.once("exit", () => resolve()); server.kill("SIGTERM"); });
  }
}
async function startWorker(): Promise<void> {
  const cwd = new URL("..", import.meta.url).pathname;
  workerOutput = "";
  server = spawn(process.execPath, [wrangler, "dev", "--local", "--persist-to", persistPath, "--port", String(serverPort), "--ip", "127.0.0.1", "--var", "INGEST_API_TOKEN:" + token], { cwd, stdio: ["ignore", "pipe", "pipe"] });
  server.stdout?.on("data", (chunk: Buffer) => { workerOutput = (workerOutput + chunk.toString()).slice(-4000); });
  server.stderr?.on("data", (chunk: Buffer) => { workerOutput = (workerOutput + chunk.toString()).slice(-4000); });
  baseUrl = "http://127.0.0.1:" + serverPort;
  let ready = false;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (server.exitCode !== null && server.exitCode !== undefined) throw new Error("local Worker exited before starting: " + workerOutput.slice(-2000));
    try { ready = (await fetch(baseUrl + "/health")).status === 200; } catch { /* server is starting */ }
    if (ready) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("local Worker did not start: " + workerOutput.slice(-2000));
}
async function query(sql: string): Promise<any[]> {
  await stopWorker();
  try {
    const output = execFileSync(process.execPath, [wrangler, "d1", "execute", "denmarktgbot-db", "--local", "--persist-to", persistPath, "--json", "--command", sql], { encoding: "utf8", cwd: new URL("..", import.meta.url).pathname });
    const parsed = JSON.parse(output) as any;
    return Array.isArray(parsed) ? parsed[0]?.results ?? [] : parsed.results ?? [];
  } finally { await startWorker(); }
}
async function sqlWithoutRestart(sql: string): Promise<any[]> {
  const output = execFileSync(process.execPath, [wrangler, "d1", "execute", "denmarktgbot-db", "--local", "--persist-to", persistPath, "--json", "--command", sql], { encoding: "utf8", cwd: new URL("..", import.meta.url).pathname });
  const parsed = JSON.parse(output) as any;
  return Array.isArray(parsed) ? parsed.map((item) => item.results ?? []) : parsed.results ?? [];
}

before(async () => {
  persistPath = await mkdtemp(join(tmpdir(), "denmarktgbot-d1-"));
  const cwd = new URL("..", import.meta.url).pathname;
  execFileSync(process.execPath, [wrangler, "d1", "migrations", "apply", "denmarktgbot-db", "--local", "--persist-to", persistPath], { cwd, stdio: "ignore" });
  const socket = createServer();
  await new Promise<void>((resolve) => socket.listen(0, "127.0.0.1", resolve));
  const address = socket.address();
  if (!address || typeof address === "string") throw new Error("test port unavailable");
  const port = address.port;
  serverPort = port;
  await new Promise<void>((resolve, reject) => socket.close((error) => error ? reject(error) : resolve()));
  await startWorker();
});
after(async () => {
  await stopWorker();
  if (persistPath) await rm(persistPath, { recursive: true, force: true });
});

test("internal routes authenticate before reading state; reject invalid JSON and unknown sources", async () => {
  const denied = await request("/internal/source-states?source=folketinget", "GET", undefined, false);
  assert.equal(denied.status, 401);
  assert.equal((await request("/internal/ingest", "POST", "{")).status, 400);
  assert.equal((await request("/internal/ingest", "POST", "x".repeat(256 * 1024 + 1))).status, 413);
  assert.equal((await request("/internal/source-states?source=unknown", "GET")).status, 400);
  assert.equal((await request("/internal/ingest", "POST", { action: "start", source: "duplicate" })).status, 400);
  const invalidId = await request("/internal/ingest", "POST", { action: "start", run_id: "00000000-0000-4000-8000-000000000021", source: "folketinget", mode: "incremental", expected_batches: 1, collection_complete: true, proposed_checkpoints: {} });
  assert.equal(invalidId.body.error, "invalid_checkpoint_streams");
});

test("valid writes persist once; batch retry and identical reingestion do not duplicate events or modify timestamps", async () => {
  const run = "00000000-0000-4000-8000-000000000001";
  assert.equal((await start(run)).status, 201);
  const first = await batch(run, [publication()]);
  assert.equal(first.status, 201);
  assert.equal((await batch(run, [publication()])).body.idempotent, true);
  assert.equal((await commit(run)).body.checkpoints_advanced, true);

  const same = "00000000-0000-4000-8000-000000000002";
  await start(same, { proposed: cursors(2) });
  await batch(same, [publication({ source_metadata: { feed_codes: ["immigration_category"] } }), publication({ external_id: "101" })]);
  await commit(same);
  const states = await request("/internal/source-states?source=folketinget", "GET");
  assert.deepEqual(states.body.streams.find((item: any) => item.stream === "Sag")?.checkpoint, { timestamp: "2026-10-16T10:11:12.1234567", id: 2 });
});

test("meaningful title, status, relationships and classification changes create typed events", async () => {
  const next = publication({
    title: "Forslag om udvidet Ukrainesærloven",
    status: "Vedtaget",
    relevance: "POTENTIAL",
    normalized_snapshot: {
      title: "Forslag om udvidet Ukrainesærloven", content_text: null, status: "Vedtaget", publication_type: "L 1", relevance: "POTENTIAL",
      documents: [{ id: 501, link_id: 701, title: "Lovudkast" }, { id: 502, link_id: 702, title: "Nyt bilag" }],
      stages: [{ id: 601, title: "Fremsat", date_raw: "2026-10-01", type_id: 1, status_id: 2 }, { id: 602, title: "Afstemning", date_raw: "2026-10-12", type_id: 2, status_id: 3 }],
      votes: [{ id: 801, stage_id: 601, number: 1, approved: true, conclusion: "Vedtaget", comment: null }, { id: 802, stage_id: 602, number: 2, approved: false, conclusion: "Forkastet", comment: null }],
    },
  });
  const run = "00000000-0000-4000-8000-000000000003";
  await start(run, { proposed: cursors(3) });
  await batch(run, [next, publication({ external_id: "101" })]);
  await commit(run);
  assert.equal((await commit(run)).body.status, "COMMITTED");
});

test("historical events cannot notify and incomplete snapshots cannot overwrite complete data or advance checkpoints", async () => {
  const histRun = "00000000-0000-4000-8000-000000000004";
  await start(histRun, { mode: "historical", proposed: cursors(4) });
  const historical = publication({ external_id: "99", historical: true });
  await batch(histRun, [historical]);
  await commit(histRun);
  const beforeState = (await request("/internal/source-states?source=folketinget", "GET")).body.streams.find((item: any) => item.stream === "Sag")?.checkpoint;
  const blockedRun = "00000000-0000-4000-8000-000000000005";
  await start(blockedRun, { proposed: cursors(5) });
  await batch(blockedRun, [publication({ title: "Partial title", complete: false, notification_blocked: true, diagnostics: [{ code: "partial" }] })]);
  const blocked = await commit(blockedRun);
  assert.equal(blocked.body.status, "BLOCKED");
  assert.equal(blocked.body.checkpoints_advanced, false);
  const afterState = (await request("/internal/source-states?source=folketinget", "GET")).body.streams.find((item: any) => item.stream === "Sag")?.checkpoint;
  assert.deepEqual(afterState, beforeState);
});

test("incomplete collections, missing batches, retries and overlapping runs cannot move checkpoints", async () => {
  const partial = await request("/internal/ingest", "POST", {
    action: "start", run_id: "00000000-0000-4000-8000-000000000006", source: "folketinget",
    mode: "incremental", expected_batches: 1, collection_complete: false, proposed_checkpoints: cursors(6),
  });
  assert.equal(partial.status, 409);
  const run = "00000000-0000-4000-8000-000000000007";
  assert.equal((await start(run, { expected: 2, proposed: cursors(7) })).status, 201);
  const overlapping = "00000000-0000-4000-8000-000000000008";
  assert.equal((await start(overlapping, { proposed: cursors(8) })).status, 409);
  assert.equal((await commit(run)).body.error, "batches_incomplete");
  await batch(run, [publication({ external_id: "100" })], 0);
  await batch(run, [], 1);
  assert.equal((await batch(run, [publication({ external_id: "100" })], 0)).body.idempotent, true);
  assert.equal((await commit(run)).body.status, "COMMITTED");
  assert.equal((await start(overlapping, { proposed: cursors(8) })).status, 201);
  const openState = (await request("/internal/source-states?source=folketinget", "GET")).body.streams.find((item: any) => item.stream === "Sag")?.checkpoint;
  assert.deepEqual(openState, { timestamp: "2026-10-16T10:11:12.1234567", id: 7 });
  assert.equal((await request("/internal/ingest", "POST", { action: "abort", run_id: overlapping })).body.status, "ABORTED");
});

test("a failed D1 event write rolls back publication and checkpoint writes; retry resumes safely", async () => {
  await query("CREATE TRIGGER fail_ingest_event BEFORE INSERT ON publication_events WHEN NEW.event_key LIKE 'ingest:%00000000-0000-4000-8000-000000000011%' BEGIN SELECT RAISE(ABORT, 'test failure'); END");
  const run = "00000000-0000-4000-8000-000000000011";
  await start(run, { proposed: cursors(11) });
  await batch(run, [publication({ external_id: "777" })]);
  const failed = await commit(run);
  assert.equal(failed.status, 500);
  const current = (await request("/internal/source-states?source=folketinget", "GET")).body.streams.find((item: any) => item.stream === "Sag")?.checkpoint;
  assert.deepEqual(current, { timestamp: "2026-10-16T10:11:12.1234567", id: 7 });
  await query("DROP TRIGGER fail_ingest_event");
  assert.equal((await commit(run)).body.status, "COMMITTED");
});

test("source separation, timestamp precision and existing Telegram routes remain intact", async () => {
  const hearRun = "00000000-0000-4000-8000-000000000009";
  const hpCursors = { immigration_ministry: null, immigration_category: null };
  assert.equal((await start(hearRun, { source: "hoeringsportalen", proposed: hpCursors })).status, 201);
  const hp = {
    source: "hoeringsportalen", external_id: "123", title: "Ukrainesærloven", url: "https://hoeringsportalen.dk/Hearing/Details/123",
    relevance: "DIRECT", relevance_reason: "direct", ruleset_version: "1.1", status: null, publication_type: null,
    published_at: null, source_updated_at: "2026-10-16T10:11:12.123456Z", source_updated_at_raw: "2026-10-16T10:11:12.123456Z",
    historical: false, notification_blocked: false, complete: true, diagnostics: [], source_metadata: { feed_codes: ["immigration_ministry"] },
    normalized_snapshot: { title: "Ukrainesærloven", content_text: null, status: null, publication_type: null, relevance: "DIRECT", documents: [], stages: [], votes: [] },
  };
  await batch(hearRun, [hp]);
  await commit(hearRun);
  const precision = await request("/internal/ingest", "POST", {
    action: "start", run_id: "00000000-0000-4000-8000-000000000010", source: "folketinget",
    mode: "incremental", expected_batches: 1, collection_complete: true,
    proposed_checkpoints: { ...cursors(9), Sag: { timestamp: "2026-10-16T10:11:12.1234567", id: 7 } },
  });
  assert.equal(precision.status, 409);
  assert.equal((await fetch(baseUrl + "/health")).status, 200);
  assert.equal((await fetch(baseUrl + "/telegram/webhook", { method: "POST" })).status, 503);
});

test("local D1 stores deduplicated snapshots, event transitions and notification-safe history", async () => {
  await stopWorker();
  const result = await sqlWithoutRestart(
    "SELECT id, source_id, external_id, original_title, updated_at, source_complete FROM publications ORDER BY source_id, external_id;" +
    "SELECT publication_id, event_key, event_type, previous_status, new_status, detected_at, notify_eligible FROM publication_events ORDER BY id;" +
    "SELECT source_id, stream_key, checkpoint, last_success_at FROM source_states ORDER BY source_id, stream_key;",
  );
  const publications = result[0] as any[];
  const events = result[1] as any[];
  const checkpoints = result[2] as any[];
  assert.equal(publications.length, 6);
  const case42 = publications.find((row) => row.source_id === 1 && row.external_id === "42");
  assert.equal(case42?.original_title, "Forslag om udvidet Ukrainesærloven");
  assert.equal(case42?.source_complete, 1);
  const case101 = publications.find((row) => row.external_id === "101");
  const first101 = events.find((event) => event.event_type === "FIRST_SEEN" && event.publication_id === case101?.id);
  assert.ok(case101);
  assert.equal(case101?.updated_at, first101?.detected_at);
  const events42 = events.filter((event) => event.publication_id === case42?.id);
  for (const type of ["FIRST_SEEN", "TITLE_CHANGED", "STATUS_CHANGED", "DOCUMENTS_CHANGED", "STAGES_CHANGED", "VOTES_CHANGED", "RECLASSIFIED"]) assert.ok(events42.some((event) => event.event_type === type), type);
  const status = events42.find((event) => event.event_type === "STATUS_CHANGED");
  assert.equal(status?.previous_status, "Fremsat");
  assert.equal(status?.new_status, "Vedtaget");
  assert.equal(new Set(events.map((event) => event.event_key)).size, events.length);
  const historicalPublication = publications.find((row) => row.external_id === "99");
  assert.ok(events.find((event) => event.publication_id === historicalPublication?.id && event.event_type === "FIRST_SEEN" && event.notify_eligible === 0));
  assert.equal(publications.filter((row) => row.source_id === 2 && row.external_id === "123").length, 1);
  assert.equal(checkpoints.length, 8);
  const folketingetSag = checkpoints.find((row) => row.source_id === 1 && row.stream_key === "Sag");
  assert.deepEqual(JSON.parse(folketingetSag?.checkpoint), { timestamp: "2026-10-16T10:11:12.1234567", id: 11 });
});

