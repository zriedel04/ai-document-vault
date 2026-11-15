import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type LabelNode, NO_LABEL_TEXT, isLabelId, labelText } from "../api/labels";
import { type FetchFn, TEST_TOKEN, healthyBackend, jsonResponse, renderApp } from "../test/render";

const SCHOOL = "01J9Z8Y7X6W5V4T3S2R1Q0P9M1";
const COURSE = "01J9Z8Y7X6W5V4T3S2R1Q0P9M2";
const HOSTILE_ID = "01J9Z8Y7X6W5V4T3S2R1Q0P9M3";
const TAG = "01J9Z8Y7X6W5V4T3S2R1Q0P9M4";
// Label names are the owner's text: markup, SQL, a path and an encoded slash are shown as written.
const MARKUP = '<img src=x onerror="alert(1)">';
const SQL = "'); DROP TABLE labels; --";
const PATHLIKE = "../../%2F..%2Fetc?x=1#frag";

function label(overrides: Partial<LabelNode>): LabelNode {
  return {
    id: SCHOOL,
    kind: "folder",
    name: "School",
    parent_id: null,
    path: ["School"],
    documents: 3,
    ...overrides,
  };
}

const TREE: LabelNode[] = [
  label({}),
  label({
    id: COURSE,
    name: "CSE485",
    parent_id: SCHOOL,
    path: ["School", "CSE485"],
    documents: 2,
  }),
  label({
    id: HOSTILE_ID,
    name: MARKUP,
    parent_id: COURSE,
    path: ["School", "CSE485", MARKUP],
    documents: 1,
  }),
  label({ id: TAG, kind: "tag", name: SQL, path: [SQL, PATHLIKE], parent_id: null, documents: 0 }),
];

interface Options {
  labels?: () => Response;
  documents?: (url: URL) => Response;
  search?: (body: Record<string, unknown>) => Response;
}

const EMPTY_LIST = (url: URL) =>
  jsonResponse({
    items: [],
    total: 0,
    limit: Number(url.searchParams.get("limit")),
    offset: Number(url.searchParams.get("offset")),
  });

/** The backend as the page sees it, recording every address asked for and every search body. */
function backend(options: Options = {}) {
  const urls: URL[] = [];
  const bodies: Record<string, unknown>[] = [];
  const spy = vi.fn<FetchFn>(async (input) => {
    const request = input as Request;
    const url = new URL(request.url);
    urls.push(url);
    const signedIn = request.headers.get("authorization") === `Bearer ${TEST_TOKEN}`;
    if (url.pathname === "/api/labels") {
      if (!signedIn) return jsonResponse({ detail: "request refused" }, 401);
      return (options.labels ?? (() => jsonResponse({ items: TREE })))();
    }
    if (url.pathname === "/api/documents") return (options.documents ?? EMPTY_LIST)(url);
    if (url.pathname === "/api/search") {
      const body = (await request.json()) as Record<string, unknown>;
      bodies.push(body);
      return (
        options.search ?? (() => jsonResponse({ items: [], total: 0, limit: 20, offset: 0 }))
      )(body);
    }
    return healthyBackend(input);
  });
  const listParams = () =>
    urls.filter((url) => url.pathname === "/api/documents").map((url) => url.searchParams);
  return { spy, urls, bodies, listParams };
}

