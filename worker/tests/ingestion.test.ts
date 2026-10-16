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
  for (const [source, runId, emptyCheckpoints] of [
    ["folketinget", "00000000-0000-4000-8000-000000000030", Object.fromEntries(["Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"].map((key) => [key, null]))],
    ["hoeringsportalen", "00000000-0000-4000-8000-000000000031", { immigration_ministry: null, immigration_category: null }],
  ] as const) {
    const firstIncremental = await request("/internal/ingest", "POST", {
      action: "start", run_id: runId, source, mode: "incremental", expected_batches: 1,
      collection_complete: true, proposed_checkpoints: emptyCheckpoints,
    });
    assert.equal(firstIncremental.status, 409);
    assert.equal(firstIncremental.body.error, "historical_baseline_required");
  }
  for (const [source, runId, emptyCheckpoints] of [
    ["folketinget", "00000000-0000-4000-8000-000000000032", Object.fromEntries(["Sag", "Sagstrin", "SagDokument", "Dokument", "Afstemning", "Sagsstatus"].map((key) => [key, null]))],
    ["hoeringsportalen", "00000000-0000-4000-8000-000000000033", { immigration_ministry: null, immigration_category: null }],
  ] as const) {
    const baselineRun = await start(runId, { source, mode: "historical", proposed: emptyCheckpoints });
    assert.equal(baselineRun.status, 201);
    await batch(runId, []);
    assert.equal((await commit(runId)).body.status, "COMMITTED");
    const incrementalRunId = source === "folketinget" ? "00000000-0000-4000-8000-000000000034" : "00000000-0000-4000-8000-000000000035";
    const incrementalRun = await request("/internal/ingest", "POST", {
      action: "start", run_id: incrementalRunId, source, mode: "incremental", expected_batches: 1,
      collection_complete: true, proposed_checkpoints: emptyCheckpoints,
    });
    assert.equal(incrementalRun.status, 201);
    assert.equal((await request("/internal/ingest", "POST", { action: "abort", run_id: incrementalRunId })).body.status, "ABORTED");
  }
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

test("relationship source timestamps alone are stored but do not create document, stage, or vote events", async () => {
  const initial = publication({ external_id: "902", normalized_snapshot: {
    title: "Forslag om Ukrainesærloven", content_text: null, status: "Fremsat", publication_type: "L 1", relevance: "DIRECT",
    documents: [{ id: 1, link_id: 2, title: "Lovudkast", date_raw: "2026-10-01", source_updated_at_raw: "2026-10-10T00:00:00", updated_at_utc: null }],
    stages: [{ id: 3, title: "Fremsat", date_raw: "2026-10-01", type_id: 1, status_id: 2, source_updated_at_raw: "2026-10-10T00:00:00", updated_at_utc: null }],
    votes: [{ id: 4, stage_id: 3, number: 1, approved: true, conclusion: "Vedtaget", source_updated_at_raw: "2026-10-10T00:00:00", updated_at_utc: null }],
  } });
  const firstRun = "00000000-0000-4000-8000-000000000020";
  await start(firstRun, { proposed: cursors(20) });
  await batch(firstRun, [initial]);
  await commit(firstRun);

  const changed = publication({ external_id: "902", normalized_snapshot: {
    ...initial.normalized_snapshot,
    documents: [{ id: 1, link_id: 2, title: "Lovudkast", date_raw: "2026-10-01", source_updated_at_raw: "2026-10-11T00:00:00", updated_at_utc: "2026-10-11T00:00:00Z" }],
    stages: [{ id: 3, title: "Fremsat", date_raw: "2026-10-01", type_id: 1, status_id: 2, source_updated_at_raw: "2026-10-11T00:00:00", updated_at_utc: "2026-10-11T00:00:00Z" }],
    votes: [{ id: 4, stage_id: 3, number: 1, approved: true, conclusion: "Vedtaget", source_updated_at_raw: "2026-10-11T00:00:00", updated_at_utc: "2026-10-11T00:00:00Z" }],
  }, source_updated_at_raw: "2026-10-11T00:00:00", source_metadata: { source_updated_at_raw: "2026-10-11T00:00:00" } });
  const secondRun = "00000000-0000-4000-8000-000000000021";
  await start(secondRun, { proposed: cursors(21) });
  await batch(secondRun, [changed]);
  assert.equal((await commit(secondRun)).body.status, "COMMITTED");
  const rows = await query("SELECT e.event_type FROM publication_events e JOIN publications p ON p.id=e.publication_id WHERE p.external_id='902' ORDER BY e.id");
  assert.deepEqual(rows.map((row) => row.event_type), ["FIRST_SEEN"]);
  const stored = await query("SELECT normalized_snapshot_json FROM publications WHERE external_id='902'");
  assert.match(stored[0].normalized_snapshot_json, /2026-10-11T00:00:00/);
});

