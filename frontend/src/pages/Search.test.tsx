import { readFileSync } from "node:fs";

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ERROR_TEXT } from "../api/documents";
import {
  MAX_SEARCH_OFFSET,
  MAX_SEARCH_PAGE,
  SEARCH_ERROR_TEXT,
  SEARCH_PAGE_SIZE,
  SEARCH_TYPES,
  type SearchHit,
  type SearchResults,
} from "../api/search";
import { type FetchFn, TEST_TOKEN, healthyBackend, jsonResponse, renderApp } from "../test/render";

const ID = "01J9Z8Y7X6W5V4T3S2R1Q0P9N8";
// A query as private as a file name: it must not reach the address, the history or the console.
const QUERY = '"Jane Doe" diagnos*';

function hit(overrides: Partial<SearchHit> = {}): SearchHit {
  return {
    document_id: ID,
    name: "Clinic letter.pdf",
    extension: "pdf",
    chunk_id: 7,
    page: 3,
    char_start: 10,
    char_end: 420,
    section_path: ["Findings"],
    snippet: [
      { text: "…the letter for ", match: false },
      { text: "Jane", match: true },
      { text: " ", match: false },
      { text: "Doe", match: true },
      { text: " gives the ", match: false },
      { text: "diagnosis", match: true },
      { text: "…", match: false },
    ],
    matches: 4,
    name_highlight: [{ text: "Clinic letter.pdf", match: false }],
    section_highlight: [[{ text: "Findings", match: false }]],
    ...overrides,
  };
}

function results(items: SearchHit[], total = items.length, offset = 0): SearchResults {
  return { items, total, limit: SEARCH_PAGE_SIZE, offset };
}

interface Sent {
  query: string;
  limit: number;
  offset: number;
  types: string[];
}

type Answer = (body: Sent, index: number) => Response | Promise<Response>;

/** The real backend's open paths, plus `answer` for each search (with the token it takes). */
function backend(answer: Answer) {
  const sent: Sent[] = [];
  const urls: string[] = [];
  const spy = vi.fn<FetchFn>(async (input) => {
    const request = input as Request;
    const url = new URL(request.url);
    if (url.pathname === "/api/search") {
      urls.push(request.url);
      if (request.headers.get("authorization") !== `Bearer ${TEST_TOKEN}`) {
        return jsonResponse({ detail: "request refused" }, 401);
      }
      expect(request.method).toBe("POST");
      const body = (await request.json()) as Sent;
      sent.push(body);
      return answer(body, sent.length - 1);
    }
    return healthyBackend(input);
  });
  return { spy, sent, urls };
}

async function openSearch(spy: FetchFn, token: string | undefined = TEST_TOKEN) {
  const rendered = await renderApp({ route: "/search", fetch: spy, token });
  await screen.findByRole("heading", { level: 2, name: "Search" });
  return rendered;
}

async function search(words: string) {
  const field = screen.getByRole("searchbox", { name: "Search for" });
  await userEvent.clear(field);
  // userEvent.type reads braces and brackets as keys; the queries here have none.
  if (words.length > 0) await userEvent.type(field, words);
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
}

function resultList() {
  return screen.findByRole("list", { name: "Results" });
}

