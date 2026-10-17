import { readFileSync } from "node:fs";

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type DocumentDetail,
  type DocumentItem,
  MAX_OFFSET,
  MAX_PAGE,
  ORDERS,
  PAGE_SIZE,
  SORT_KEYS,
  errorFor,
  fileName,
  formatSize,
} from "../api/documents";
import { type FetchFn, TEST_TOKEN, healthyBackend, jsonResponse, renderApp } from "../test/render";
import { DEFAULT_SEARCH, parseDocumentsSearch } from "./documents-search";

const ID = "01J9Z8Y7X6W5V4T3S2R1Q0P9N8";

function item(overrides: Partial<DocumentItem> = {}): DocumentItem {
  return {
    id: ID,
    name: "Lease agreement.pdf",
    extension: "pdf",
    mime: "application/pdf",
    size: 1536,
    state: "processing",
    added_at: "2026-10-01T12:00:00Z",
    trashed_at: null,
    sources: 1,
    ...overrides,
  };
}

function detail(overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    ...item(),
    sha256: "ab".repeat(32),
    local_only: false,
    version_id: "01J9Z8Y7X6W5V4T3S2R1Q0P9N9",
    version_no: 1,
    version_origin: "import",
    import_sources: [
      {
        id: "01J9Z8Y7X6W5V4T3S2R1Q0P9NA",
        filename: "Lease agreement.pdf",
        original_path: null,
        folders: [],
        original_mtime: null,
        method: "upload",
        batch_id: "01J9Z8Y7X6W5V4T3S2R1Q0P9NB",
        imported_at: "2026-10-01T12:00:00Z",
      },
    ],
    jobs: [
      {
        stage: "extract",
        status: "failed",
        attempts: 3,
        max_attempts: 3,
        last_error: "ExtractionTimeout",
        finished_at: "2026-10-01T12:05:00Z",
      },
    ],
    ...overrides,
  };
}

type Handler = (request: Request, url: URL) => Response | Promise<Response> | undefined;

/** The real backend's open paths, plus whatever `handler` answers for the documents API. */
function backend(handler: Handler) {
  return vi.fn<FetchFn>((input) => {
    const request = input as Request;
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/documents")) {
      if (request.headers.get("authorization") !== `Bearer ${TEST_TOKEN}`) {
        return Promise.resolve(jsonResponse({ detail: "request refused" }, 401));
      }
      const answer = handler(request, url);
      if (answer !== undefined) return Promise.resolve(answer);
    }
    return healthyBackend(input);
  });
}

function listOf(items: DocumentItem[], total = items.length): Handler {
  return (_request, url) =>
    url.pathname === "/api/documents"
      ? jsonResponse({
          items,
          total,
          limit: Number(url.searchParams.get("limit")),
          offset: Number(url.searchParams.get("offset")),
        })
      : undefined;
}

/**
 * A backend whose token can change while the page is open, as when the server restarted with a
 * new `local-token`: every protected path refuses any token but `accepted.token`.
 */
function restartingBackend(handler: Handler) {
  const accepted = { token: TEST_TOKEN };
  const spy = vi.fn<FetchFn>((input) => {
    const request = input as Request;
    const url = new URL(request.url);
    const isProtected =
      url.pathname === "/api/session" || url.pathname.startsWith("/api/documents");
    if (isProtected && request.headers.get("authorization") !== `Bearer ${accepted.token}`) {
      return Promise.resolve(jsonResponse({ detail: "request refused" }, 401));
    }
    if (url.pathname === "/api/session") {
      return Promise.resolve(jsonResponse({ id: "local", name: "Local user" }));
    }
    const answer = isProtected ? handler(request, url) : undefined;
    return answer === undefined ? healthyBackend(input) : Promise.resolve(answer);
  });
  return { spy, accepted };
}

const NEW_TOKEN = "n3w-token-after-restart_".repeat(2);

async function pasteToken(token: string) {
  expect(await screen.findByText("The access token was not accepted")).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("Access token"), token);
  await userEvent.click(screen.getByRole("button", { name: "Save token" }));
}

function listRequests(spy: ReturnType<typeof backend>): URLSearchParams[] {
  return spy.mock.calls
    .map(([input]) => new URL((input as Request).url))
    .filter((url) => url.pathname === "/api/documents")
    .map((url) => url.searchParams);
}