test("substantive document, stage, and vote changes still produce events", async () => {
  const initial = publication({ external_id: "903" });
  const firstRun = "00000000-0000-4000-8000-000000000022";
  await start(firstRun, { proposed: cursors(22) });
  await batch(firstRun, [initial]);
  await commit(firstRun);
  const updated = publication({ external_id: "903", normalized_snapshot: {
    ...initial.normalized_snapshot,
    documents: [{ id: 501, link_id: 701, title: "Ændret lovudkast" }],
    stages: [{ id: 601, title: "Fremsat", date_raw: "2026-10-01", type_id: 1, status_id: 3 }],
    votes: [{ id: 801, stage_id: 601, number: 1, approved: false, conclusion: "Forkastet", comment: null }],
  } });
  const secondRun = "00000000-0000-4000-8000-000000000023";
  await start(secondRun, { proposed: cursors(23) });
  await batch(secondRun, [updated]);
  assert.equal((await commit(secondRun)).body.status, "COMMITTED");
  const rows = await query("SELECT event_type FROM publication_events e JOIN publications p ON p.id=e.publication_id WHERE p.external_id='903' ORDER BY e.id");
  assert.deepEqual(rows.map((row) => row.event_type), ["FIRST_SEEN", "DOCUMENTS_CHANGED", "STAGES_CHANGED", "VOTES_CHANGED"]);
});

test("concurrent commits process a staged record once and advance one checkpoint set", async () => {
  const run = "00000000-0000-4000-8000-000000000024";
  await start(run, { proposed: cursors(24) });
  await batch(run, [publication({ external_id: "904" })]);
  const results = await Promise.all([commit(run), commit(run)]);
  assert.ok(results.every((result) => result.status === 200));
  const rows = await query("SELECT processed_count, accepted_count, status FROM ingestion_runs WHERE run_id='00000000-0000-4000-8000-000000000024'");
  assert.deepEqual(rows[0], { processed_count: 1, accepted_count: 1, status: "COMMITTED" });
  const events = await query("SELECT COUNT(*) AS count FROM publication_events e JOIN publications p ON p.id=e.publication_id WHERE p.external_id='904'");
  assert.equal(events[0].count, 1);
});

test("checkpoint CAS failure cannot partially finalize and retry after interruption is safe", async () => {
  const initial = "00000000-0000-4000-8000-000000000025";
  await start(initial, { proposed: cursors(25) });
  await batch(initial, [publication({ external_id: "905" })]);
  assert.equal((await commit(initial)).body.status, "COMMITTED");
  await query("CREATE TRIGGER ignore_sag_checkpoint BEFORE UPDATE OF checkpoint ON source_states WHEN OLD.stream_key='Sag' BEGIN SELECT RAISE(IGNORE); END");
  const interrupted = "00000000-0000-4000-8000-000000000026";
  await start(interrupted, { proposed: cursors(26) });
  await batch(interrupted, [publication({ external_id: "906" }), publication({ external_id: "907" })]);
  const failed = await commit(interrupted);
  assert.equal(failed.status, 500);
  const stateAfterFailure = (await request("/internal/source-states?source=folketinget", "GET")).body.streams;
  assert.ok(stateAfterFailure.every((item: any) => item.checkpoint?.id === 25));
  const statusAfterFailure = await query("SELECT status, processed_count, accepted_count, checkpoint_writes FROM ingestion_runs WHERE run_id='00000000-0000-4000-8000-000000000026'");
  assert.deepEqual(statusAfterFailure[0], { status: "COMMITTING", processed_count: 2, accepted_count: 2, checkpoint_writes: 0 });
  await query("DROP TRIGGER ignore_sag_checkpoint");
  assert.equal((await commit(interrupted)).body.status, "COMMITTED");
  const recovered = (await request("/internal/source-states?source=folketinget", "GET")).body.streams;
  assert.ok(recovered.every((item: any) => item.checkpoint?.id === 26));
});

