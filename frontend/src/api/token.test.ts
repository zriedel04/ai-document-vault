import { afterEach, describe, expect, it, vi } from "vitest";

import { TEST_TOKEN } from "../test/render";
import { TOKEN_KEY, forgetToken, isToken, readToken, saveToken, takeTokenFromUrl } from "./token";

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("isToken", () => {
  it("accepts the shape the backend creates", () => {
    expect(isToken(TEST_TOKEN)).toBe(true);
    expect(isToken("A".repeat(32))).toBe(true);
    expect(isToken("a-_".repeat(42))).toBe(true);
  });

  it.each([
    ["too short", "a".repeat(31)],
    ["too long", "a".repeat(129)],
    ["empty", ""],
    ["a trailing newline", `${"a".repeat(40)}\n`],
    ["a trailing space", `${"a".repeat(40)} `],
    ["a leading space", ` ${"a".repeat(40)}`],
    ["a path", "../".repeat(20)],
    ["non-ASCII letters", "é".repeat(40)],
    ["a plus sign", `${"a".repeat(40)}+`],
  ])("refuses %s", (_name, value) => {
    expect(isToken(value)).toBe(false);
  });
});

describe("keeping the token", () => {
  it("stores it for this tab only and reads it back", () => {
    expect(readToken()).toBeNull();
    expect(saveToken(TEST_TOKEN)).toBe(true);
    expect(readToken()).toBe(TEST_TOKEN);
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBe(TEST_TOKEN);
    expect(window.localStorage.length).toBe(0); // never in storage that outlives the tab
  });

  it("refuses something that is not a token and keeps nothing", () => {
    expect(saveToken("not a token")).toBe(false);
    expect(readToken()).toBeNull();
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("does not trust a stored value that is not shaped like a token", () => {
    window.sessionStorage.setItem(TOKEN_KEY, "<script>alert(1)</script>");
    expect(readToken()).toBeNull();
  });

  it("forgets it everywhere", () => {
    saveToken(TEST_TOKEN);
    forgetToken();
    expect(readToken()).toBeNull();
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("still works for this page when the browser refuses storage", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(saveToken(TEST_TOKEN)).toBe(true);
    expect(readToken()).toBe(TEST_TOKEN);
  });
});

function place(hash: string, pathname = "/documents", search = "?page=2") {
  const replace = vi.fn();
  const here = {
    location: { hash, pathname, search },
    history: { state: { key: 1 }, replaceState: replace },
  } as unknown as Pick<Window, "location" | "history">;
  return { here, replace };
}

describe("takeTokenFromUrl", () => {
  it("stores the token and removes the fragment, keeping path and query", () => {
    const { here, replace } = place(`#token=${TEST_TOKEN}`);
    expect(takeTokenFromUrl(here)).toBe(true);
    expect(readToken()).toBe(TEST_TOKEN);
    expect(replace).toHaveBeenCalledExactlyOnceWith({ key: 1 }, "", "/documents?page=2");
  });

  it("keeps other fragment parameters", () => {
    const { here, replace } = place(`#section=a&token=${TEST_TOKEN}`);
    expect(takeTokenFromUrl(here)).toBe(true);
    expect(replace).toHaveBeenCalledWith({ key: 1 }, "", "/documents?page=2#section=a");
  });

  it("removes a token that is not usable, stores nothing and says so", () => {
    for (const bad of ["short", "", `${TEST_TOKEN}%0A`, `${TEST_TOKEN}%20`]) {
      forgetToken();
      const { here, replace } = place(`#token=${bad}`);
      expect(takeTokenFromUrl(here)).toBe(false);
      expect(readToken()).toBeNull();
      expect(replace).toHaveBeenCalledWith({ key: 1 }, "", "/documents?page=2");
    }
  });

  it("uses the first of two tokens and removes both", () => {
    const { here, replace } = place(`#token=${TEST_TOKEN}&token=${"b".repeat(40)}`);
    expect(takeTokenFromUrl(here)).toBe(true);
    expect(readToken()).toBe(TEST_TOKEN);
    expect(replace).toHaveBeenCalledWith({ key: 1 }, "", "/documents?page=2");
  });

  it("leaves an address without a token alone", () => {
    for (const hash of ["", "#", "#section=a", "#tokens=x", "#TOKEN=x"]) {
      const { here, replace } = place(hash);
      expect(takeTokenFromUrl(here)).toBe(false);
      expect(replace).not.toHaveBeenCalled();
    }
  });

  it("works on the real address bar", () => {
    window.history.replaceState(null, "", `/search?q=a#token=${TEST_TOKEN}`);
    expect(takeTokenFromUrl()).toBe(true);
    expect(window.location.href).toBe(`${window.location.origin}/search?q=a`);
    expect(window.location.href).not.toContain(TEST_TOKEN);
    expect(readToken()).toBe(TEST_TOKEN);
  });
});
