/** Identity and ordering rules for official Folketinget SQL backup snapshots. */
export interface SnapshotIdentity {
  backup_sha256: string;
  backup_finish_date: string;
  schema_fingerprint: string;
}

export type SnapshotIdentityErrorCode =
  | "snapshot_identity_invalid"
  | "snapshot_regression"
  | "snapshot_identity_conflict"
  | "snapshot_schema_changed";

export class SnapshotIdentityError extends Error {
  readonly code: SnapshotIdentityErrorCode;

  constructor(code: SnapshotIdentityErrorCode) {
    super(code);
    this.code = code;
    this.name = "SnapshotIdentityError";
  }
}

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const SQL_BACKUP_TIMESTAMP_PATTERN = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{3})$/;

function isLeapYear(year: number): boolean {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
}

function validSqlBackupTimestamp(value: string): boolean {
  const match = SQL_BACKUP_TIMESTAMP_PATTERN.exec(value);
  if (!match) return false;
  const [, yearText, monthText, dayText, hourText, minuteText, secondText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  if (year < 1 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return false;
  const daysPerMonth = [31, isLeapYear(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day >= 1 && day <= daysPerMonth[month - 1];
}

export function validateSnapshotIdentity(value: unknown): SnapshotIdentity {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new SnapshotIdentityError("snapshot_identity_invalid");
  }
  const candidate = value as Record<string, unknown>;
  if (
    Object.keys(candidate).length !== 3 ||
    typeof candidate.backup_sha256 !== "string" || !SHA256_PATTERN.test(candidate.backup_sha256) ||
    typeof candidate.backup_finish_date !== "string" || !validSqlBackupTimestamp(candidate.backup_finish_date) ||
    typeof candidate.schema_fingerprint !== "string" || !SHA256_PATTERN.test(candidate.schema_fingerprint)
  ) {
    throw new SnapshotIdentityError("snapshot_identity_invalid");
  }
  return {
    backup_sha256: candidate.backup_sha256,
    backup_finish_date: candidate.backup_finish_date,
    schema_fingerprint: candidate.schema_fingerprint,
  };
}

export function snapshotIdentityEquals(left: unknown, right: unknown): boolean {
  const a = validateSnapshotIdentity(left);
  const b = validateSnapshotIdentity(right);
  return a.backup_sha256 === b.backup_sha256 &&
    a.backup_finish_date === b.backup_finish_date &&
    a.schema_fingerprint === b.schema_fingerprint;
}

/** Compare the fixed-width SQL timestamp text without parsing it as a local time. */
export function compareSnapshotFinishDates(left: string, right: string): -1 | 0 | 1 {
  if (!validSqlBackupTimestamp(left) || !validSqlBackupTimestamp(right)) {
    throw new SnapshotIdentityError("snapshot_identity_invalid");
  }
  return left < right ? -1 : left > right ? 1 : 0;
}

/** Validate an observed target against the persisted base; returns same/progression. */
export function validateSnapshotProgression(baseValue: unknown, targetValue: unknown): "same" | "progression" {
  const base = validateSnapshotIdentity(baseValue);
  const target = validateSnapshotIdentity(targetValue);
  const order = compareSnapshotFinishDates(base.backup_finish_date, target.backup_finish_date);
  if (order === 0) {
    if (snapshotIdentityEquals(base, target)) return "same";
    throw new SnapshotIdentityError("snapshot_identity_conflict");
  }
  if (order > 0) throw new SnapshotIdentityError("snapshot_regression");
  if (base.schema_fingerprint !== target.schema_fingerprint) {
    throw new SnapshotIdentityError("snapshot_schema_changed");
  }
  return "progression";
}