describe("search page", () => {
  it("shows each matching document with its marked snippet, section and page, and links to it", async () => {
    const { spy, sent } = backend(() => jsonResponse(results([hit()])));
    await openSearch(spy);
    await search(QUERY);

    const list = await resultList();
    const link = within(list).getByRole("link", { name: "Clinic letter.pdf" });
    expect(link).toHaveAttribute("href", `/documents/${ID}`);
    const marks = [...list.querySelectorAll("mark")].map((mark) => mark.textContent);
    expect(marks).toEqual(["Jane", "Doe", "diagnosis"]);
    expect(
      within(list)
        .getByText(/the letter for/)
        .closest("p")?.textContent,
    ).toBe("…the letter for Jane Doe gives the diagnosis…");
    expect(
      within(list)
        .getByText(/Page 3/)
        .closest("p")?.textContent,
    ).toBe("Page 3 · Findings");
    expect(within(list).getByText("4 matching passages")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Showing 1 to 1 of 1 matching documents");
    expect(sent).toEqual([{ query: QUERY, limit: SEARCH_PAGE_SIZE, offset: 0, types: [] }]);
  });

  it("keeps the query out of the address, the history and the request URL", async () => {
    const { spy, urls } = backend(() => jsonResponse(results([hit()])));
    const { router } = await openSearch(spy);
    const before = router.history.length;
    await search(QUERY);
    await resultList();
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await resultList();

    expect(router.history.location.href).toBe("/search");
    expect(router.history.length).toBe(before);
    for (const url of urls) expect(url).toBe(`${globalThis.location.origin}/api/search`);
    const field = screen.getByRole("searchbox", { name: "Search for" });
    expect(field).not.toHaveAttribute("name");
    expect(field).toHaveAttribute("autocomplete", "off");
    // The page handles the submission itself: the browser never sends the form anywhere.
    const form = screen.getByRole("search", { name: "Search the vault" });
    expect(form).not.toHaveAttribute("action");
    expect(fireEvent.submit(form)).toBe(false);
    // Leaving the page forgets the query.
    await userEvent.click(screen.getByRole("link", { name: "Documents" }));
    await userEvent.click(screen.getByRole("link", { name: "Search" }));
    expect(screen.getByRole("searchbox", { name: "Search for" })).toHaveValue("");
    expect(screen.queryByRole("list", { name: "Results" })).toBeNull();
  });

  it("marks matches in the name and the section titles too", async () => {
    const { spy } = backend(() =>
      jsonResponse(
        results([
          hit({
            extension: "md",
            name: "Doe notes.md",
            name_highlight: [
              { text: "Doe", match: true },
              { text: " notes.md", match: false },
            ],
            section_path: ["Plan", "Doe visit"],
            section_highlight: [
              [{ text: "Plan", match: false }],
              [
                { text: "Doe", match: true },
                { text: " visit", match: false },
              ],
            ],
            snippet: [{ text: "First lines of the chunk", match: false }],
            matches: 1,
          }),
        ]),
      ),
    );
    await openSearch(spy);
    await search("Doe");

    const list = await resultList();
    // The accessible name in jsdom drops the space at a mark; browsers keep it, so read the text.
    const link = within(list).getByRole("link");
    expect(link.textContent).toBe("Doe notes.md");
    expect(link.querySelector("mark")?.textContent).toBe("Doe");
    // A Markdown file has one page of text, so no page number is shown.
    expect(within(list).getByText("Plan").closest("p")?.textContent).toBe("Plan › Doe visit");
    expect(within(list).queryByText(/Page/)).toBeNull();
    expect(within(list).queryByText(/matching passages/)).toBeNull();
    expect([...list.querySelectorAll("mark")].map((mark) => mark.textContent)).toEqual([
      "Doe",
      "Doe",
    ]);
  });

  it("names a document with no import name by its id and type", async () => {
    const { spy } = backend(() =>
      jsonResponse(results([hit({ name: null, name_highlight: [], section_path: [] })])),
    );
    await openSearch(spy);
    await search("letter");
    const list = await resultList();
    expect(within(list).getByRole("link", { name: `${ID}.pdf` })).toBeInTheDocument();
  });

  it("shows markup in snippets, names and sections literally and never as HTML", async () => {
    const markup = '<img src=x onerror="alert(1)"><script>alert(2)</script><b>bold</b>';
    const { spy } = backend(() =>
      jsonResponse(
        results([
          hit({
            name: `${markup}.txt`,
            extension: "txt",
            name_highlight: [{ text: `${markup}.txt`, match: false }],
            section_path: [markup],
            section_highlight: [[{ text: markup, match: true }]],
            snippet: [
              { text: markup, match: false },
              { text: "<i>marked</i>", match: true },
            ],
          }),
        ]),
      ),
    );
    await openSearch(spy);
    await search("bold");

    const list = await resultList();
    expect(list.querySelector("img, script, b, i")).toBeNull();
    expect(within(list).getByRole("link").textContent).toBe(`${markup}.txt`);
    expect(list.textContent).toContain(`${markup}<i>marked</i>`);
  });

  it("filters by type and starts again from the first page", async () => {
    const { spy, sent } = backend((body) => jsonResponse(results([hit()], 45, body.offset)));
    await openSearch(spy);
    await search("letter");
    await resultList();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => {
      expect(sent.at(-1)?.offset).toBe(SEARCH_PAGE_SIZE);
    });

    await userEvent.click(screen.getByRole("checkbox", { name: "Markdown" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "PDF" }));
    await waitFor(() => {
      expect(sent.at(-1)).toEqual({
        query: "letter",
        limit: SEARCH_PAGE_SIZE,
        offset: 0,
        types: ["pdf", "md"],
      });
    });
    await userEvent.click(screen.getByRole("checkbox", { name: "PDF" }));
    await waitFor(() => {
      expect(sent.at(-1)?.types).toEqual(["md"]);
    });
  });

  it("pages through the results with the backend's offsets", async () => {
    const { spy, sent } = backend((body) =>
      jsonResponse(results([hit(), hit({ document_id: `${ID}X` })], 45, body.offset)),
    );
    await openSearch(spy);
    await search("letter");
    await resultList();
    expect(screen.getByRole("status")).toHaveTextContent("Showing 1 to 2 of 45 matching documents");
    await userEvent.click(screen.getByRole("button", { name: "3" }));
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Showing 41 to 42 of 45 matching documents",
      );
    });
    expect(sent.map((body) => body.offset)).toEqual([0, 2 * SEARCH_PAGE_SIZE]);
    // A new search starts at the first page.
    await search("lease");
    await waitFor(() => {
      expect(sent.at(-1)).toMatchObject({ query: "lease", offset: 0 });
    });
  });

  it("moves to the last page when the page asked for is past the end", async () => {
    // The first answer has three pages; by the time page 3 is asked for, only one remains.
    const { spy, sent } = backend((body, index) =>
      index === 1
        ? jsonResponse(results([], 5, body.offset))
        : jsonResponse(results([hit()], index === 0 ? 45 : 5, body.offset)),
    );
    await openSearch(spy);
    await search("letter");
    await resultList();
    await userEvent.click(screen.getByRole("button", { name: "3" }));
    await waitFor(() => {
      expect(sent.map((body) => body.offset)).toEqual([0, 2 * SEARCH_PAGE_SIZE, 0]);
    });
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Showing 1 to 1 of 5 matching documents",
    );
  });

  it("says when nothing matches", async () => {
    const { spy } = backend(() => jsonResponse(results([])));
    await openSearch(spy);
    await search("-");
    expect(await screen.findByRole("status")).toHaveTextContent("No documents match.");
  });

  it("sends nothing for an empty query", async () => {
    const { spy, sent } = backend(() => jsonResponse(results([hit()])));
    await openSearch(spy);
    await search("   ");
    await search("");
    expect(sent).toEqual([]);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("asks again when Search is pressed again with the same words", async () => {
    const { spy, sent } = backend(() => jsonResponse(results([hit()])));
    await openSearch(spy);
    await search("letter");
    await resultList();
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(sent).toHaveLength(2);
    });
  });

  it("explains a query the backend refuses, without its message and without a retry", async () => {
    const { spy } = backend(() =>
      jsonResponse({ detail: "The query has more than 16 terms." }, 422),
    );
    await openSearch(spy);
    await search("a b c");
    expect(await screen.findByText(SEARCH_ERROR_TEXT.refused)).toBeInTheDocument();
    expect(screen.queryByText(/more than 16 terms/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it.each([
    [500, SEARCH_ERROR_TEXT.unreachable],
    [503, SEARCH_ERROR_TEXT["no-vault"]],
    [404, SEARCH_ERROR_TEXT["not-found"]],
  ])("shows a fixed text for a %d and tries again on request", async (status, message) => {
    const { spy, sent } = backend((_body, index) =>
      index === 0
        ? jsonResponse({ detail: "server words" }, status)
        : jsonResponse(results([hit()])),
    );
    await openSearch(spy);
    await search("letter");
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByText("server words")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await resultList();
    expect(sent).toHaveLength(2);
    expect(sent[1]).toEqual(sent[0]);
  });

  it("shows a lost connection as unreachable", async () => {
    const { spy } = backend(() => Promise.reject(new TypeError("Failed to fetch")));
    await openSearch(spy);
    await search("letter");
    expect(await screen.findByText(SEARCH_ERROR_TEXT.unreachable)).toBeInTheDocument();
  });

  it("asks the search nothing until the browser has an accepted token", async () => {
    const { spy, urls } = backend(() => jsonResponse(results([hit()])));
    await openSearch(spy, "a-token-the-server-no-longer-takes");
    await search("letter");
    expect(await screen.findAllByText(ERROR_TEXT["needs-token"])).not.toHaveLength(0);
    // Without an accepted token the page asks the search nothing.
    expect(urls).toEqual([]);
  });

  it("asks the search again once its token is refused, and shows the notice", async () => {
    let accepted = true;
    const spy = vi.fn<FetchFn>((input) => {
      const request = input as Request;
      const { pathname } = new URL(request.url);
      if (pathname === "/api/search") {
        accepted = false;
        return Promise.resolve(jsonResponse({ detail: "request refused" }, 401));
      }
      if (pathname === "/api/session" && !accepted) {
        return Promise.resolve(jsonResponse({ detail: "request refused" }, 401));
      }
      return healthyBackend(input);
    });
    await openSearch(spy);
    await search("letter");
    expect(await screen.findByLabelText("Access token")).toBeInTheDocument();
    const sessions = spy.mock.calls.filter(
      ([input]) => new URL((input as Request).url).pathname === "/api/session",
    );
    expect(sessions.length).toBeGreaterThan(1);
  });

  it("shows only the answer to the latest search", async () => {
    let release: (response: Response) => void = () => undefined;
    const first = new Promise<Response>((resolve) => {
      release = resolve;
    });
    const { spy } = backend((_body, index) =>
      index === 0
        ? first
        : jsonResponse(results([hit({ name: "Second.pdf", name_highlight: [] })])),
    );
    await openSearch(spy);
    await search("first");
    await search("second");
    const list = await resultList();
    expect(within(list).getByRole("link", { name: "Second.pdf" })).toBeInTheDocument();
    release(jsonResponse(results([hit({ name: "First.pdf", name_highlight: [] })])));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByRole("link", { name: "First.pdf" })).toBeNull();
  });

  it("never writes the query or a result to the console, whatever the outcome", async () => {
    const calls = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method),
    );
    const answers: (() => Response | Promise<Response>)[] = [
      () => jsonResponse(results([hit()])),
      () => jsonResponse({ detail: "x" }, 422),
      () => jsonResponse({ detail: "x" }, 500),
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => new Response("not json", { status: 200 }),
    ];
    const { spy, sent } = backend((_body, index) =>
      (answers[index] ?? (() => jsonResponse(results([]))))(),
    );
    await openSearch(spy);
    for (const [index] of answers.entries()) {
      await search(`Jane Doe ${String(index)}`);
      await waitFor(() => {
        expect(screen.queryByLabelText("Searching")).toBeNull();
      });
    }
    expect(sent).toHaveLength(answers.length);
    for (const call of calls) {
      expect(JSON.stringify(call.mock.calls)).not.toMatch(/Jane|Doe|diagnosis|Clinic/);
    }
  });
});