function labelSelect() {
  return screen.findByRole("combobox", { name: "Label" });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("label filter helpers", () => {
  it.each([
    [SCHOOL, true],
    ["01j9z8y7x6w5v4t3s2r1q0p9m1", false],
    [`${SCHOOL}\n`, false],
    [`${SCHOOL} `, false],
    [`${SCHOOL}X`, false],
    ["81J9Z8Y7X6W5V4T3S2R1Q0P9M1", false],
    ["01J9Z8Y7X6W5V4T3S2R1Q0P9MU", false],
    ["' OR 1=1 --", false],
    [123, false],
    [null, false],
  ])("checks the shape of a label id %j", (value, expected) => {
    expect(isLabelId(value)).toBe(expected);
  });

  it("shows a label by its whole path, with its count when asked", () => {
    expect(labelText({ path: ["School", "CSE485"], documents: 2 }, true)).toBe(
      "School › CSE485 (2)",
    );
    expect(labelText({ path: ["School"], documents: 2 }, false)).toBe("School");
  });
});

describe("label filter on the document list", () => {
  it("lists the labels as text and filters by the chosen label's id", async () => {
    const { spy, listParams, urls } = backend();
    const { router, container } = await renderApp({
      route: "/documents?page=3",
      fetch: spy,
      token: TEST_TOKEN,
    });
    const select = await labelSelect();
    await waitFor(() => {
      expect(select).toBeEnabled();
    });
    const folders = within(select).getByRole("group", { name: "Folders" });
    expect(
      within(folders)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["School (3)", "School › CSE485 (2)", `School › CSE485 › ${MARKUP} (1)`]);
    const tags = within(select).getByRole("group", { name: "Tags" });
    expect(within(tags).getByRole("option").textContent).toBe(`${SQL} › ${PATHLIKE} (0)`);
    expect(container.querySelector("img")).toBeNull();

    // By its value: user-event compares an option's markup, in which `<` is escaped.
    await userEvent.selectOptions(select, HOSTILE_ID);
    await waitFor(() => {
      expect(listParams().at(-1)?.get("label")).toBe(HOSTILE_ID);
    });
    expect(listParams().at(-1)?.get("offset")).toBe("0");
    expect(router.state.location.search).toMatchObject({ label: HOSTILE_ID, page: 1 });
    // Only the id reaches an address: the page's and every request's.
    for (const address of [router.history.location.href, ...urls.map((url) => url.href)]) {
      expect(address).not.toContain("img");
      expect(address).not.toContain("DROP");
      expect(decodeURIComponent(address)).not.toContain(MARKUP);
    }
    expect(await screen.findByText("No document in the library has this label.")).toBeVisible();

    await userEvent.selectOptions(select, "All documents");
    await waitFor(() => {
      expect(listParams().at(-1)?.has("label")).toBe(false);
    });
    expect(router.history.location.href).not.toContain("label");
  });

  it("leaves out the counts in the trash, which count the library", async () => {
    const { spy } = backend();
    await renderApp({ route: "/documents?trash=true", fetch: spy, token: TEST_TOKEN });
    const select = await labelSelect();
    await waitFor(() => {
      expect(within(select).getByRole("option", { name: "School" })).toBeInTheDocument();
    });
  });

  it("never sends an id the address holds that is not a label id", async () => {
    const { spy, listParams } = backend();
    await renderApp({
      route: `/documents?label=${encodeURIComponent('"\' OR 1=1 --"')}`,
      fetch: spy,
      token: TEST_TOKEN,
    });
    await screen.findByText("No documents yet.");
    expect(listParams().every((params) => !params.has("label"))).toBe(true);
  });

  it("says when the chosen label is gone and offers every document", async () => {
    const { spy, listParams } = backend({
      documents: (url) =>
        url.searchParams.has("label")
          ? jsonResponse({ detail: "no such label" }, 404)
          : EMPTY_LIST(url),
    });
    const gone = "01J9Z8Y7X6W5V4T3S2R1Q0P9ZZ";
    await renderApp({ route: `/documents?label=${gone}`, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText(NO_LABEL_TEXT)).toBeVisible();
    expect(
      within(await labelSelect()).getByRole("option", { name: "Label not found" }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show every document" }));
    expect(await screen.findByText("No documents yet.")).toBeVisible();
    expect(listParams().at(-1)?.has("label")).toBe(false);
  });

  it("keeps the list working when the labels cannot be loaded", async () => {
    const { spy } = backend({ labels: () => jsonResponse({ detail: "x" }, 500) });
    await renderApp({ route: "/documents", fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText("Labels could not be loaded.")).toBeVisible();
    expect(await labelSelect()).toBeDisabled();
    expect(screen.getByText("No documents yet.")).toBeVisible();
  });

  it("asks for no labels without a token", async () => {
    const { spy, urls } = backend();
    await renderApp({ route: "/documents", fetch: spy });
    await screen.findByText(/has no access to the vault yet/);
    expect(urls.some((url) => url.pathname === "/api/labels")).toBe(false);
  });
});

describe("label filter on the search page", () => {
  async function searchFor(words: string) {
    await userEvent.type(screen.getByRole("searchbox", { name: "Search for" }), words);
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
  }

  it("sends the label in the body and keeps it out of the address", async () => {
    const { spy, bodies, urls } = backend();
    const { router } = await renderApp({ route: "/search", fetch: spy, token: TEST_TOKEN });
    const select = await labelSelect();
    await waitFor(() => {
      expect(select).toBeEnabled();
    });
    await searchFor("letter");
    await waitFor(() => {
      expect(bodies).toHaveLength(1);
    });
    expect(bodies[0]).not.toHaveProperty("label");

    await userEvent.selectOptions(select, TAG);
    await waitFor(() => {
      expect(bodies.at(-1)).toEqual({
        query: "letter",
        limit: 20,
        offset: 0,
        types: [],
        label: TAG,
      });
    });
    expect(router.history.location.href).toBe("/search");
    for (const url of urls) expect(url.href).not.toContain(TAG);
  });

  it("says when the chosen label is gone", async () => {
    const { spy } = backend({
      search: (body) =>
        "label" in body
          ? jsonResponse({ detail: "no such label" }, 404)
          : jsonResponse({ items: [], total: 0, limit: 20, offset: 0 }),
    });
    await renderApp({ route: "/search", fetch: spy, token: TEST_TOKEN });
    const select = await labelSelect();
    await waitFor(() => {
      expect(select).toBeEnabled();
    });
    await userEvent.selectOptions(select, "School (3)");
    await searchFor("letter");
    expect(await screen.findByText(NO_LABEL_TEXT)).toBeVisible();
  });
});
