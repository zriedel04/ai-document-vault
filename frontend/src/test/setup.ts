import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { forgetToken } from "../api/token";
import { resetPdf } from "./react-pdf-script";

// PDF.js needs a canvas and a worker, which jsdom does not have: every test gets the stand-in in
// ./react-pdf.tsx instead, and the worker's address is a fixed one of this origin.
vi.mock("react-pdf", async () => ({
  ...(await import("./react-pdf")),
  pdfjs: (await import("./react-pdf-script")).pdfjs,
}));
vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?worker&url", () => ({
  default: "/assets/pdf.worker.js",
}));

// Mantine and the router use these browser APIs, which jsdom does not provide.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }),
});

// The router scrolls to the top after a navigation.
window.scrollTo = vi.fn();

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  window.sessionStorage.clear();
  forgetToken();
  resetPdf();
});