describe("search API", () => {
  it("knows every type the backend filters by, and no other", () => {
    const schema = JSON.parse(readFileSync("openapi.json", "utf8")) as {
      components: {
        schemas: { SearchRequest: { properties: { types: { items: { enum: string[] } } } } };
      };
    };
    const types = schema.components.schemas.SearchRequest.properties.types.items.enum;
    expect([...SEARCH_TYPES].sort()).toEqual([...types].sort());
  });

  it("pages only as far as the backend's page size and largest offset allow", () => {
    const schema = JSON.parse(readFileSync("openapi.json", "utf8")) as {
      components: {
        schemas: {
          SearchRequest: {
            properties: { limit: { maximum: number }; offset: { maximum: number } };
          };
        };
      };
    };
    const { limit, offset } = schema.components.schemas.SearchRequest.properties;
    expect(SEARCH_PAGE_SIZE).toBeLessThanOrEqual(limit.maximum);
    expect(offset.maximum).toBe(MAX_SEARCH_OFFSET);
    expect((MAX_SEARCH_PAGE - 1) * SEARCH_PAGE_SIZE).toBeLessThanOrEqual(MAX_SEARCH_OFFSET);
    expect(MAX_SEARCH_PAGE * SEARCH_PAGE_SIZE).toBeGreaterThan(MAX_SEARCH_OFFSET);
  });
});
