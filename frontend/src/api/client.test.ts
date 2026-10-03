import { describe, expect, it, vi } from "vitest";

import { type FetchFn, TEST_TOKEN, jsonResponse } from "../test/render";
import { createApi } from "./client";

function requests() {
  const fetchSpy = vi.fn<FetchFn>(() =>
    Promise.resolve(jsonResponse({ status: "ok", version: "1" })),
  );
  const sent = () => fetchSpy.mock.calls.map(([input]) => input as Request);
  return { fetchSpy, sent };
}

describe("the API client", () => {
  it("sends the token as a bearer header, and only that", async () => {
    const { fetchSpy, sent } = requests();
    await createApi(fetchSpy, () => TEST_TOKEN).GET("/api/health");
    const [request] = sent();
    expect(request?.headers.get("authorization")).toBe(`Bearer ${TEST_TOKEN}`);
    expect(request?.headers.get("cookie")).toBeNull();
    expect(request?.credentials).not.toBe("include");
  });

  it("sends no header when there is no token", async () => {
    const { fetchSpy, sent } = requests();
    await createApi(fetchSpy, () => null).GET("/api/health");
    expect(sent()[0]?.headers.has("authorization")).toBe(false);
  });

  it("asks only its own origin, so the token never goes anywhere else", async () => {
    const { fetchSpy, sent } = requests();
    const api = createApi(fetchSpy, () => TEST_TOKEN);
    await api.GET("/api/health");
    await api.GET("/api/session");
    for (const request of sent()) {
      expect(new URL(request.url).origin).toBe(window.location.origin);
    }
  });

  it("reads the stored token for each request, so a token saved later is used", async () => {
    const { fetchSpy, sent } = requests();
    let token: string | null = null;
    const api = createApi(fetchSpy, () => token);
    await api.GET("/api/health");
    token = TEST_TOKEN;
    await api.GET("/api/health");
    expect(sent().map((r) => r.headers.get("authorization"))).toEqual([
      null,
      `Bearer ${TEST_TOKEN}`,
    ]);
  });
});
