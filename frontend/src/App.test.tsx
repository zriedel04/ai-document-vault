import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { NAV_ITEMS, isInSection } from "./nav";
import { type FetchFn, TEST_TOKEN, jsonResponse, renderApp } from "./test/render";
import { readToken } from "./api/token";

describe("navigation", () => {
  it("lists every section", async () => {
    await renderApp();
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const item of NAV_ITEMS) {
      expect(within(nav).getByRole("link", { name: item.label })).toBeInTheDocument();
    }
  });

  it("shows the dashboard first and marks it as the current page", async () => {
    await renderApp();
    expect(screen.getByRole("heading", { level: 2, name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
  });

  // The dashboard, documents, import and search pages are built; the others are placeholders.
  it.each(
    NAV_ITEMS.filter((item) => !["/", "/documents", "/import", "/search"].includes(item.path)),
  )("opens the $label placeholder", async ({ label, path, description }) => {
    await renderApp();
    await userEvent.click(screen.getByRole("link", { name: label }));
    expect(screen.getByRole("heading", { level: 2, name: label })).toBeInTheDocument();
    expect(screen.getByText(description)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: label })).toHaveAttribute("href", path);
  });

  it("marks only the current section, not the dashboard, on another page", async () => {
    await renderApp({ route: "/search" });
    expect(screen.getByRole("link", { name: "Search" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Dashboard" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Search" })).toHaveAttribute("data-active", "true");
    expect(screen.getByRole("link", { name: "Dashboard" })).not.toHaveAttribute("data-active");
  });

  it("does not mark a section whose name is only the start of an unknown address", async () => {
    await renderApp({ route: "/documents-old" });
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    for (const item of NAV_ITEMS) {
      const link = screen.getByRole("link", { name: item.label });
      expect(link).not.toHaveAttribute("aria-current");
      expect(link).not.toHaveAttribute("data-active"); // the highlight, set by Shell
    }
  });

  it("shows a not-found page for an unknown address", async () => {
    await renderApp({ route: "/nope" });
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("loads a section directly from its address", async () => {
    await renderApp({ route: "/search" });
    expect(screen.getByRole("heading", { level: 2, name: "Search" })).toBeInTheDocument();
  });
});

describe("isInSection", () => {
  it.each([
    ["/documents", "/documents", true],
    ["/documents/", "/documents", true],
    ["/documents/42", "/documents", true],
    ["/documents-old", "/documents", false],
    ["/documentsx/documents", "/documents", false],
    ["/", "/", true],
    ["/search", "/", false],
    ["", "/documents", false],
  ])("%s in section %s is %s", (pathname, path, expected) => {
    expect(isInSection(pathname, path)).toBe(expected);
  });
});

describe("backend health", () => {
  it("shows that the backend is ok, with its version", async () => {
    await renderApp();
    expect(await screen.findByText("Backend: ok")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("The backend is running (version 1.2.3).");
  });

  it("asks the API at /api/health", async () => {
    const fetchSpy = vi.fn<FetchFn>(() =>
      Promise.resolve(jsonResponse({ status: "ok", version: "1.2.3" })),
    );
    await renderApp({ fetch: fetchSpy });
    await screen.findByText("Backend: ok");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const request = fetchSpy.mock.calls[0]?.[0] as Request;
    const url = new URL(request.url);
    expect(url.origin).toBe(window.location.origin);
    expect(url.pathname).toBe("/api/health");
    expect(request.method).toBe("GET");
  });

  it("says the backend is checking until it answers", async () => {
    await renderApp({ fetch: () => new Promise<Response>(() => undefined) });
    expect(screen.getByText("Backend: checking")).toBeInTheDocument();
  });

  it("says the backend is unreachable when the request fails", async () => {
    await renderApp({ fetch: () => Promise.reject(new TypeError("network down")) });
    expect(await screen.findByText("Backend: unreachable")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("did not answer");
  });

  it("says the backend is unreachable on a server error", async () => {
    await renderApp({ fetch: () => Promise.resolve(jsonResponse({ detail: "boom" }, 500)) });
    expect(await screen.findByText("Backend: unreachable")).toBeInTheDocument();
  });
});

describe("colour scheme", () => {
  it("switches between light and dark mode", async () => {
    await renderApp();
    const root = document.documentElement;
    await waitFor(() => {
      expect(root).toHaveAttribute("data-mantine-color-scheme", "light");
    });

    await userEvent.click(screen.getByRole("button", { name: "Switch to dark mode" }));
    await waitFor(() => {
      expect(root).toHaveAttribute("data-mantine-color-scheme", "dark");
    });

    await userEvent.click(screen.getByRole("button", { name: "Switch to light mode" }));
    await waitFor(() => {
      expect(root).toHaveAttribute("data-mantine-color-scheme", "light");
    });
  });
});

describe("access token", () => {
  const sessionCalls = (spy: ReturnType<typeof vi.fn<FetchFn>>) =>
    spy.mock.calls
      .map(([input]) => input as Request)
      .filter((request) => new URL(request.url).pathname === "/api/session");

  it("asks for the token, and makes no request that would be refused, when there is none", async () => {
    const { healthyBackend } = await import("./test/render");
    const spy = vi.fn<FetchFn>(healthyBackend);
    await renderApp({ fetch: spy });
    expect(await screen.findByText("Access token needed")).toBeInTheDocument();
    await screen.findByText("Backend: ok");
    expect(sessionCalls(spy)).toHaveLength(0);
    expect(screen.getByText(/has no access token yet/)).toBeInTheDocument();
  });

  it("sends the stored token and shows that the browser has access", async () => {
    const { healthyBackend } = await import("./test/render");
    const spy = vi.fn<FetchFn>(healthyBackend);
    await renderApp({ fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText("This browser has access, as Local user.")).toBeInTheDocument();
    expect(screen.queryByText("Access token needed")).not.toBeInTheDocument();
    const [call] = sessionCalls(spy);
    expect(call?.headers.get("authorization")).toBe(`Bearer ${TEST_TOKEN}`);
  });

  it("says when the stored token is not accepted", async () => {
    await renderApp({ token: "x".repeat(40) });
    expect(await screen.findByText("The access token was not accepted")).toBeInTheDocument();
    expect(screen.getByText(/was not accepted. Paste the current one/)).toBeInTheDocument();
  });

  it("says when the backend cannot be asked", async () => {
    const { healthyBackend } = await import("./test/render");
    await renderApp({
      token: TEST_TOKEN,
      fetch: (input) =>
        new URL((input as Request).url).pathname === "/api/session"
          ? Promise.reject(new TypeError("network down"))
          : healthyBackend(input),
    });
    expect(await screen.findByText(/Access could not be checked/)).toBeInTheDocument();
    expect(screen.queryByText("Access token needed")).not.toBeInTheDocument();
  });

  it("takes a pasted token, keeps it for this tab and signs in", async () => {
    await renderApp();
    const field = await screen.findByLabelText("Access token");
    expect(field).toHaveAttribute("type", "password"); // the token is never shown
    await userEvent.type(field, TEST_TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByText("This browser has access, as Local user.")).toBeInTheDocument();
    expect(screen.queryByText("Access token needed")).not.toBeInTheDocument();
    expect(readToken()).toBe(TEST_TOKEN);
    expect(document.body).not.toHaveTextContent(TEST_TOKEN);
  });

  it("refuses text that is not a token without saving or asking the backend", async () => {
    const { healthyBackend } = await import("./test/render");
    const spy = vi.fn<FetchFn>(healthyBackend);
    await renderApp({ fetch: spy });
    await userEvent.type(await screen.findByLabelText("Access token"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(screen.getByText("That does not look like an access token.")).toBeInTheDocument();
    expect(readToken()).toBeNull();
    expect(sessionCalls(spy)).toHaveLength(0);
  });

  it("asks again when a pasted token is not the right one", async () => {
    await renderApp();
    await userEvent.type(await screen.findByLabelText("Access token"), "y".repeat(40));
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByText("The access token was not accepted")).toBeInTheDocument();
  });
});
