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

test("source status renders only localized health labels and separates planned sources", () => {
  for (const language of ["uk", "ru"] as const) {
    const active = sourceStatusScreen(language, "2026-10-16T11:30:00.000Z", "2026-10-16T11:30:00.000Z", NOW);
    const stale = sourceStatusScreen(language, "2026-10-14T10:30:00.000Z", "2026-10-14T10:30:00.000Z", NOW);
    const unknown = sourceStatusScreen(language, null, null, NOW);
    assert.match(active, /Høringsportalen/);
    assert.match(active, language === "ru" ? /Høringsportalen: данные актуальны/u : /Høringsportalen: дані актуальні/u);
    assert.match(active, language === "ru" ? /Folketinget: данные актуальны/u : /Folketinget: дані актуальні/u);
    assert.match(stale, language === "ru" ? /Høringsportalen: данные устарели/u : /Høringsportalen: дані застаріли/u);
    assert.match(stale, language === "ru" ? /Folketinget: данные устарели/u : /Folketinget: дані застаріли/u);
    assert.match(unknown, language === "ru" ? /Høringsportalen: статус неизвестен/u : /Høringsportalen: статус невідомий/u);
    assert.match(unknown, language === "ru" ? /Folketinget: статус неизвестен/u : /Folketinget: статус невідомий/u);
    assert.match(unknown, /Nyidanmark, Retsinformation/u);
    assert.doesNotMatch(active + stale + unknown, /2026|16|12:00|snapshot_states|D1|GitHub Actions/u);
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

test("Folketinget status reuses committed snapshot updated_at and returns unknown when absent or invalid", async () => {
  let row: { updated_at: string | null } | null = { updated_at: "2026-10-16T10:00:00.000Z" };
  let sql = "";
  const db = {
    prepare(value: string) {
      sql = value;
      return { async first() { return row; } };
    },
  } as never;
  const repository = new D1SourceStatusRepository(db);
  assert.equal(await repository.lastSuccessfulFolketingetRun(), "2026-10-16T10:00:00.000Z");
  assert.match(sql, /source_snapshot_states/);
  assert.match(sql, /ss\.updated_at/);
  assert.match(sql, /s\.code = 'folketinget'/);
  row = null;
  assert.equal(await repository.lastSuccessfulFolketingetRun(), null);
  row = { updated_at: "not-a-timestamp" };
  assert.equal(await repository.lastSuccessfulFolketingetRun(), null);
});