describe("document list", () => {
  it("shows each document with its name, type, size and status, linked to its page", async () => {
    const spy = backend(listOf([item(), item({ id: "B".repeat(26), name: null, sources: 2 })]));
    await renderApp({ route: "/documents", fetch: spy, token: TEST_TOKEN });

    const table = await screen.findByRole("table");
    const link = within(table).getByRole("link", { name: "Lease agreement.pdf" });
    expect(link).toHaveAttribute("href", `/documents/${ID}`);
    // A document whose imports gave no name is shown by the name it downloads under.
    expect(within(table).getByRole("link", { name: `${"B".repeat(26)}.pdf` })).toBeInTheDocument();
    expect(within(table).getByText("(2 imports)")).toBeInTheDocument();
    expect(within(table).getAllByText("1.5 KB")).toHaveLength(2);
    expect(within(table).getAllByText("Processing")).toHaveLength(2);
    expect(screen.getByRole("status")).toHaveTextContent("Showing 1 to 2 of 2 documents");

    const [query] = listRequests(spy);
    expect(Object.fromEntries(query ?? [])).toEqual({
      sort: "added",
      order: "desc",
      limit: String(PAGE_SIZE),
      offset: "0",
      trash: "false",
    });
  });

  it("makes no request that would be refused while the browser has no token", async () => {
    const spy = backend(listOf([item()]));
    await renderApp({ route: "/documents", fetch: spy });
    expect(await screen.findByText(/has no access to the vault yet/)).toBeInTheDocument();
    expect(listRequests(spy)).toHaveLength(0);
  });

  it("says when the library is empty, and when the trash is", async () => {
    await renderApp({ route: "/documents", fetch: backend(listOf([])), token: TEST_TOKEN });
    expect(await screen.findByText("No documents yet.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Trash" }));
    expect(await screen.findByText("The trash is empty.")).toBeInTheDocument();
  });

  it("asks the backend for the chosen sort and order, from the first page", async () => {
    const spy = backend(listOf([item()], 120));
    await renderApp({ route: "/documents?page=2", fetch: spy, token: TEST_TOKEN });
    await screen.findByRole("table");
    expect(listRequests(spy).at(-1)?.get("offset")).toBe(String(PAGE_SIZE));

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Sort by" }), "Name");
    await waitFor(() => {
      expect(listRequests(spy).at(-1)?.get("sort")).toBe("name");
    });
    expect(listRequests(spy).at(-1)?.get("offset")).toBe("0");

    await userEvent.click(screen.getByText("Ascending"));
    await waitFor(() => {
      expect(listRequests(spy).at(-1)?.get("order")).toBe("asc");
    });
  });

  it("pages through the list", async () => {
    const spy = backend(listOf([item()], 120));
    await renderApp({ route: "/documents", fetch: spy, token: TEST_TOKEN });
    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => {
      expect(listRequests(spy).at(-1)?.get("offset")).toBe(String(PAGE_SIZE));
    });
    await userEvent.click(screen.getByRole("button", { name: "3" }));
    await waitFor(() => {
      expect(listRequests(spy).at(-1)?.get("offset")).toBe(String(2 * PAGE_SIZE));
    });
  });

  it("lists the trash, from its first page, with the date each was moved there", async () => {
    const trashed = item({ state: "trashed", trashed_at: "2026-10-05T09:30:00Z" });
    const spy = backend(listOf([trashed], 120));
    await renderApp({ route: "/documents?page=2", fetch: spy, token: TEST_TOKEN });
    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("tab", { name: "Trash" }));
    await waitFor(() => {
      expect(listRequests(spy).at(-1)?.get("trash")).toBe("true");
    });
    expect(listRequests(spy).at(-1)?.get("offset")).toBe("0");
    expect(await screen.findByRole("columnheader", { name: "Moved to trash" })).toBeInTheDocument();
    expect(screen.getByText("In trash")).toBeInTheDocument();
  });

  it("goes to the last page when the address points past the end", async () => {
    const spy = backend((_request, url) => {
      if (url.pathname !== "/api/documents") return undefined;
      const offset = Number(url.searchParams.get("offset"));
      return jsonResponse({ items: offset < 120 ? [item()] : [], total: 120, limit: 50, offset });
    });
    await renderApp({ route: "/documents?page=9", fetch: spy, token: TEST_TOKEN });
    await waitFor(() => {
      expect(listRequests(spy).at(-1)?.get("offset")).toBe(String(2 * PAGE_SIZE));
    });
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("brings the token notice back when the backend stops accepting the token", async () => {
    const { spy, accepted } = restartingBackend(listOf([item()], 120));
    await renderApp({ route: "/documents", fetch: spy, token: TEST_TOKEN });
    await screen.findByRole("table");
    expect(screen.queryByText("Access token")).toBeNull();

    accepted.token = NEW_TOKEN;
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await pasteToken(NEW_TOKEN);

    expect(await screen.findByRole("table")).toBeInTheDocument();
    const last = spy.mock.calls.map(([input]) => input as Request).at(-1);
    expect(last?.headers.get("authorization")).toBe(`Bearer ${NEW_TOKEN}`);
    expect(screen.queryByText("The access token was not accepted")).toBeNull();
  });

  it("never asks the backend for a page past the largest offset it accepts", async () => {
    const spy = backend(listOf([item()], 120));
    await renderApp({
      route: `/documents?page=${String(MAX_PAGE + 1)}`,
      fetch: spy,
      token: TEST_TOKEN,
    });
    await screen.findByRole("table");

    const offsets = listRequests(spy).map((query) => Number(query.get("offset")));
    expect(offsets.length).toBeGreaterThan(0);
    expect(Math.max(...offsets)).toBeLessThanOrEqual(MAX_OFFSET);
  });

  it("ignores search values it does not understand instead of sending them", async () => {
    const spy = backend(listOf([item()]));
    await renderApp({
      route: "/documents?sort=%22secret%22&order=%22up%22&page=-3&trash=%22yes%22",
      fetch: spy,
      token: TEST_TOKEN,
    });
    await screen.findByRole("table");
    const query = listRequests(spy).at(-1);
    expect(query?.get("sort")).toBe("added");
    expect(query?.get("order")).toBe("desc");
    expect(query?.get("offset")).toBe("0");
    expect(query?.get("trash")).toBe("false");
  });

  it.each([
    [503, "No vault is open."],
    [500, "The documents could not be loaded."],
  ])("explains a %i and loads again on request", async (status, text) => {
    let failing = true;
    const spy = backend((_request, url) => {
      if (url.pathname !== "/api/documents") return undefined;
      return failing ? jsonResponse({ detail: "x" }, status) : listOf([item()])(_request, url);
    });
    await renderApp({ route: "/documents", fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(new RegExp(text))).toBeInTheDocument();
    failing = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("never shows an older page's answer for the page now asked for", async () => {
    const answers: ((response: Response) => void)[] = [];
    const spy = vi.fn<FetchFn>((input) =>
      new URL((input as Request).url).pathname === "/api/documents"
        ? new Promise<Response>((resolve) => answers.push(resolve))
        : healthyBackend(input),
    );
    const library = { items: [item()], total: 1, limit: 50, offset: 0 };
    await renderApp({ route: "/documents", fetch: spy, token: TEST_TOKEN });
    await waitFor(() => {
      expect(answers).toHaveLength(1);
    });
    answers[0]?.(jsonResponse(library));
    expect(await screen.findByRole("table")).toBeInTheDocument();

    // While the trash is asked for, the library's rows are not shown as if they were the trash.
    await userEvent.click(screen.getByRole("tab", { name: "Trash" }));
    await waitFor(() => {
      expect(answers).toHaveLength(2);
    });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Loading documents")).toBeInTheDocument();

    // Back to the library and over to the trash again: the first trash request answers last.
    await userEvent.click(screen.getByRole("tab", { name: "Library" }));
    await userEvent.click(screen.getByRole("tab", { name: "Trash" }));
    await waitFor(() => {
      expect(answers).toHaveLength(4);
    });
    answers[3]?.(jsonResponse({ items: [], total: 0, limit: 50, offset: 0 }));
    expect(await screen.findByText("The trash is empty.")).toBeInTheDocument();
    answers[2]?.(jsonResponse(library));
    answers[1]?.(jsonResponse(library));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText("The trash is empty.")).toBeInTheDocument();
  });
});

describe("document details", () => {
  const detailHandler =
    (document: DocumentDetail): Handler =>
    (_request, url) =>
      url.pathname === `/api/documents/${document.id}` ? jsonResponse(document) : undefined;

  it("opens from the list and shows the hash, imports and processing steps", async () => {
    const spy = backend(
      (request, url) => listOf([item()])(request, url) ?? detailHandler(detail())(request, url),
    );
    await renderApp({ route: "/documents", fetch: spy, token: TEST_TOKEN });
    await userEvent.click(await screen.findByRole("link", { name: "Lease agreement.pdf" }));

    expect(
      await screen.findByRole("heading", { level: 2, name: "Lease agreement.pdf" }),
    ).toBeInTheDocument();
    expect(screen.getByText("ab".repeat(32))).toBeInTheDocument();
    expect(screen.getByText("Not known (uploaded)")).toBeInTheDocument();
    expect(screen.getByText("Upload")).toBeInTheDocument();
    expect(screen.getByText("Text extraction")).toBeInTheDocument();
    expect(screen.getByText("ExtractionTimeout")).toBeInTheDocument();
    expect(screen.getByText("3 of 3")).toBeInTheDocument();
    // The Documents section stays marked while a document is open.
    expect(screen.getByRole("link", { name: "Documents" })).toHaveAttribute("data-active", "true");
  });

  it("shows where an imported file came from", async () => {
    const first = detail().import_sources[0];
    if (first === undefined) throw new Error("the fixture has an import");
    const document = detail({
      import_sources: [
        {
          ...first,
          original_path: "C:\\Synthetic\\School\\Lease agreement.pdf",
          method: "folder",
        },
      ],
    });
    await renderApp({
      route: `/documents/${ID}`,
      fetch: backend(detailHandler(document)),
      token: TEST_TOKEN,
    });
    expect(
      await screen.findByText("C:\\Synthetic\\School\\Lease agreement.pdf"),
    ).toBeInTheDocument();
    expect(screen.getByText("Folder import")).toBeInTheDocument();
  });

  it("says when there is no such document, without offering to try again", async () => {
    await renderApp({
      route: "/documents/nope",
      fetch: backend(() => undefined),
      token: TEST_TOKEN,
    });
    expect(await screen.findByText("There is no such document.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to documents" })).toHaveAttribute(
      "href",
      "/documents",
    );
  });
});

describe("download original", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function captureSave() {
    const saved: { name: string; href: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      saved.push({ name: this.download, href: this.href });
    });
    const create = vi.fn<(blob: Blob) => string>(() => "blob:test/1");
    const revoke = vi.fn<(url: string) => void>();
    URL.createObjectURL = create;
    URL.revokeObjectURL = revoke;
    return { saved, create, revoke };
  }

  it("fetches the bytes with the token and saves them under the document's name", async () => {
    const { saved, create, revoke } = captureSave();
    const timers = vi.spyOn(globalThis, "setTimeout");
    const spy = backend((_request, url) => {
      if (url.pathname === `/api/documents/${ID}/file`) {
        return new Response(new Uint8Array([0x25, 0x50, 0x44, 0x46]), {
          headers: { "content-type": "application/pdf" },
        });
      }
      return url.pathname === `/api/documents/${ID}` ? jsonResponse(detail()) : undefined;
    });
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    await userEvent.click(await screen.findByRole("button", { name: "Download original" }));

    await waitFor(() => {
      expect(saved).toEqual([{ name: "Lease agreement.pdf", href: "blob:test/1" }]);
    });
    const blob = create.mock.calls[0]?.[0] ?? new Blob();
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(
      new Uint8Array([0x25, 0x50, 0x44, 0x46]),
    );
    // The link is gone from the page, and the URL is released later, not while the browser reads it.
    expect(document.querySelector('a[href="blob:test/1"]')).toBeNull();
    expect(revoke).not.toHaveBeenCalled();
    const release = timers.mock.calls.find(([, delay]) => delay === 60_000)?.[0];
    expect(release).toBeTypeOf("function");
    (release as () => void)();
    expect(revoke).toHaveBeenCalledWith("blob:test/1");
    const fileRequest = spy.mock.calls
      .map(([input]) => input as Request)
      .find((request) => new URL(request.url).pathname.endsWith("/file"));
    expect(fileRequest?.headers.get("authorization")).toBe(`Bearer ${TEST_TOKEN}`);
  });

  it("brings the token notice back when the download is refused", async () => {
    const { saved } = captureSave();
    const { spy, accepted } = restartingBackend((_request, url) =>
      url.pathname === `/api/documents/${ID}` ? jsonResponse(detail()) : undefined,
    );
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    const button = await screen.findByRole("button", { name: "Download original" });

    accepted.token = NEW_TOKEN;
    await userEvent.click(button);
    await pasteToken(NEW_TOKEN);

    expect(await screen.findByRole("button", { name: "Download original" })).toBeInTheDocument();
    expect(saved).toEqual([]);
  });

  it("says when the stored copy is unavailable and saves nothing", async () => {
    const { saved } = captureSave();
    const spy = backend((_request, url) => {
      if (url.pathname === `/api/documents/${ID}/file`) return jsonResponse({ detail: "x" }, 500);
      return url.pathname === `/api/documents/${ID}` ? jsonResponse(detail()) : undefined;
    });
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    await userEvent.click(await screen.findByRole("button", { name: "Download original" }));
    expect(
      await screen.findByText(/stored copy of this file is missing or damaged/),
    ).toBeInTheDocument();
    expect(saved).toEqual([]);
  });
});

