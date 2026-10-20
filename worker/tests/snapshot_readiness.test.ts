import assert from "node:assert/strict";
import { test } from "node:test";
import app from "../src/index.ts";

const TOKEN = "snapshot-readiness-test-token";
const TABLE_COLUMNS: Record<string, string[]> = {
  sources: ["id", "code"],
  ingestion_runs: [
    "run_id", "source_id", "status", "mode", "state_kind", "proposed_checkpoints_json",
    "base_checkpoints_json", "checkpoint_writes", "base_snapshot_json", "target_snapshot_json",
    "snapshot_state_writes",
  ],
  source_states: ["source_id"],
  source_snapshot_states: ["source_id", "backup_sha256", "backup_finish_date", "schema_fingerprint", "generation", "last_run_id", "initialized_at", "updated_at"],
};
const TRIGGERS = [
  "source_states_reject_snapshot_run_insert", "source_states_reject_snapshot_run_update",
  "source_states_reject_snapshot_run_delete", "source_snapshot_states_guard_insert",
  "source_snapshot_states_count_insert", "source_snapshot_states_guard_update",
  "source_snapshot_states_count_update", "source_snapshot_states_reject_delete",
  "ingestion_runs_guard_state_kind_commit",
];

type Scenario = {
  missingTable?: string;
  missingColumn?: [string, string];
  missingTrigger?: string;
  initialized?: boolean;
  activeStatus?: "OPEN" | "COMMITTING" | null;
  queryFailure?: boolean;
};

function database(scenario: Scenario = {}) {
  const calls: string[] = [];
  const db = {
    prepare(sql: string) {
      const bound: unknown[] = [];
      const statement = {
        bind(...args: unknown[]) { bound.push(...args); return statement; },
        async all() {
          calls.push(sql);
          if (scenario.queryFailure) throw new Error("private SQL failure detail");
          if (sql.includes("sqlite_master") && sql.includes("type = 'table'")) {
            return { results: Object.keys(TABLE_COLUMNS).filter((name) => name !== scenario.missingTable).map((name) => ({ name })) };
          }
          if (sql.includes("pragma_table_info")) {
            const table = String(bound[0]);
            const columns = TABLE_COLUMNS[table] ?? [];
            return { results: columns.filter((name) => !(scenario.missingColumn?.[0] === table && scenario.missingColumn[1] === name)).map((name) => ({ name })) };
          }
          if (sql.includes("type = 'trigger'")) {
            return { results: TRIGGERS.filter((name) => name !== scenario.missingTrigger).map((name) => ({ name })) };
          }
          throw new Error("unexpected read query");
        },
        async first() {
          calls.push(sql);
          if (scenario.queryFailure) throw new Error("private SQL failure detail");
          if (sql.includes("FROM sources")) return { id: 3 };
          if (sql.includes("FROM source_snapshot_states")) return scenario.initialized ? { generation: 2 } : null;
          if (sql.includes("FROM ingestion_runs")) return { count: scenario.activeStatus ? 1 : 0 };
          throw new Error("unexpected read query");
        },
        async run() { throw new Error("readiness endpoint attempted a write"); },
      };
      return statement;
    },
  };
  return { db: db as unknown as D1Database, calls };
}

async function request(scenario: Scenario = {}, options: { auth?: boolean; source?: string } = {}) {
  const fixture = database(scenario);
  const response = await app.fetch(new Request(
    `https://worker.test/internal/snapshot-readiness?source=${options.source ?? "folketinget"}`,
    { headers: options.auth === false ? {} : { Authorization: `Bearer ${TOKEN}` } },
  ), { DB: fixture.db, INGEST_API_TOKEN: TOKEN } as never, {} as ExecutionContext);
  return { response, body: await response.json() as Record<string, unknown>, calls: fixture.calls };
}

test("snapshot readiness is read-only and allows an uninitialized idle Folketinget source", async () => {
  const { response, body, calls } = await request();
  assert.equal(response.status, 200);
  assert.deepEqual(body, {
    source: "folketinget", schema_ready: true, snapshot_state_initialized: false,
    snapshot_generation: null, active_ingestion_run: false,
    worker_supports_snapshot_ingestion: true, historical_snapshot_start_allowed: true,
  });
  assert.equal(calls.length, 9);
});

test("missing migration table, required column, or trigger fails closed before state queries", async () => {
  for (const scenario of [
    { missingTable: "source_snapshot_states" },
    { missingColumn: ["ingestion_runs", "target_snapshot_json"] as [string, string] },
    { missingColumn: ["ingestion_runs", "proposed_checkpoints_json"] as [string, string] },
    { missingTrigger: "source_snapshot_states_guard_insert" },
  ]) {
    const { response, body, calls } = await request(scenario);
    assert.equal(response.status, 200);
    assert.equal(body.schema_ready, false);
    assert.equal(body.historical_snapshot_start_allowed, false);
    assert.equal(body.snapshot_state_initialized, null);
    assert.equal(body.active_ingestion_run, null);
    assert.equal(calls.some((sql) => sql.includes("FROM source_snapshot_states")), false);
  }
});

test("initialized snapshot state blocks the first historical snapshot start", async () => {
  const { body } = await request({ initialized: true });
  assert.equal(body.schema_ready, true);
  assert.equal(body.snapshot_state_initialized, true);
  assert.equal(body.snapshot_generation, 2);
  assert.equal(body.historical_snapshot_start_allowed, false);
});

test("OPEN and COMMITTING source runs each block historical snapshot start", async () => {
  for (const activeStatus of ["OPEN", "COMMITTING"] as const) {
    const { body } = await request({ activeStatus });
    assert.equal(body.schema_ready, true);
    assert.equal(body.active_ingestion_run, true);
    assert.equal(body.historical_snapshot_start_allowed, false);
  }
});

test("unauthorized and unsupported-source requests do not inspect D1", async () => {
  const unauthorized = await request({}, { auth: false });
  assert.equal(unauthorized.response.status, 401);
  assert.deepEqual(unauthorized.body, { error: "unauthorized" });
  assert.equal(unauthorized.calls.length, 0);

  const unsupported = await request({}, { source: "unknown" });
  assert.equal(unsupported.response.status, 400);
  assert.deepEqual(unsupported.body, { error: "invalid_source" });
  assert.equal(unsupported.calls.length, 0);
});

test("database failures return a closed aggregate response without raw details", async () => {
  const { body } = await request({ queryFailure: true });
  assert.deepEqual(body, {
    source: "folketinget", schema_ready: false, snapshot_state_initialized: null,
    snapshot_generation: null, active_ingestion_run: null,
    worker_supports_snapshot_ingestion: true, historical_snapshot_start_allowed: false,
  });
  assert.equal(JSON.stringify(body).includes("private SQL failure detail"), false);
});
