import assert from "node:assert/strict";
import { test } from "node:test";
import {
  compareSnapshotFinishDates,
  SnapshotIdentityError,
  snapshotIdentityEquals,
  validateSnapshotIdentity,
  validateSnapshotProgression,
  type SnapshotIdentity,
  type SnapshotIdentityErrorCode,
} from "../src/snapshot_state.ts";

const fingerprintA = "a".repeat(64);
const fingerprintB = "b".repeat(64);
const shaA = "1".repeat(64);
const shaB = "2".repeat(64);
const first: SnapshotIdentity = {
  backup_sha256: shaA,
  backup_finish_date: "2026-10-19 02:01:05.000",
  schema_fingerprint: fingerprintA,
};

function assertSnapshotError(code: SnapshotIdentityErrorCode, action: () => unknown): void {
  assert.throws(action, (error) => error instanceof SnapshotIdentityError && error.code === code);
}

test("snapshot identity validates strict lowercase hashes and SQL backup timestamp", () => {
  assert.deepEqual(validateSnapshotIdentity(first), first);
  for (const invalid of [
    { ...first, backup_sha256: "A".repeat(64) },
    { ...first, backup_sha256: "a".repeat(63) },
    { ...first, schema_fingerprint: "g".repeat(64) },
    { ...first, backup_finish_date: "2026-10-19T02:01:05.000Z" },
    { ...first, backup_finish_date: "2026-02-29 02:01:05.000" },
    { ...first, backup_finish_date: "2026-10-19 24:01:05.000" },
    { ...first, extra: "unexpected" },
    null,
  ]) assertSnapshotError("snapshot_identity_invalid", () => validateSnapshotIdentity(invalid));
});

test("snapshot equality and finish-date ordering do not interpret local time", () => {
  assert.equal(snapshotIdentityEquals(first, { ...first }), true);
  assert.equal(snapshotIdentityEquals(first, { ...first, backup_sha256: shaB }), false);
  assert.equal(compareSnapshotFinishDates("2026-10-19 02:01:05.000", "2026-10-19 02:01:06.000"), -1);
  assert.equal(compareSnapshotFinishDates("2026-10-19 02:01:05.000", "2026-10-19 02:01:05.000"), 0);
  assert.equal(compareSnapshotFinishDates("2026-10-19 02:01:06.000", "2026-10-19 02:01:05.000"), 1);
});

test("snapshot progression accepts only a later backup with the same schema", () => {
  assert.equal(validateSnapshotProgression(first, first), "same");
  assert.equal(validateSnapshotProgression(first, {
    backup_sha256: shaB,
    backup_finish_date: "2026-10-20 02:01:05.000",
    schema_fingerprint: fingerprintA,
  }), "progression");
  assertSnapshotError("snapshot_regression", () => validateSnapshotProgression(first, {
    backup_sha256: shaB,
    backup_finish_date: "2026-10-18 02:01:05.000",
    schema_fingerprint: fingerprintA,
  }));
  assertSnapshotError("snapshot_identity_conflict", () => validateSnapshotProgression(first, {
    backup_sha256: shaB,
    backup_finish_date: "2026-10-19 02:01:05.000",
    schema_fingerprint: fingerprintA,
  }));
  assertSnapshotError("snapshot_schema_changed", () => validateSnapshotProgression(first, {
    backup_sha256: shaB,
    backup_finish_date: "2026-10-20 02:01:05.000",
    schema_fingerprint: fingerprintB,
  }));
});