test("historical events cannot notify and incomplete snapshots cannot overwrite complete data or advance checkpoints", async () => {
  const histRun = "00000000-0000-4000-8000-000000000004";
  await start(histRun, { mode: "historical", proposed: cursors(40) });
  const historical = publication({ external_id: "99", historical: true });
  await batch(histRun, [historical]);
  await commit(histRun);
  const beforeState = (await request("/internal/source-states?source=folketinget", "GET")).body.streams.find((item: any) => item.stream === "Sag")?.checkpoint;
  const blockedRun = "00000000-0000-4000-8000-000000000005";
  await start(blockedRun, { proposed: cursors(41) });
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
    mode: "incremental", expected_batches: 1, collection_complete: false, proposed_checkpoints: cursors(42),
  });
  assert.equal(partial.status, 409);
  const run = "00000000-0000-4000-8000-000000000007";
  assert.equal((await start(run, { expected: 2, proposed: cursors(43) })).status, 201);
  const overlapping = "00000000-0000-4000-8000-000000000008";
  assert.equal((await start(overlapping, { proposed: cursors(44) })).status, 409);
  assert.equal((await commit(run)).body.error, "batches_incomplete");
  await batch(run, [publication({ external_id: "100" })], 0);
  await batch(run, [], 1);
  assert.equal((await batch(run, [publication({ external_id: "100" })], 0)).body.idempotent, true);
  assert.equal((await commit(run)).body.status, "COMMITTED");
  assert.equal((await start(overlapping, { proposed: cursors(44) })).status, 201);
  const openState = (await request("/internal/source-states?source=folketinget", "GET")).body.streams.find((item: any) => item.stream === "Sag")?.checkpoint;
  assert.deepEqual(openState, { timestamp: "2026-10-16T10:11:12.1234567", id: 43 });
  assert.equal((await request("/internal/ingest", "POST", { action: "abort", run_id: overlapping })).body.status, "ABORTED");
});

