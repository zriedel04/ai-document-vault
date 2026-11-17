import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import type { DocumentDetail } from "../api/documents";
import { TITLE_SOURCE_TEXT, folderPaths, formatPropertyDate } from "./document-fields";

type Label = DocumentDetail["labels"][number];

let next = 0;
function folder(path: string[], overrides: Partial<Label> = {}): Label {
  next += 1;
  return {
    id: `01J9Z8Y7X6W5V4T3S2R1Q0P${String(next).padStart(3, "0")}`,
    kind: "folder",
    name: path.at(-1) ?? "",
    path,
    source: "import_path",
    status: "accepted",
    ...overrides,
  };
}

describe("folder paths", () => {
  it("shows only the deepest folder of each import, in the backend's order", () => {
    const labels = [
      folder(["School"]),
      folder(["School", "CSE 485"]),
      folder(["School", "CSE 485", "Assignments"]),
      folder(["Work"]),
      folder(["Work", "Assignments"]),
    ];
    expect(folderPaths(labels)).toEqual([
      ["School", "CSE 485", "Assignments"],
      ["Work", "Assignments"],
    ]);
  });

  it("keeps a folder that is only a prefix of another name, not a folder above it", () => {
    // "School" and "Schoolwork" are siblings, and so are "A" and "A B" one level down.
    const labels = [folder(["School"]), folder(["Schoolwork"]), folder(["A"]), folder(["A B"])];
    expect(folderPaths(labels)).toEqual([["School"], ["Schoolwork"], ["A"], ["A B"]]);
  });

  it("shows a label carried twice once, and leaves out other kinds and unaccepted labels", () => {
    const labels = [
      folder(["School", "CSE 485"]),
      folder(["School", "CSE 485"], { source: "rule" }),
      folder(["Suggested"], { status: "suggested" }),
      folder(["Rejected", "Deeper"], { status: "rejected" }),
      folder(["Course"], { kind: "course" }),
      folder([]),
    ];
    expect(folderPaths(labels)).toEqual([["School", "CSE 485"]]);
  });

  it("does not let a rejected deeper label hide the accepted folder above it", () => {
    const labels = [folder(["School"]), folder(["School", "Old"], { status: "rejected" })];
    expect(folderPaths(labels)).toEqual([["School"]]);
  });

  it("keeps the same name at two levels", () => {
    expect(folderPaths([folder(["A"]), folder(["A", "A"])])).toEqual([["A", "A"]]);
  });

  it("is empty when the document has no folder labels", () => {
    expect(folderPaths([])).toEqual([]);
  });
});

describe("file property dates", () => {
  // en-US is the locale the test environment's Intl data always has.
  it.each([
    ["2026", "2026"],
    ["2026-03", "March 2026"],
    ["2026-03-04", "Mar 4, 2026"],
    // A day is never moved by the viewer's time zone: the first and last day of a year stay.
    ["2026-01-01", "Jan 1, 2026"],
    ["2026-12-31", "Dec 31, 2026"],
    // Years below 100 are not read as 1900 to 1999.
    ["0050-06-15", "Jun 15, 50"],
    ["0001", "1"],
  ])("shows %s as %s", (value, shown) => {
    expect(formatPropertyDate(value, "en-US")).toBe(shown);
  });

  it("shows a time in the viewer's time zone, as the rest of the page does", () => {
    const value = "2026-03-04T05:06:07Z";
    expect(formatPropertyDate(value, "en-US")).toBe(
      new Date(value).toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" }),
    );
  });

  it.each([
    "2026-02-30",
    "2026-13",
    "2026-00-10",
    "0000",
    "2026-03-04T24:00:00Z",
    "2026-03-04T05:06:07+01:00",
    "2026-03-04T05:06Z",
    "2026\n",
    "2026-03-04\r",
    " 2026",
    "2026 ",
    "March 2026",
    "<b>2026</b>",
    "",
  ])("shows %j, which the backend never stores, as written", (value) => {
    expect(formatPropertyDate(value, "en-US")).toBe(value);
  });
});

describe("title source", () => {
  it("has a text for every source the backend names, and no other", () => {
    const schema = JSON.parse(readFileSync("openapi.json", "utf8")) as {
      components: { schemas: { Title: { properties: { source: { enum: string[] } } } } };
    };
    expect(Object.keys(TITLE_SOURCE_TEXT).sort()).toEqual(
      [...schema.components.schemas.Title.properties.source.enum].sort(),
    );
  });
});
