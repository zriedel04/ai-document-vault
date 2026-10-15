import { describe, expect, it } from "vitest";
import { launchId } from "../vite.config";

const LAUNCH = "abcdefghijklmnopqrstuvwxyzABCDEF-_012345";

describe("the launch id the dev server answers with", () => {
  it("is the value the launcher started it with", () => {
    expect(launchId(LAUNCH)).toBe(LAUNCH);
  });

  it.each([
    ["no value", undefined],
    ["an empty value", ""],
    ["31 characters", "a".repeat(31)],
    ["129 characters", "a".repeat(129)],
    ["a trailing newline", `${LAUNCH}\n`],
    ["a trailing space", `${LAUNCH} `],
    ["a mark outside the alphabet", `${LAUNCH}!`],
  ])("is null for %s", (_name, value) => {
    expect(launchId(value)).toBeNull();
  });
});
