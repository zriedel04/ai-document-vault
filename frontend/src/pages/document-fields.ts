import type { DocumentDetail } from "../api/documents";

type Title = NonNullable<DocumentDetail["title"]>;
type Label = DocumentDetail["labels"][number];

/** Where the title the page shows came from, as the backend names its source. */
export const TITLE_SOURCE_TEXT: Record<Title["source"], string> = {
  file_properties: "from the file's properties",
  filename: "from the file name",
};

/** What goes between the levels of a folder path on the page. */
export const PATH_SEPARATOR = " › ";

/**
 * The folder paths to show for a document: one for each folder it was imported from. A document
 * from `School/CSE 485` carries a label for `School` and one for `School/CSE 485` (so a filter on
 * `School` finds it), and only the deepest is shown, as a path that already names the others.
 * Rejected and suggested labels are left out, and a label carried twice (by two sources) is shown
 * once. The backend's order (by kind and path) is kept.
 */
export function folderPaths(labels: readonly Label[]): string[][] {
  const paths: string[][] = [];
  const seen = new Set<string>();
  for (const label of labels) {
    if (label.kind !== "folder" || label.status !== "accepted" || label.path.length === 0) continue;
    // A name never holds a NUL (the import cleans control characters out), so this is one key
    // per path.
    const key = label.path.join("\u0000");
    if (seen.has(key)) continue;
    seen.add(key);
    paths.push(label.path);
  }
  return paths.filter((path) => !paths.some((other) => isAbove(path, other)));
}

/** Whether `upper` is a folder strictly above `lower`. */
function isAbove(upper: readonly string[], lower: readonly string[]): boolean {
  return upper.length < lower.length && upper.every((name, index) => name === lower[index]);
}

// The forms the backend stores (`parse_w3cdtf` in vault.ingest.metadata_stage): a year, a month,
// a day, or a time in UTC to the second. Matched whole: an anchored pattern with no flags, so a
// trailing newline does not match.
const YEAR = /^([0-9]{4})$/;
const MONTH = /^([0-9]{4})-([0-9]{2})$/;
const DAY = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
const TIME = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z$/;

/**
 * A UTC date from its parts, or null when they name no such day. Built with `setUTCFullYear`
 * because `Date.UTC` reads the years 0 to 99 as 1900 to 1999.
 */
function utcDate(
  year: number,
  month: number,
  day: number,
  hour = 0,
  minute = 0,
  second = 0,
): Date | null {
  if (year < 1 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return null;
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(hour, minute, second, 0);
  const same =
    date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
  return same ? date : null;
}

/**
 * A file's own date (its created or modified property) in the viewer's locale. A year, a month or
 * a day is shown as just that, in UTC so that the viewer's time zone never moves it to the day
 * before; a time is shown in the viewer's time zone. A value in no form the backend stores is
 * shown as written (it is text either way, never markup).
 */
export function formatPropertyDate(value: string, locale?: string): string {
  const parts = (pattern: RegExp) => pattern.exec(value)?.slice(1).map(Number);
  const time = parts(TIME);
  if (time !== undefined) {
    const [year = 0, month = 0, day = 0, hour = 0, minute = 0, second = 0] = time;
    const date = utcDate(year, month, day, hour, minute, second);
    return date === null
      ? value
      : date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
  }
  const shown = (date: Date | null, options: Intl.DateTimeFormatOptions) =>
    date === null ? value : date.toLocaleDateString(locale, { ...options, timeZone: "UTC" });
  const dayParts = parts(DAY);
  if (dayParts !== undefined) {
    const [year = 0, month = 0, day = 0] = dayParts;
    return shown(utcDate(year, month, day), { dateStyle: "medium" });
  }
  const monthParts = parts(MONTH);
  if (monthParts !== undefined) {
    const [year = 0, month = 0] = monthParts;
    return shown(utcDate(year, month, 1), { year: "numeric", month: "long" });
  }
  const yearParts = parts(YEAR);
  if (yearParts !== undefined) {
    const [year = 0] = yearParts;
    return shown(utcDate(year, 1, 1), { year: "numeric" });
  }
  return value;
}