describe("helpers", () => {
  it.each([
    [0, "0 bytes"],
    [1, "1 byte"],
    [1023, "1023 bytes"],
    [1024, "1 KB"],
    [1536, "1.5 KB"],
    [10 * 1024, "10 KB"],
    [5 * 1024 * 1024, "5 MB"],
    [3 * 1024 ** 3, "3 GB"],
    [2 * 1024 ** 5, "2048 TB"],
  ])("formats %i bytes as %s", (bytes, text) => {
    expect(formatSize(bytes)).toBe(text);
  });

  it("names a document by its first import, or by its id and extension", () => {
    expect(fileName(item())).toBe("Lease agreement.pdf");
    expect(fileName(item({ name: null, extension: "md" }))).toBe(`${ID}.md`);
  });

  it.each([
    [401, "needs-token"],
    [404, "not-found"],
    [500, "unreachable"],
    [503, "no-vault"],
    [502, "unreachable"],
  ] as const)("reads status %i as %s", (status, error) => {
    expect(errorFor(status)).toBe(error);
  });

  it.each([
    [{}, DEFAULT_SEARCH],
    [
      { sort: "size", order: "asc", page: 3, trash: true },
      { sort: "size", order: "asc", page: 3, trash: true },
    ],
    [{ sort: "Size", order: "ASC", page: "3", trash: "true" }, DEFAULT_SEARCH],
    [{ page: 0 }, DEFAULT_SEARCH],
    [{ page: 1.5 }, DEFAULT_SEARCH],
    [{ page: Number.MAX_SAFE_INTEGER + 1 }, DEFAULT_SEARCH],
    [{ page: MAX_PAGE }, { ...DEFAULT_SEARCH, page: MAX_PAGE }],
    [{ page: MAX_PAGE + 1 }, { ...DEFAULT_SEARCH, page: MAX_PAGE }],
    [{ page: Number.MAX_SAFE_INTEGER }, { ...DEFAULT_SEARCH, page: MAX_PAGE }],
    [{ trash: 1 }, DEFAULT_SEARCH],
  ])("reads the address %j", (search, expected) => {
    expect(parseDocumentsSearch(search)).toEqual(expected);
  });

  it("pages only as far as the backend's largest offset", () => {
    const schema = JSON.parse(readFileSync("openapi.json", "utf8")) as {
      paths: {
        "/api/documents": { get: { parameters: { name: string; schema: { maximum?: number } }[] } };
      };
    };
    const offset = schema.paths["/api/documents"].get.parameters.find(
      (parameter) => parameter.name === "offset",
    );
    expect(offset?.schema.maximum).toBe(MAX_OFFSET);
    expect((MAX_PAGE - 1) * PAGE_SIZE).toBeLessThanOrEqual(MAX_OFFSET);
    expect(MAX_PAGE * PAGE_SIZE).toBeGreaterThan(MAX_OFFSET);
  });

  it("knows every sort and order the backend accepts, and no other", () => {
    const schema = JSON.parse(readFileSync("openapi.json", "utf8")) as {
      components: { schemas: { SortKey: { enum: string[] }; Order: { enum: string[] } } };
    };
    expect([...SORT_KEYS].sort()).toEqual([...schema.components.schemas.SortKey.enum].sort());
    expect([...ORDERS].sort()).toEqual([...schema.components.schemas.Order.enum].sort());
  });
});