test("a failed D1 event write rolls back publication and checkpoint writes; retry resumes safely", async () => {
  await query("CREATE TRIGGER fail_ingest_event BEFORE INSERT ON publication_events WHEN instr(NEW.event_key, '00000000-0000-4000-8000-000000000011:1:778:FIRST_SEEN:') > 0 BEGIN SELECT RAISE(ABORT, 'test failure'); END");
  const run = "00000000-0000-4000-8000-000000000011";
  await start(run, { proposed: cursors(45) });
  await batch(run, [publication({ external_id: "777" }), publication({ external_id: "778" })]);
  const failed = await commit(run);
  assert.equal(failed.status, 500);
  const interruptedState = await query("SELECT status, processed_count, accepted_count FROM ingestion_runs WHERE run_id='00000000-0000-4000-8000-000000000011'");
  assert.deepEqual(interruptedState[0], { status: "COMMITTING", processed_count: 1, accepted_count: 2 });
  const firstRecord = await query("SELECT COUNT(*) AS count FROM publications WHERE external_id='777'");
  const secondRecord = await query("SELECT COUNT(*) AS count FROM publications WHERE external_id='778'");
  assert.equal(firstRecord[0].count, 1);
  assert.equal(secondRecord[0].count, 0);
  const current = (await request("/internal/source-states?source=folketinget", "GET")).body.streams.find((item: any) => item.stream === "Sag")?.checkpoint;
  assert.deepEqual(current, { timestamp: "2026-10-16T10:11:12.1234567", id: 43 });
  await query("DROP TRIGGER fail_ingest_event");
  assert.equal((await commit(run)).body.status, "COMMITTED");
  const processed = await query("SELECT processed_count, status FROM ingestion_runs WHERE run_id='00000000-0000-4000-8000-000000000011'");
  assert.deepEqual(processed[0], { processed_count: 2, status: "COMMITTED" });
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

test("Høringsportalen first-seen records stay notification-ineligible while later meaningful updates remain distinguishable", async () => {
  const proposed = { immigration_ministry: null, immigration_category: null };
  const baselineRun = "00000000-0000-4000-8000-000000000036";
  assert.equal((await start(baselineRun, { source: "hoeringsportalen", mode: "historical", proposed })).status, 201);
  assert.equal((await batch(baselineRun, [])).status, 201);
  assert.equal((await commit(baselineRun)).body.status, "COMMITTED");
  const run1 = "00000000-0000-4000-8000-000000000037";
  const record = {
    source: "hoeringsportalen", external_id: "910", title: "Ældre høring fundet i feed-vinduet",
    url: "https://hoeringsportalen.dk/Hearing/Details/910", relevance: "DIRECT", relevance_reason: "rule match",
    ruleset_version: "1.1", status: null, publication_type: null, published_at: null,
    source_updated_at: "2020-01-02T00:00:00.000000Z", source_updated_at_raw: "2020-01-02T00:00:00Z",
    historical: false, notification_blocked: false, complete: true, diagnostics: [],
    source_metadata: { feed_codes: ["immigration_ministry"] },
    normalized_snapshot: { title: "Ældre høring fundet i feed-vinduet", content_text: null, status: null, publication_type: null, relevance: "DIRECT", documents: [], stages: [], votes: [] },
  };
  assert.equal((await start(run1, { source: "hoeringsportalen", proposed })).status, 201);
  assert.equal((await batch(run1, [record])).status, 201);
  assert.equal((await commit(run1)).body.status, "COMMITTED");
  const first = await query("SELECT event_type, notify_eligible FROM publication_events e JOIN publications p ON p.id=e.publication_id WHERE p.source_id=2 AND p.external_id='910'");
  assert.deepEqual(first, [{ event_type: "FIRST_SEEN", notify_eligible: 0 }]);

  const run2 = "00000000-0000-4000-8000-000000000038";
  const updated = { ...record, title: "Ældre høring med opdateret titel", normalized_snapshot: { ...record.normalized_snapshot, title: "Ældre høring med opdateret titel" } };
  assert.equal((await start(run2, { source: "hoeringsportalen", proposed })).status, 201);
  assert.equal((await batch(run2, [updated])).status, 201);
  assert.equal((await commit(run2)).body.status, "COMMITTED");
  const events = await query("SELECT event_type, notify_eligible FROM publication_events e JOIN publications p ON p.id=e.publication_id WHERE p.source_id=2 AND p.external_id='910' ORDER BY e.id");
  assert.deepEqual(events, [{ event_type: "FIRST_SEEN", notify_eligible: 0 }, { event_type: "TITLE_CHANGED", notify_eligible: 1 }]);
});

test("staging cleanup defaults to dry-run and preserves active, blocked, publication, event, user, and checkpoint data", async () => {
  const old = "2000-01-01T00:00:00.000Z";
  await query("UPDATE ingestion_runs SET completed_at='" + old + "' WHERE run_id IN ('00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000005')");

  const hpState = (await request("/internal/source-states?source=hoeringsportalen", "GET")).body;
  const savedHp = Object.fromEntries(hpState.streams.map((item: any) => [item.stream, item.checkpoint]));
  const aborted = "00000000-0000-4000-8000-000000000027";
  await start(aborted, { source: "hoeringsportalen", proposed: savedHp });
  const hpPublication = {
    source: "hoeringsportalen", external_id: "124", title: "Høringspostering", url: "https://hoeringsportalen.dk/Hearing/Details/124",
    relevance: "IRRELEVANT", relevance_reason: "no match", ruleset_version: "1.1", status: null, publication_type: null,
    published_at: null, source_updated_at: "2026-10-16T10:11:12.123456Z", source_updated_at_raw: "2026-10-16T10:11:12.123456Z",
    historical: false, notification_blocked: false, complete: true, diagnostics: [], source_metadata: { feed_codes: ["immigration_ministry"] },
    normalized_snapshot: { title: "Høringspostering", content_text: null, status: null, publication_type: null, relevance: "IRRELEVANT", documents: [], stages: [], votes: [] },
  };
  await batch(aborted, [hpPublication]);
  assert.equal((await request("/internal/ingest", "POST", { action: "abort", run_id: aborted })).body.status, "ABORTED");
  await query("UPDATE ingestion_runs SET completed_at='" + old + "' WHERE run_id='00000000-0000-4000-8000-000000000027'");

  const open = "00000000-0000-4000-8000-000000000028";
  await start(open, { proposed: cursors(45) });
  await batch(open, [publication({ external_id: "909" })]);
  const committing = "00000000-0000-4000-8000-000000000029";
  await start(committing, { source: "hoeringsportalen", proposed: savedHp });
  await batch(committing, [hpPublication]);
  await query("UPDATE ingestion_runs SET status='COMMITTING' WHERE run_id='00000000-0000-4000-8000-000000000029'");

  const folketingStateBefore = (await request("/internal/source-states?source=folketinget", "GET")).body.streams;
  const hpStateBefore = (await request("/internal/source-states?source=hoeringsportalen", "GET")).body.streams;
  const protectedDataBefore = await query("SELECT (SELECT COUNT(*) FROM publications) AS publications, (SELECT COUNT(*) FROM publication_events) AS events, (SELECT COUNT(*) FROM users) AS users, (SELECT COUNT(*) FROM notifications) AS notifications");
  const dryRun = await request("/internal/ingest", "POST", { action: "cleanup", retention_days: 30 });
  assert.equal(dryRun.body.dry_run, true);
  assert.equal(dryRun.body.eligible_runs, 2);
  assert.equal(dryRun.body.deleted_runs, 0);
  assert.equal((await query("SELECT COUNT(*) AS count FROM ingestion_staging WHERE run_id='00000000-0000-4000-8000-000000000001'")).at(0)?.count, 1);
  const unconfirmed = await request("/internal/ingest", "POST", { action: "cleanup", retention_days: 30, dry_run: false });
  assert.equal(unconfirmed.status, 400);
  const deleted = await request("/internal/ingest", "POST", { action: "cleanup", retention_days: 30, dry_run: false, confirm: "DELETE_STAGING" });
  assert.equal(deleted.body.deleted_runs, 2);
  assert.equal(deleted.body.remaining_eligible_runs, 0);
  const retained = await query("SELECT run_id, status FROM ingestion_runs WHERE run_id IN ('00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000005','00000000-0000-4000-8000-000000000027','00000000-0000-4000-8000-000000000028','00000000-0000-4000-8000-000000000029') ORDER BY run_id");
  assert.equal(retained.length, 5);
  assert.equal((await query("SELECT COUNT(*) AS count FROM ingestion_staging WHERE run_id IN ('00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000027')")).at(0)?.count, 0);
  assert.equal((await query("SELECT COUNT(*) AS count FROM ingestion_batches WHERE run_id IN ('00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000027')")).at(0)?.count, 0);
  assert.ok((await query("SELECT COUNT(*) AS count FROM ingestion_staging WHERE run_id='00000000-0000-4000-8000-000000000005'")).at(0)?.count > 0);
  assert.ok((await query("SELECT COUNT(*) AS count FROM ingestion_staging WHERE run_id='00000000-0000-4000-8000-000000000028'")).at(0)?.count > 0);
  assert.ok((await query("SELECT COUNT(*) AS count FROM ingestion_staging WHERE run_id='00000000-0000-4000-8000-000000000029'")).at(0)?.count > 0);
  assert.deepEqual((await request("/internal/source-states?source=folketinget", "GET")).body.streams, folketingStateBefore);
  assert.deepEqual((await request("/internal/source-states?source=hoeringsportalen", "GET")).body.streams, hpStateBefore);
  assert.deepEqual((await query("SELECT (SELECT COUNT(*) FROM publications) AS publications, (SELECT COUNT(*) FROM publication_events) AS events, (SELECT COUNT(*) FROM users) AS users, (SELECT COUNT(*) FROM notifications) AS notifications"))[0], protectedDataBefore[0]);
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
  assert.equal(publications.length, 14);
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
  assert.deepEqual(JSON.parse(folketingetSag?.checkpoint), { timestamp: "2026-10-16T10:11:12.1234567", id: 45 });
});

