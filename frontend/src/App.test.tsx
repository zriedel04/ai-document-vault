import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { NAV_ITEMS } from "./nav";
import { type FetchFn, jsonResponse, renderApp } from "./test/render";

describe("navigation", () => {
  it("lists every section", () => {
    renderApp();
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const item of NAV_ITEMS) {
      expect(within(nav).getByRole("link", { name: item.label })).toBeInTheDocument();
    }
  });

  it("shows the dashboard first and marks it as the current page", () => {
    renderApp();
    expect(screen.getByRole("heading", { level: 2, name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
  });

  it.each(NAV_ITEMS.filter((item) => item.path !== "/"))(
    "opens the $label placeholder",
    async ({ label, path, description }) => {
      renderApp();
      await userEvent.click(screen.getByRole("link", { name: label }));
      expect(screen.getByRole("heading", { level: 2, name: label })).toBeInTheDocument();
      expect(screen.getByText(description)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: label })).toHaveAttribute("href", path);
    },
  );

  it("shows a not-found page for an unknown address", () => {
    renderApp({ route: "/nope" });
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("loads a section directly from its address", () => {
    renderApp({ route: "/search" });
    expect(screen.getByRole("heading", { level: 2, name: "Search" })).toBeInTheDocument();
  });
});

describe("backend health", () => {
  it("shows that the backend is ok, with its version", async () => {
    renderApp();
    expect(await screen.findByText("Backend: ok")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("The backend is running (version 1.2.3).");
  });

  it("asks the API at /api/health", async () => {
    const fetchSpy = vi.fn<FetchFn>(() =>
      Promise.resolve(jsonResponse({ status: "ok", version: "1.2.3" })),
    );
    renderApp({ fetch: fetchSpy });
    await screen.findByText("Backend: ok");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const request = fetchSpy.mock.calls[0]?.[0] as Request;
    const url = new URL(request.url);
    expect(url.origin).toBe(window.location.origin);
    expect(url.pathname).toBe("/api/health");
    expect(request.method).toBe("GET");
  });

  it("says the backend is checking until it answers", () => {
    renderApp({ fetch: () => new Promise<Response>(() => undefined) });
    expect(screen.getByText("Backend: checking")).toBeInTheDocument();
  });

  it("says the backend is unreachable when the request fails", async () => {
    renderApp({ fetch: () => Promise.reject(new TypeError("network down")) });
    expect(await screen.findByText("Backend: unreachable")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("did not answer");
  });

  it("says the backend is unreachable on a server error", async () => {
    renderApp({ fetch: () => Promise.resolve(jsonResponse({ detail: "boom" }, 500)) });
    expect(await screen.findByText("Backend: unreachable")).toBeInTheDocument();
  });
});

describe("colour scheme", () => {
  it("switches between light and dark mode", async () => {
    renderApp();
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
