import { readFileSync } from "node:fs";

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DocumentDetail } from "../api/documents";
import {
  LABEL_CHANGE_ERROR_TEXT,
  type LabelNode,
  OWNER_KINDS,
  ownerAssignments,
} from "../api/labels";
import { type FetchFn, TEST_TOKEN, healthyBackend, jsonResponse, renderApp } from "../test/render";

const ID = "01J9Z8Y7X6W5V4T3S2R1Q0P9N8";
const SCHOOL = "01J9Z8Y7X6W5V4T3S2R1Q0P9M1";
const COURSE = "01J9Z8Y7X6W5V4T3S2R1Q0P9M2";
const URGENT = "01J9Z8Y7X6W5V4T3S2R1Q0P9M3";
const FOLDER = "01J9Z8Y7X6W5V4T3S2R1Q0P9M4";
const NEW = "01J9Z8Y7X6W5V4T3S2R1Q0P9M5";
// Names are the owner's text: markup is shown as written, never made into an element.
const MARKUP = '<img src=x onerror="alert(1)">';
const SERVER_TEXT = "Patient-Diagnosis-Canary";

const NOT_EXTRACTED = {
  version_id: "01J9Z8Y7X6W5V4T3S2R1Q0P9N9",
  extracted: false,
  extractor: null,
  total: 0,
  offset: 0,
  limit: 20,
  pages: [],
  next_offset: null,
};

function node(id: string, kind: LabelNode["kind"], path: string[], parent: string | null = null) {
  return {
    id,
    kind,
    name: path[path.length - 1] ?? "",
    parent_id: parent,
    path,
    documents: 0,
  } satisfies LabelNode;
}

function detail(labels: DocumentDetail["labels"]): DocumentDetail {
  return {
    id: ID,
    name: "Notes.txt",
    extension: "txt",
    mime: "text/plain",
    size: 12,
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
    labels,
    file_properties: { title: null, author: null, created: null, modified: null },
    title: null,
  };
}

function carried(label: LabelNode, source: "user" | "ai" | "import_path" = "user") {
  return {
    id: label.id,
    kind: label.kind,
    name: label.name,
    path: label.path,
    source,
    status: source === "ai" ? ("suggested" as const) : ("accepted" as const),
  };
}

interface Options {
  /** Answers a change instead of the stand-in backend, when it returns a response. */
  answer?: (request: Request, url: URL) => Response | undefined;
}

/**
 * A backend with a label tree and one document whose owner's assignments change as the page asks.
 * It records each change request (method and path) and each body sent to make a label.
 */
function backend(tree: LabelNode[], assigned: DocumentDetail["labels"], options: Options = {}) {
  const labels = [...tree];
  let current = [...assigned];
  const changes: string[] = [];
  const created: unknown[] = [];
  const spy = vi.fn<FetchFn>(async (input) => {
    const request = input as Request;
    const url = new URL(request.url);
    const signedIn = request.headers.get("authorization") === `Bearer ${TEST_TOKEN}`;
    if (!url.pathname.startsWith("/api/documents") && !url.pathname.startsWith("/api/labels")) {
      return healthyBackend(input);
    }
    if (!signedIn) return jsonResponse({ detail: "request refused" }, 401);
    if (request.method !== "GET") changes.push(`${request.method} ${url.pathname}`);
    const answer = options.answer?.(request, url);
    if (answer !== undefined) return answer;
    if (url.pathname === "/api/labels" && request.method === "GET") {
      return jsonResponse({ items: labels });
    }
    if (url.pathname === "/api/labels" && request.method === "POST") {
      const body = (await request.json()) as { kind: LabelNode["kind"]; name: string };
      created.push(body);
      const made = node(NEW, body.kind, [body.name]);
      labels.push(made);
      return jsonResponse(made, 201);
    }
    if (url.pathname === `/api/documents/${ID}`) return jsonResponse(detail(current));
    if (url.pathname === `/api/documents/${ID}/pages`) return jsonResponse(NOT_EXTRACTED);
    const match = /^\/api\/documents\/[^/]+\/labels\/([^/]+)$/.exec(url.pathname);
    if (match) {
      const label = labels.find((each) => each.id === match[1]);
      if (label === undefined) return jsonResponse({ detail: SERVER_TEXT }, 404);
      current = current.filter((each) => !(each.id === label.id && each.source === "user"));
      if (request.method === "PUT") current.push(carried(label));
      return jsonResponse(detail(current));
    }
    return jsonResponse({ detail: "not found" }, 404);
  });
  return { spy, changes, created };
}

const TREE = [
  node(SCHOOL, "collection", ["School"]),
  node(COURSE, "collection", ["School", MARKUP], SCHOOL),
  node(URGENT, "tag", ["Urgent"]),
  node(FOLDER, "folder", ["Imported"]),
];

async function openDocument(fetch: FetchFn) {
  await renderApp({ route: `/documents/${ID}`, fetch, token: TEST_TOKEN });
  await screen.findByRole("heading", { level: 3, name: "Collections and tags" });
  // The add list fills once the labels have loaded.
  await waitFor(() => {
    expect(screen.getByLabelText("Add to")).toBeEnabled();
  });
}

