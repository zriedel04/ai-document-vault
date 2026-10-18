import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  type DocumentDetail,
  type DocumentPage,
  type DocumentPages,
  PAGES_ERROR_TEXT,
  TEXT_WINDOW,
} from "../api/documents";
import { type FetchFn, TEST_TOKEN, healthyBackend, jsonResponse, renderApp } from "../test/render";
import { splitAtHeadings } from "./text-segments";

const ID = "01J9Z8Y7X6W5V4T3S2R1Q0P9N8";
const VERSION = "01J9Z8Y7X6W5V4T3S2R1Q0P9N9";
const ROUTE = `/documents/${ID}`;
// Letters outside ASCII and one character outside the Basic Multilingual Plane, from code points,
// so a heading after it sits one UTF-16 unit later than its offset in characters.
const UNICODE = `café — 履歴書 ${String.fromCodePoint(0x1f4c4)}`;

const DETAIL: DocumentDetail = {
  id: ID,
  name: "Notes.md",
  extension: "md",
  mime: "text/markdown",
  size: 120,
  state: "searchable",
  added_at: "2026-10-01T12:00:00Z",
  trashed_at: null,
  sources: 1,
  sha256: "ab".repeat(32),
  local_only: false,
  version_id: VERSION,
  version_no: 1,
  version_origin: "import",
  import_sources: [],
  jobs: [],
};

function pagesOf(pages: DocumentPage[], overrides: Partial<DocumentPages> = {}): DocumentPages {
  return {
    version_id: VERSION,
    extracted: true,
    extractor: "text-1",
    total: pages.length,
    offset: 0,
    limit: TEXT_WINDOW,
    pages,
    next_offset: null,
    ...overrides,
  };
}

type PagesHandler = (url: URL) => Response | Promise<Response>;

