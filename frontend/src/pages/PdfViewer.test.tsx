import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { pdfjs } from "react-pdf";
import { describe, expect, it, vi } from "vitest";

import { type DocumentDetail, VIEWER_ERROR_TEXT } from "../api/documents";
import { type FetchFn, TEST_TOKEN, healthyBackend, jsonResponse, renderApp } from "../test/render";
import { scriptPdf, seen } from "../test/react-pdf-script";
import { parseDocumentSearch } from "./documents-search";
import { PAGE_FAILED_TEXT, PDF_DAMAGED_TEXT, PDF_PASSWORD_TEXT } from "./PdfViewer";
import { PDF_OPTIONS, pageToShow, typedPage } from "./pdf-view";

const ID = "01J9Z8Y7X6W5V4T3S2R1Q0P9N8";
const ROUTE = `/documents/${ID}`;
// Synthetic bytes: the stand-in for react-pdf never parses them, the test only follows them.
const BYTES = new TextEncoder().encode("%PDF-1.7\n% synthetic test file\n");

const PDF: DocumentDetail = {
  id: ID,
  name: "Course reader.pdf",
  extension: "pdf",
  mime: "application/pdf",
  size: BYTES.length,
  state: "searchable",
  added_at: "2026-10-01T12:00:00Z",
  trashed_at: null,
  sources: 1,
  sha256: "ab".repeat(32),
  local_only: false,
  version_id: "01J9Z8Y7X6W5V4T3S2R1Q0P9N9",
  version_no: 1,
  version_origin: "import",
  import_sources: [],
  jobs: [],
};

type FileHandler = () => Response | Promise<Response>;

const servePdf: FileHandler = () =>
  new Response(BYTES, { headers: { "content-type": "application/pdf" } });

/** A backend with one document; `file` answers its download, and `accepted` is the token. */
function backend(file: FileHandler = servePdf, detail: DocumentDetail = PDF) {
  // `next`, when set, becomes the accepted token as the first request for the file arrives: the
  // server restarted with a new `local-token` after the details loaded.
  const accepted: { token: string; next?: string } = { token: TEST_TOKEN };
  const spy = vi.fn<FetchFn>((input) => {
    const request = input as Request;
    const url = new URL(request.url);
    if (url.pathname === `/api/documents/${ID}/file` && accepted.next !== undefined) {
      accepted.token = accepted.next;
      accepted.next = undefined;
    }
    const isProtected =
      url.pathname === "/api/session" || url.pathname.startsWith("/api/documents");
    if (isProtected && request.headers.get("authorization") !== `Bearer ${accepted.token}`) {
      return Promise.resolve(jsonResponse({ detail: "request refused" }, 401));
    }
    if (url.pathname === "/api/session") {
      return Promise.resolve(jsonResponse({ id: "local", name: "Local user" }));
    }
    if (url.pathname === `/api/documents/${ID}`) return Promise.resolve(jsonResponse(detail));
    if (url.pathname === `/api/documents/${ID}/file`) return Promise.resolve(file());
    if (url.pathname === `/api/documents/${ID}/pages`) {
      return Promise.resolve(
        jsonResponse({
          version_id: detail.version_id,
          extracted: false,
          extractor: null,
          total: 0,
          offset: 0,
          limit: 20,
          pages: [],
          next_offset: null,
        }),
      );
    }
    return healthyBackend(input);
  });
  return { spy, accepted };
}

function fileRequests(spy: ReturnType<typeof backend>["spy"]): Request[] {
  return spy.mock.calls
    .map(([input]) => input as Request)
    .filter((request) => new URL(request.url).pathname.endsWith("/file"));
}

async function shownPage(): Promise<string | null> {
  return (await screen.findByTestId("pdf-page")).getAttribute("data-page");
}