function section() {
  const heading = screen.getByRole("heading", { level: 3, name: "Collections and tags" });
  const parent = heading.parentElement;
  if (parent === null) throw new Error("no section");
  return within(parent);
}

describe("the document's collections and tags", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists the owner's kinds as the backend does", () => {
    const schema = JSON.parse(readFileSync("openapi.json", "utf8")) as {
      components: { schemas: { NewLabel: { properties: { kind: { enum: string[] } } } } };
    };
    expect([...OWNER_KINDS].sort()).toEqual(
      [...schema.components.schemas.NewLabel.properties.kind.enum].sort(),
    );
  });

  it("shows only the owner's own assignments, names as text", async () => {
    const school = TREE[0];
    const course = TREE[1];
    const urgent = TREE[2];
    const folder = TREE[3];
    if (!school || !course || !urgent || !folder) throw new Error("tree");
    const labels = [carried(course), carried(urgent, "ai"), carried(folder, "import_path")];
    expect(ownerAssignments(detail(labels)).map((label) => label.id)).toEqual([COURSE]);
    const { spy } = backend(TREE, labels);
    await openDocument(spy);
    expect(section().getByText(`School › ${MARKUP}`)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
    // The add list holds the owner's labels not yet given, never a folder.
    const options = within(screen.getByLabelText("Add to"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options).toEqual(["Choose a collection or tag", "School (Collection)", "Urgent (Tag)"]);
  });

  it("adds a label from the list and removes it again", async () => {
    const { spy, changes } = backend(TREE, []);
    await openDocument(spy);
    expect(section().getByText("This document is in no collection and has no tags.")).toBeVisible();
    await userEvent.selectOptions(screen.getByLabelText("Add to"), URGENT);
    await userEvent.click(section().getByRole("button", { name: "Add" }));
    const remove = await section().findByRole("button", { name: "Remove Urgent" });
    expect(changes).toEqual([`PUT /api/documents/${ID}/labels/${URGENT}`]);
    await userEvent.click(remove);
    await section().findByText("This document is in no collection and has no tags.");
    expect(changes).toEqual([
      `PUT /api/documents/${ID}/labels/${URGENT}`,
      `DELETE /api/documents/${ID}/labels/${URGENT}`,
    ]);
  });

  it("makes a new collection at the top and adds the document to it", async () => {
    const { spy, changes, created } = backend(TREE, []);
    await openDocument(spy);
    await userEvent.type(screen.getByLabelText("New collection"), "Taxes 2026");
    await userEvent.click(section().getByRole("button", { name: "Create and add" }));
    expect(await section().findByRole("button", { name: "Remove Taxes 2026" })).toBeVisible();
    expect(created).toEqual([{ kind: "collection", name: "Taxes 2026" }]);
    expect(changes).toEqual(["POST /api/labels", `PUT /api/documents/${ID}/labels/${NEW}`]);
    expect(screen.getByLabelText("New collection")).toHaveValue("");
  });

  it.each([
    [409, "name-taken"],
    [422, "bad-name"],
    [404, "not-found"],
  ] as const)("says why a new collection was refused (%i) in fixed text", async (status, error) => {
    const { spy, changes } = backend(TREE, [], {
      answer: (request, url) =>
        url.pathname === "/api/labels" && request.method === "POST"
          ? jsonResponse({ detail: SERVER_TEXT }, status)
          : undefined,
    });
    await openDocument(spy);
    await userEvent.type(screen.getByLabelText("New collection"), "School");
    await userEvent.click(section().getByRole("button", { name: "Create and add" }));
    expect(await screen.findByText(LABEL_CHANGE_ERROR_TEXT[error])).toBeVisible();
    expect(screen.queryByText(SERVER_TEXT)).toBeNull();
    expect(changes).toEqual(["POST /api/labels"]);
  });

  it("refuses a folder label in fixed text", async () => {
    const { spy } = backend(TREE, [], {
      answer: (request) =>
        request.method === "PUT" ? jsonResponse({ detail: SERVER_TEXT }, 409) : undefined,
    });
    await openDocument(spy);
    await userEvent.selectOptions(screen.getByLabelText("Add to"), SCHOOL);
    await userEvent.click(section().getByRole("button", { name: "Add" }));
    expect(await screen.findByText(LABEL_CHANGE_ERROR_TEXT["not-owners"])).toBeVisible();
    expect(screen.queryByText(SERVER_TEXT)).toBeNull();
  });

  it("shows the document as it is when an answer does not prove the change", async () => {
    let failed = false;
    const { spy } = backend(TREE, [], {
      answer: (request) => {
        if (request.method !== "PUT" || failed) return undefined;
        failed = true;
        return jsonResponse({ detail: SERVER_TEXT }, 500);
      },
    });
    await openDocument(spy);
    await userEvent.selectOptions(screen.getByLabelText("Add to"), URGENT);
    await userEvent.click(section().getByRole("button", { name: "Add" }));
    expect(await screen.findByText("Change not confirmed")).toBeVisible();
    expect(screen.queryByText(SERVER_TEXT)).toBeNull();
  });
});