/** A backend with one document; `pages` answers its text, and `accepted` is the token it takes. */
function backend(pages: PagesHandler) {
  // `next`, when set, becomes the accepted token as the first request for the text arrives: the
  // server restarted with a new `local-token` after the details loaded.
  const accepted: { token: string; next?: string } = { token: TEST_TOKEN };
  const spy = vi.fn<FetchFn>((input) => {
    const request = input as Request;
    const url = new URL(request.url);
    if (url.pathname === `/api/documents/${ID}/pages` && accepted.next !== undefined) {
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
    if (url.pathname === `/api/documents/${ID}`) return Promise.resolve(jsonResponse(DETAIL));
    if (url.pathname === `/api/documents/${ID}/pages`) return Promise.resolve(pages(url));
    return healthyBackend(input);
  });
  return { spy, accepted };
}

function pageRequests(spy: ReturnType<typeof backend>["spy"]): URLSearchParams[] {
  return spy.mock.calls
    .map(([input]) => new URL((input as Request).url))
    .filter((url) => url.pathname === `/api/documents/${ID}/pages`)
    .map((url) => url.searchParams);
}

async function textSection() {
  const heading = await screen.findByRole("heading", { level: 3, name: "Text" });
  const section = heading.parentElement;
  if (section === null) throw new Error("the Text heading has a section");
  return section;
}

describe("text view", () => {
  it("shows each page as plain text with its number, and an outline that jumps to a heading", async () => {
    const first = `# ${UNICODE}\nintro\n## Plan\nstep one\n`;
    const plan = Array.from(first.slice(0, first.indexOf("## Plan"))).length;
    const { spy } = backend(() =>
      jsonResponse(
        pagesOf([
          {
            number: 1,
            text: first,
            headings: [
              { level: 1, text: UNICODE, offset: 0 },
              { level: 2, text: "Plan", offset: plan },
            ],
          },
          { number: 2, text: "second page\n", headings: [] },
        ]),
      ),
    );
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });

    const page1 = await screen.findByRole("region", { name: "Page 1" });
    expect(within(page1).getByText("Page 1")).toBeInTheDocument();
    expect(page1.querySelector("pre")?.textContent).toBe(first);
    const page2 = screen.getByRole("region", { name: "Page 2" });
    expect(page2.querySelector("pre")?.textContent).toBe("second page\n");
    expect(screen.getByText("Pages 1 to 2 of 2")).toBeInTheDocument();

    const outline = screen.getByRole("navigation", { name: "Outline" });
    await userEvent.click(within(outline).getByRole("button", { name: "Plan" }));
    // The offset counts characters, so the heading is found after the character outside the BMP.
    expect(window.document.activeElement?.textContent).toBe("## Plan\nstep one\n");
    await userEvent.click(within(outline).getByRole("button", { name: UNICODE }));
    expect(window.document.activeElement?.id).toMatch(/-p1-h0$/);
    expect(window.document.activeElement?.textContent).toBe(`# ${UNICODE}\nintro\n`);

    const [query] = pageRequests(spy);
    expect(query?.get("offset")).toBe("0");
    expect(query?.get("limit")).toBe(String(TEXT_WINDOW));
  });

  it("shows markup in the text literally and never as HTML", async () => {
    const markup =
      '<img src="x" onerror="window.injected=1"><b>bold</b><script>window.injected=2</script>&amp;';
    const { spy } = backend(() =>
      jsonResponse(
        pagesOf([
          {
            number: 1,
            text: markup,
            headings: [{ level: 1, text: "<i>title</i>", offset: 0 }],
          },
        ]),
      ),
    );
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });

    const page = await screen.findByRole("region", { name: "Page 1" });
    expect(page.querySelector("pre")?.textContent).toBe(markup);
    expect(page.querySelector("img, b, script")).toBeNull();
    const outline = screen.getByRole("navigation", { name: "Outline" });
    expect(within(outline).getByRole("button", { name: "<i>title</i>" })).toBeInTheDocument();
    expect(outline.querySelector("i")).toBeNull();
    expect((window as unknown as { injected?: number }).injected).toBeUndefined();
  });

  it("says when the text has not been extracted yet", async () => {
    const { spy } = backend(() =>
      jsonResponse(pagesOf([], { extracted: false, extractor: null, total: 0 })),
    );
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(/The text has not been extracted yet/)).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Outline" })).toBeNull();
  });

  it("says when a page has no text", async () => {
    const { spy } = backend(() => jsonResponse(pagesOf([{ number: 1, text: "", headings: [] }])));
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    const page = await screen.findByRole("region", { name: "Page 1" });
    expect(within(page).getByText("This page has no text.")).toBeInTheDocument();
  });

  it("shows a loader while the text loads", async () => {
    const { spy } = backend(() => new Promise<Response>(() => undefined));
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    const section = await textSection();
    expect(within(section).getByLabelText("Loading the text")).toBeInTheDocument();
  });

  it("says when the stored text is damaged, and tries again on request", async () => {
    let fail = true;
    const { spy } = backend(() =>
      fail
        ? jsonResponse({ detail: "the extracted text of this document is damaged" }, 500)
        : jsonResponse(pagesOf([{ number: 1, text: "repaired", headings: [] }])),
    );
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(PAGES_ERROR_TEXT.unavailable)).toBeInTheDocument();
    expect(screen.queryByText(/the extracted text of this document is damaged$/)).toBeNull();

    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("repaired")).toBeInTheDocument();
  });

  it("says when the backend cannot be reached", async () => {
    const { spy } = backend(() => Promise.reject(new TypeError("offline")));
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(PAGES_ERROR_TEXT.unreachable)).toBeInTheDocument();
  });

  it("says when the document is gone, without offering to try again", async () => {
    const { spy } = backend(() => jsonResponse({ detail: "no such document" }, 404));
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });
    const section = await textSection();
    expect(await within(section).findByText(PAGES_ERROR_TEXT["not-found"])).toBeInTheDocument();
    expect(within(section).queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("brings the token notice back when the text is refused, and loads it with the new token", async () => {
    const newToken = "n3w-token-after-restart_".repeat(2);
    const { spy, accepted } = backend(() =>
      jsonResponse(pagesOf([{ number: 1, text: "after the restart", headings: [] }])),
    );
    accepted.next = newToken;
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });

    expect(await screen.findByText("The access token was not accepted")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Access token"), newToken);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByText("after the restart")).toBeInTheDocument();
  });

  it("pages forward and back through the windows the backend gives", async () => {
    const windows: Record<string, DocumentPages> = {
      "0": pagesOf(
        [
          { number: 1, text: "one", headings: [] },
          { number: 2, text: "two", headings: [] },
        ],
        {
          total: 5,
          next_offset: 2,
        },
      ),
      // A shorter window, as when its pages are long: "Previous" must return to offset 2, not 0.
      "2": pagesOf([{ number: 3, text: "three", headings: [] }], {
        total: 5,
        offset: 2,
        next_offset: 3,
      }),
      "3": pagesOf(
        [
          { number: 4, text: "four", headings: [] },
          { number: 5, text: "five", headings: [] },
        ],
        { total: 5, offset: 3, next_offset: null },
      ),
    };
    const { spy } = backend((url) => {
      const answer = windows[url.searchParams.get("offset") ?? ""];
      return answer ? jsonResponse(answer) : jsonResponse({ detail: "no" }, 404);
    });
    await renderApp({ route: ROUTE, fetch: spy, token: TEST_TOKEN });

    expect(await screen.findByText("Pages 1 to 2 of 5")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Previous pages" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Next pages" }));
    expect(await screen.findByText("Pages 3 to 3 of 5")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next pages" }));
    expect(await screen.findByText("Pages 4 to 5 of 5")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Next pages" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Previous pages" }));
    expect(await screen.findByText("Pages 3 to 3 of 5")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Previous pages" }));
    expect(await screen.findByText("Pages 1 to 2 of 5")).toBeInTheDocument();

    await waitFor(() => {
      expect(pageRequests(spy).map((query) => query.get("offset"))).toEqual([
        "0",
        "2",
        "3",
        "2",
        "0",
      ]);
    });
  });
});