describe("PDF viewer", () => {
  it("fetches the original with the token in the header and gives PDF.js only its bytes", async () => {
    const { spy } = backend();
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await shownPage()).toBe("1");

    const [request, ...others] = fileRequests(spy);
    expect(others).toEqual([]);
    expect(request?.method).toBe("GET");
    expect(request?.headers.get("authorization")).toBe(`Bearer ${TEST_TOKEN}`);
    // The address names the document and nothing else: no token, no query, no fragment.
    const url = new URL(request?.url ?? "");
    expect(url.origin).toBe(window.location.origin);
    expect(url.pathname).toBe(`/api/documents/${ID}/file`);
    expect(url.search).toBe("");
    expect(url.hash).toBe("");
    expect(request?.url).not.toContain(TEST_TOKEN);

    // PDF.js gets the bytes and the fixed options, never an address to fetch.
    const [opened] = seen.documents;
    const file = opened?.file as { data: Uint8Array } | undefined;
    expect(Object.keys(file ?? {})).toEqual(["data"]);
    expect(Array.from(file?.data ?? [])).toEqual(Array.from(BYTES));
    expect(opened?.options).toBe(PDF_OPTIONS);
    expect(opened?.suspense).toBe(false);
  });

  it("asks PDF.js to fetch nothing, run nothing and log nothing", () => {
    expect(PDF_OPTIONS).toEqual({
      verbosity: 0,
      enableXfa: false,
      useWasm: false,
      useWorkerFetch: false,
    });
    for (const key of [
      "url",
      "cMapUrl",
      "standardFontDataUrl",
      "iccUrl",
      "wasmUrl",
      "docBaseUrl",
      "httpHeaders",
      "withCredentials",
    ]) {
      expect(PDF_OPTIONS).not.toHaveProperty(key);
    }
    expect(Object.isFrozen(PDF_OPTIONS)).toBe(true);
  });

  it("loads the PDF.js worker from this origin", async () => {
    // Set when the module loads; the address is the one Vite gives the bundled worker (mocked in
    // setup.ts as a path of this origin, as Vite's build gives it).
    await import("./PdfViewer");
    expect(pdfjs.GlobalWorkerOptions.workerSrc).toBe("/assets/pdf.worker.js");
  });

  it("draws no annotation layer and no form fields, so no link or script of the file is shown", async () => {
    const { spy } = backend();
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    await shownPage();
    expect(seen.pages.length).toBeGreaterThan(0);
    for (const page of seen.pages) {
      expect(page.renderAnnotationLayer).toBe(false);
      expect(page.renderForms).toBe(false);
    }
    expect(seen.documents.every((opened) => opened.externalLinkTarget === undefined)).toBe(true);
  });

  it("opens at the page the address names", async () => {
    const { spy } = backend();
    await renderApp({ route: `${ROUTE}?page=2`, fetch: spy, token: TEST_TOKEN });
    expect(await shownPage()).toBe("2");
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();
    expect(screen.getByLabelText("Go to page")).toHaveValue("2");
  });

  it.each(["0", "-1", "abc", "2.5", "%222%22", "", "true", "null"])(
    "opens at the first page when the address asks for page %j",
    async (value) => {
      const { spy } = backend();
      await renderApp({ route: `${ROUTE}?page=${value}`, fetch: spy, token: TEST_TOKEN });
      expect(await shownPage()).toBe("1");
      expect(screen.queryByText("No such page")).toBeNull();
    },
  );

  it("shows the last page, and says so, when the address asks for one past the end", async () => {
    const { spy } = backend();
    await renderApp({ route: `${ROUTE}?page=9`, fetch: spy, token: TEST_TOKEN });
    expect(await shownPage()).toBe("3");
    const notice = screen.getByRole("status");
    expect(within(notice).getByText("No such page")).toBeInTheDocument();
    expect(notice).toHaveTextContent("asks for page 9, but this PDF has 3 pages");
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });

  it("says so for a single page in the singular", async () => {
    scriptPdf({ pages: 1 });
    const { spy } = backend();
    await renderApp({ route: `${ROUTE}?page=2`, fetch: spy, token: TEST_TOKEN });
    expect(await shownPage()).toBe("1");
    expect(screen.getByRole("status")).toHaveTextContent("this PDF has 1 page.");
  });

  it("moves to the next and previous page, keeps it in the address and fetches the file once", async () => {
    const { spy } = backend();
    const { router } = await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await shownPage()).toBe("1");
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();

    const scrolled = vi.spyOn(window, "scrollTo");
    scrolled.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => {
      expect(screen.getByTestId("pdf-page")).toHaveAttribute("data-page", "2");
    });
    expect(router.state.location.search).toEqual({ page: 2 });
    // The details page stays where the owner scrolled it, instead of jumping to the top.
    expect(scrolled).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => {
      expect(screen.getByTestId("pdf-page")).toHaveAttribute("data-page", "3");
    });
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Previous page" }));
    await waitFor(() => {
      expect(screen.getByTestId("pdf-page")).toHaveAttribute("data-page", "2");
    });
    expect(router.state.location.search).toEqual({ page: 2 });

    // Back in the browser returns to the page before, and the field follows.
    router.history.back();
    await waitFor(() => {
      expect(screen.getByTestId("pdf-page")).toHaveAttribute("data-page", "3");
    });
    expect(screen.getByLabelText("Go to page")).toHaveValue("3");
    expect(fileRequests(spy)).toHaveLength(1);
    expect(seen.documents).toHaveLength(1);
  });

  it("goes to a page typed in, and refuses one the PDF does not have", async () => {
    const { spy } = backend();
    const { router } = await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    await shownPage();
    const field = screen.getByLabelText("Go to page");

    await userEvent.clear(field);
    await userEvent.type(field, "3");
    await userEvent.click(screen.getByRole("button", { name: "Go" }));
    await waitFor(() => {
      expect(screen.getByTestId("pdf-page")).toHaveAttribute("data-page", "3");
    });
    expect(router.state.location.search).toEqual({ page: 3 });

    for (const typed of ["7", "0"]) {
      await userEvent.clear(field);
      await userEvent.type(field, `${typed}{Enter}`);
      expect(await screen.findByText("A page from 1 to 3")).toBeInTheDocument();
      expect(screen.getByTestId("pdf-page")).toHaveAttribute("data-page", "3");
    }
    await userEvent.clear(field);
    await userEvent.click(screen.getByRole("button", { name: "Go" }));
    expect(screen.getByText("A page from 1 to 3")).toBeInTheDocument();
    expect(router.state.location.search).toEqual({ page: 3 });
  });

  it("says when the PDF cannot be opened", async () => {
    scriptPdf({ pages: 3, fail: "damaged" });
    const { spy } = backend();
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(PDF_DAMAGED_TEXT)).toBeInTheDocument();
    expect(screen.queryByTestId("pdf-page")).toBeNull();
  });

  it("never asks for a password, and says why the PDF is not shown", async () => {
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("guess");
    scriptPdf({ pages: 3, fail: "password" });
    const { spy } = backend();
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(PDF_PASSWORD_TEXT)).toBeInTheDocument();
    expect(prompt).not.toHaveBeenCalled();
    prompt.mockRestore();
  });

  it("says when a page cannot be drawn", async () => {
    scriptPdf({ pages: 3, pageFails: true });
    const { spy } = backend();
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(PAGE_FAILED_TEXT)).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
  });

  it("says this PDF has no pages", async () => {
    scriptPdf({ pages: 0 });
    const { spy } = backend();
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText("This PDF has no pages.")).toBeInTheDocument();
    expect(screen.queryByTestId("pdf-page")).toBeNull();
  });

  it.each([
    [500, "unavailable"],
    [503, "no-vault"],
    [502, "unreachable"],
  ] as const)("says why after a %i and offers to try again", async (status, error) => {
    let answer: FileHandler = () => jsonResponse({ detail: "server words" }, status);
    const { spy } = backend(() => answer());
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(VIEWER_ERROR_TEXT[error])).toBeInTheDocument();
    expect(screen.queryByText("server words")).toBeNull();

    answer = servePdf;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await shownPage()).toBe("1");
    expect(fileRequests(spy)).toHaveLength(2);
  });

  it("says when the backend cannot be reached", async () => {
    const { spy } = backend(() => Promise.reject(new TypeError("offline")));
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(VIEWER_ERROR_TEXT.unreachable)).toBeInTheDocument();
  });

  it("says when the document is gone, without offering to try again", async () => {
    const { spy } = backend(() => jsonResponse({ detail: "no such document" }, 404));
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    const alert = (await screen.findByText("PDF not loaded")).closest<HTMLElement>(
      '[role="alert"]',
    );
    expect(within(alert ?? document.body).getByText(VIEWER_ERROR_TEXT["not-found"])).toBeVisible();
    expect(within(alert ?? document.body).queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("brings the token notice back when the file is refused, and loads it with the new token", async () => {
    const newToken = "n3w-token-after-restart_".repeat(2);
    const { spy, accepted } = backend();
    accepted.next = newToken;
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText("The access token was not accepted")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Access token"), newToken);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await shownPage()).toBe("1");
  });

  it("keeps the text view, and fetches no file, for a document that is not a PDF", async () => {
    const { spy } = backend(servePdf, {
      ...PDF,
      name: "Notes.md",
      extension: "md",
      mime: "text/markdown",
    });
    await renderApp({ route: `${ROUTE}?page=2`, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByRole("heading", { name: "Text" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "PDF" })).toBeNull();
    expect(fileRequests(spy)).toEqual([]);
    expect(seen.documents).toEqual([]);
  });

  it("chooses the viewer by the media type the import found, not by the name", async () => {
    const { spy } = backend(servePdf, { ...PDF, name: "report.txt", extension: "txt" });
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await shownPage()).toBe("1");
  });
});

describe("the page in the address", () => {
  it("is a whole number from 1, or left out", () => {
    expect(parseDocumentSearch({ page: 4 })).toEqual({ page: 4 });
    expect(parseDocumentSearch({ page: 1 })).toEqual({ page: 1 });
    for (const page of [0, -1, 2.5, Number.NaN, Infinity, 2 ** 53, "4", true, null, [4], {}]) {
      expect(parseDocumentSearch({ page })).toEqual({});
    }
    expect(parseDocumentSearch({})).toEqual({});
  });

  it("is shown as asked, as page 1 when absent, and as the last page past the end", () => {
    expect(pageToShow(undefined, 5)).toEqual({ page: 1, pastEnd: false });
    expect(pageToShow(3, 5)).toEqual({ page: 3, pastEnd: false });
    expect(pageToShow(5, 5)).toEqual({ page: 5, pastEnd: false });
    expect(pageToShow(6, 5)).toEqual({ page: 5, pastEnd: true });
  });

  it("is typed as a whole number from 1 to the last page", () => {
    expect(typedPage("3", 5)).toBe(3);
    expect(typedPage(3, 5)).toBe(3);
    expect(typedPage("05", 5)).toBe(5);
    for (const typed of [
      "",
      "0",
      "6",
      "-1",
      "2.5",
      "1e1",
      " 3",
      "3 ",
      "3\n",
      "3\r",
      "x",
      0,
      6,
      2.5,
    ]) {
      expect(typedPage(typed, 5)).toBeNull();
    }
  });
});
