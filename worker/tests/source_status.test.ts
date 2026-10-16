import assert from "node:assert/strict";
import { test } from "node:test";
import { sourceFreshness, sourceStatusScreen } from "../src/bot/source_status.ts";
import { D1SourceStatusRepository } from "../src/database/source_status.ts";

const NOW = Date.parse("2026-10-16T12:00:00.000Z");

test("source freshness classifies recent, stale, missing, invalid, and future metadata", () => {
  assert.equal(sourceFreshness("2026-10-16T11:59:00.000Z", NOW), "active");
  assert.equal(sourceFreshness("2026-10-15T11:59:00.000Z", NOW), "stale");
  assert.equal(sourceFreshness(null, NOW), "unknown");
  assert.equal(sourceFreshness("not-a-timestamp", NOW), "unknown");
  assert.equal(sourceFreshness("2026-10-16", NOW), "unknown");
  assert.equal(sourceFreshness("2026-10-16T13:00:00.000Z", NOW), "unknown");
});

test("source status renders localized dates and never invents an unknown time", () => {
  for (const language of ["uk", "ru"] as const) {
    const active = sourceStatusScreen(language, "2026-10-16T10:30:00.000Z", NOW);
    const stale = sourceStatusScreen(language, "2026-10-14T10:30:00.000Z", NOW);
    const unknown = sourceStatusScreen(language, null, NOW);
    assert.match(active, /Høringsportalen/);
    assert.match(active, /16/);
    assert.match(stale, /14/);
    assert.match(unknown, /неизвест|невідом/u);
    assert.doesNotMatch(unknown, /2026|16|12:00/);
    assert.match(unknown, /Folketinget/);
    assert.match(unknown, /расписан|розкладу/u);
    assert.match(unknown, /мониторинг пока не активен|моніторинг ще не активний/u);
  }
});

test("D1 status requires both Høringsportalen feed rows and uses the conservative timestamp", async () => {
  let rows: Array<{ stream_key: string; last_success_at: string | null }> = [];
  let bound: unknown[] = [];
  const db = {
    prepare(sql: string) {
      assert.match(sql, /source_states/);
      return {
        bind(...values: unknown[]) { bound = values; return this; },
        async all() { return { results: rows }; },
      };
    },
  } as never;
  const repository = new D1SourceStatusRepository(db);
  rows = [
    { stream_key: "immigration_ministry", last_success_at: "2026-10-16T10:00:00.000Z" },
    { stream_key: "immigration_category", last_success_at: "2026-10-16T10:05:00.000Z" },
  ];
  assert.equal(await repository.lastSuccessfulHoeringsportalenRun(), "2026-10-16T10:00:00.000Z");
  assert.deepEqual(bound, ["immigration_ministry", "immigration_category"]);
  rows = [{ stream_key: "immigration_ministry", last_success_at: "2026-10-16T10:00:00.000Z" }];
  assert.equal(await repository.lastSuccessfulHoeringsportalenRun(), null);
});