describe("splitting a page at its headings", () => {
  const page = (text: string, offsets: number[]): DocumentPage => ({
    number: 1,
    text,
    headings: offsets.map((offset, position) => ({
      level: 1,
      text: `h${String(position)}`,
      offset,
    })),
  });
  const parts = (text: string, offsets: number[]) =>
    splitAtHeadings(page(text, offsets)).map((segment) => [
      segment.text,
      segment.heading?.text ?? null,
    ]);

  it("keeps every character, in order", () => {
    expect(parts("intro\n# A\nbody\n# B\n", [6, 15])).toEqual([
      ["intro\n", null],
      ["# A\nbody\n", "h0"],
      ["# B\n", "h1"],
    ]);
    expect(parts("no headings", [])).toEqual([["no headings", null]]);
    expect(parts("", [])).toEqual([["", null]]);
  });

  it("counts offsets in code points, not UTF-16 units", () => {
    const astral = String.fromCodePoint(0x1f4c4);
    // "📄\n# A": the heading is at character 2, which is UTF-16 index 3.
    expect(parts(`${astral}\n# A`, [2])).toEqual([
      [`${astral}\n`, null],
      ["# A", "h0"],
    ]);
  });

  it("leaves out a heading outside the text or out of order", () => {
    expect(parts("abc", [5])).toEqual([["abc", null]]);
    expect(parts("abc", [3])).toEqual([["abc", null]]);
    expect(parts("abcdef", [3, 1])).toEqual([
      ["abc", null],
      ["def", "h0"],
    ]);
    expect(parts("abcdef", [2, 2])).toEqual([
      ["ab", null],
      ["cdef", "h0"],
    ]);
    expect(parts("abc", [-1, 1.5])).toEqual([["abc", null]]);
  });
});
