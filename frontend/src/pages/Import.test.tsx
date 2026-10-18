import { readFileSync } from "node:fs";

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ERROR_TEXT } from "../api/documents";
import {
  MAX_MTIME_MS,
  REFUSAL_TEXT,
  type Reason,
  SUPPORTED_EXTENSIONS,
  UNKNOWN_TEXT,
  mtimeHeader,
} from "../api/imports";
import { type FetchFn, TEST_TOKEN, healthyBackend, jsonResponse, renderApp } from "../test/render";

const BATCH = "01J9Z8Y7X6W5V4T3S2R1Q0P9NB";
const OTHER_BATCH = "01J9Z8Y7X6W5V4T3S2R1Q0P9NC";
const DOC = "01J9Z8Y7X6W5V4T3S2R1Q0P9N8";
const DOC2 = "01J9Z8Y7X6W5V4T3S2R1Q0P9N9";
const MTIME = 1_759_320_000_000;

// Synthetic names that look like the owner's private files: they may be shown, never logged.
const NAMES = ["Patient intake – Jane Doe.pdf", "Lease agreement.docx", "Tax return 2025.txt"];

function file(name: string, content = `synthetic bytes of ${name}`, lastModified = MTIME): File {
  return new File([content], name, { lastModified });
}

interface Sent {
  filename: string | null;
  batch: string | null;
  mtime: string | null;
  contentType: string | null;
  body: string;
}

type Answer = (sent: Sent, index: number) => Response | Promise<Response>;

/** The open paths, plus an upload route that answers with `answer` and records each request. */
function backend(answer: Answer) {
  const sent: Sent[] = [];
  let active = 0;
  let mostAtOnce = 0;
  const spy = vi.fn<FetchFn>(async (input) => {
    const request = input as Request;
    const url = new URL(request.url);
    if (url.pathname !== "/api/imports/upload") return healthyBackend(input);
    if (request.headers.get("authorization") !== `Bearer ${TEST_TOKEN}`) {
      return jsonResponse({ detail: "request refused" }, 401);
    }
    active += 1;
    mostAtOnce = Math.max(mostAtOnce, active);
    try {
      const record: Sent = {
        filename: request.headers.get("x-vault-filename"),
        batch: request.headers.get("x-vault-batch"),
        mtime: request.headers.get("x-vault-last-modified"),
        contentType: request.headers.get("content-type"),
        body: await request.text(),
      };
      sent.push(record);
      return await answer(record, sent.length - 1);
    } finally {
      active -= 1;
    }
  });
  return { spy, sent, mostAtOnce: () => mostAtOnce };
}

const imported = (document_id = DOC, batch_id = BATCH) =>
  jsonResponse({ outcome: "imported", batch_id, document_id, reason: null });

function dropZone() {
  return screen.getByRole("region", { name: "Drop files to import" });
}

function drop(files: File[], folders: ReadonlySet<File> = new Set()) {
  const items = files.map((each) => ({
    kind: "file",
    getAsFile: () => each,
    webkitGetAsEntry: () => ({ isDirectory: folders.has(each) }),
  }));
  fireEvent.drop(dropZone(), { dataTransfer: { files, items } });
}

function row(name: string) {
  const cell = screen.getByRole("cell", { name });
  const tr = cell.closest("tr");
  if (tr === null) throw new Error("no row");
  return within(tr);
}

async function openImport(spy: FetchFn) {
  await renderApp({ route: "/import", fetch: spy, token: TEST_TOKEN });
  return screen.findByRole("region", { name: "Drop files to import" });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("import page", () => {
  it("has a navigation entry", async () => {
    const { spy } = backend(() => imported());
    await renderApp({ fetch: spy, token: TEST_TOKEN });
    await userEvent.click(screen.getByRole("link", { name: "Import" }));
    expect(screen.getByRole("heading", { level: 2, name: "Import" })).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Drop files to import" })).toBeVisible();
  });

  it("offers no drop zone and sends nothing without a token", async () => {
    const { spy } = backend(() => imported());
    await renderApp({ route: "/import", fetch: spy });
    expect(screen.getByText(ERROR_TEXT["needs-token"])).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Drop files to import" })).toBeNull();
  });

  it("uploads a multi-file drop one at a time, as one batch, with each file's outcome", async () => {
    const { spy, sent, mostAtOnce } = backend((_sent, index) => {
      if (index === 0) return imported(DOC);
      if (index === 1) {
        return jsonResponse({ outcome: "duplicate", batch_id: BATCH, document_id: DOC2 });
      }
      return jsonResponse({ outcome: "refused", batch_id: BATCH, reason: "content_mismatch" });
    });
    await openImport(spy);
    drop(NAMES.map((name) => file(name)));

    expect(await screen.findByText("3 of 3 files finished")).toBeInTheDocument();
    expect(mostAtOnce()).toBe(1);
    // The first file starts the batch; the others join the one its answer named.
    expect(sent.map((each) => each.batch)).toEqual([null, BATCH, BATCH]);
    expect(sent.map((each) => each.filename)).toEqual(NAMES.map(encodeURIComponent));
    expect(sent.map((each) => each.mtime)).toEqual(NAMES.map(() => String(MTIME)));
    expect(sent.map((each) => each.contentType)).toEqual(
      NAMES.map(() => "application/octet-stream"),
    );
    expect(sent.map((each) => each.body)).toEqual(
      NAMES.map((name) => `synthetic bytes of ${name}`),
    );
    // Nothing that names the file goes in the address.
    for (const [input] of spy.mock.calls) {
      expect(
        new URL((input as Request).url).pathname + new URL((input as Request).url).search,
      ).not.toMatch(/Patient|Lease|Tax/);
    }

    const [first, second, third] = NAMES as [string, string, string];
    expect(row(first).getByText("Imported")).toBeInTheDocument();
    expect(row(first).getByRole("link", { name: "Open document" })).toHaveAttribute(
      "href",
      `/documents/${DOC}`,
    );
    expect(row(second).getByText("Already in the vault")).toBeInTheDocument();
    expect(row(second).getByRole("link", { name: "Open document" })).toHaveAttribute(
      "href",
      `/documents/${DOC2}`,
    );
    expect(row(third).getByText("Refused")).toBeInTheDocument();
    expect(row(third).getByText(REFUSAL_TEXT.content_mismatch)).toBeInTheDocument();
    expect(row(third).queryByRole("link")).toBeNull();
  });

  it("shows waiting and uploading, and queues a drop made during a run", async () => {
    let release: (response: Response) => void = () => undefined;
    const { spy, sent, mostAtOnce } = backend((_sent, index) =>
      index === 0
        ? new Promise<Response>((resolve) => {
            release = resolve;
          })
        : imported(DOC2),
    );
    await openImport(spy);
    drop([file("a.txt"), file("b.txt")]);
    await waitFor(() => {
      expect(row("a.txt").getByText("Uploading")).toBeInTheDocument();
    });
    expect(row("b.txt").getByText("Waiting")).toBeInTheDocument();
    expect(screen.getByText("0 of 2 files finished")).toBeInTheDocument();
    drop([file("c.txt")]);
    expect(row("c.txt").getByText("Waiting")).toBeInTheDocument();
    release(imported());
    expect(await screen.findByText("3 of 3 files finished")).toBeInTheDocument();
    expect(mostAtOnce()).toBe(1);
    expect(sent.map((each) => each.filename)).toEqual(["a.txt", "b.txt", "c.txt"]);
  });

  it("starts a new batch for each drop", async () => {
    const { spy, sent } = backend((_sent, index) =>
      imported(DOC, index === 0 ? BATCH : OTHER_BATCH),
    );
    await openImport(spy);
    drop([file("a.txt")]);
    expect(await screen.findByText("1 of 1 files finished")).toBeInTheDocument();
    drop([file("b.txt"), file("c.txt")]);
    expect(await screen.findByText("3 of 3 files finished")).toBeInTheDocument();
    expect(sent.map((each) => each.batch)).toEqual([null, null, OTHER_BATCH]);
  });

  it("uploads files chosen with the button", async () => {
    const { spy, sent } = backend(() => imported());
    await openImport(spy);
    expect(screen.getByLabelText("Files to import")).toHaveAttribute(
      "accept",
      ".pdf,.docx,.png,.jpg,.jpeg,.txt,.md",
    );
    await userEvent.upload(screen.getByLabelText("Files to import"), [file("a.md"), file("b.md")]);
    expect(await screen.findByText("2 of 2 files finished")).toBeInTheDocument();
    expect(sent.map((each) => each.filename)).toEqual(["a.md", "b.md"]);
  });

  it.each(Object.keys(REFUSAL_TEXT).filter((key) => key !== "request" && key !== "folder"))(
    "shows the fixed text for the refusal %s",
    async (reason) => {
      const { spy } = backend(() =>
        jsonResponse({ outcome: "refused", batch_id: BATCH, document_id: null, reason }),
      );
      await openImport(spy);
      drop([file("x.pdf")]);
      expect(await row("x.pdf").findByText(REFUSAL_TEXT[reason as Reason])).toBeInTheDocument();
      expect(row("x.pdf").getByText("Refused")).toBeInTheDocument();
    },
  );

  it.each([
    [413, REFUSAL_TEXT.too_large],
    [400, REFUSAL_TEXT.request],
    [422, REFUSAL_TEXT.request],
  ])("shows a %i as a refusal, without the server's message", async (status, text) => {
    const { spy, sent } = backend((_sent, index) =>
      index === 0 ? jsonResponse({ detail: "server words" }, status) : imported(),
    );
    await openImport(spy);
    drop([file("x.pdf"), file("y.pdf")]);
    expect(await screen.findByText("2 of 2 files finished")).toBeInTheDocument();
    expect(row("x.pdf").getByText(text)).toBeInTheDocument();
    expect(screen.queryByText(/server words/)).toBeNull();
    // A refused request names no batch, so the next file starts one.
    expect(sent.map((each) => each.batch)).toEqual([null, null]);
  });

  it.each([
    ["a 500", () => jsonResponse({ detail: "boom" }, 500)],
    ["a lost connection", () => Promise.reject(new TypeError("Failed to fetch"))],
    ["an answer that is not JSON", () => new Response("<html>", { status: 200 })],
    ["an answer without a document", () => jsonResponse({ outcome: "imported", batch_id: BATCH })],
    ["an unknown outcome", () => jsonResponse({ outcome: "maybe", batch_id: BATCH })],
    [
      "an unknown reason",
      () => jsonResponse({ outcome: "refused", batch_id: BATCH, reason: "request" }),
    ],
    ["a batch that is not a ULID", () => imported(DOC, `${BATCH}\n`)],
  ])("shows %s as unknown and goes on with the next file", async (_label, answer) => {
    const { spy, sent } = backend((_sent, index) => (index === 0 ? answer() : imported()));
    await openImport(spy);
    drop([file("x.pdf"), file("y.pdf")]);
    expect(await screen.findByText("2 of 2 files finished")).toBeInTheDocument();
    expect(row("x.pdf").getByText("Unknown")).toBeInTheDocument();
    expect(row("x.pdf").getByText(UNKNOWN_TEXT)).toBeInTheDocument();
    expect(row("y.pdf").getByText("Imported")).toBeInTheDocument();
    expect(sent).toHaveLength(2);
  });

  it("stops the run and brings the token notice back when the token is refused", async () => {
    const { spy, sent } = backend(() => jsonResponse({ detail: "request refused" }, 401));
    await openImport(spy);
    let sessionChecks = spy.mock.calls.filter(
      ([input]) => new URL((input as Request).url).pathname === "/api/session",
    ).length;
    expect(sessionChecks).toBe(1);
    drop([file("x.pdf"), file("y.pdf"), file("z.pdf")]);
    expect(await screen.findByText("3 of 3 files finished")).toBeInTheDocument();
    expect(sent).toHaveLength(1);
    expect(row("x.pdf").getByText("Not imported")).toBeInTheDocument();
    expect(row("y.pdf").getByText("Not sent")).toBeInTheDocument();
    expect(row("z.pdf").getAllByText(ERROR_TEXT["needs-token"])).toHaveLength(1);
    await waitFor(() => {
      sessionChecks = spy.mock.calls.filter(
        ([input]) => new URL((input as Request).url).pathname === "/api/session",
      ).length;
      expect(sessionChecks).toBe(2);
    });
  });

  it("stops the run when no vault is open", async () => {
    const { spy, sent } = backend(() => jsonResponse({ detail: "no vault is open" }, 503));
    await openImport(spy);
    drop([file("x.pdf"), file("y.pdf")]);
    expect(await screen.findByText("2 of 2 files finished")).toBeInTheDocument();
    expect(sent).toHaveLength(1);
    expect(row("x.pdf").getByText(ERROR_TEXT["no-vault"])).toBeInTheDocument();
    expect(row("y.pdf").getByText("Not sent")).toBeInTheDocument();
  });

  it("refuses a dropped folder without sending it", async () => {
    const { spy, sent } = backend(() => imported());
    await openImport(spy);
    const folder = new File([], "Clients");
    drop([folder, file("a.txt")], new Set([folder]));
    expect(await screen.findByText("2 of 2 files finished")).toBeInTheDocument();
    expect(row("Clients").getByText(REFUSAL_TEXT.folder)).toBeInTheDocument();
    expect(sent.map((each) => each.filename)).toEqual(["a.txt"]);
  });

  it("keeps the browser from opening a file dropped beside the zone", async () => {
    const { spy, sent } = backend(() => imported());
    await openImport(spy);
    const outside = new Event("drop", { cancelable: true });
    window.dispatchEvent(outside);
    expect(outside.defaultPrevented).toBe(true);
    expect(sent).toHaveLength(0);
  });

  it("reads the files of a drop whose browser lists no items", async () => {
    const { spy, sent } = backend(() => imported());
    await openImport(spy);
    fireEvent.drop(dropZone(), { dataTransfer: { files: [file("a.txt")], items: [] } });
    expect(await screen.findByText("1 of 1 files finished")).toBeInTheDocument();
    expect(sent.map((each) => each.filename)).toEqual(["a.txt"]);
  });

  it("refuses a name that cannot be encoded without sending it", async () => {
    const { spy, sent } = backend(() => imported());
    await openImport(spy);
    // A File's name is a USVString, so the File constructor replaces a lone surrogate; the guard
    // covers a name that reaches the page some other way.
    const bad = file("bad.txt");
    Object.defineProperty(bad, "name", { value: "bad\uD800.txt" });
    drop([bad, file("good.txt")]);
    expect(await screen.findByText("2 of 2 files finished")).toBeInTheDocument();
    expect(sent.map((each) => each.filename)).toEqual(["good.txt"]);
    expect(screen.getByText(REFUSAL_TEXT.request)).toBeInTheDocument();
  });

  it("leaves out a modification time the backend would refuse", async () => {
    const { spy, sent } = backend(() => imported());
    await openImport(spy);
    drop([file("old.txt", "x", -1), file("new.txt", "x", 0)]);
    expect(await screen.findByText("2 of 2 files finished")).toBeInTheDocument();
    expect(sent.map((each) => each.mtime)).toEqual([null, "0"]);
  });

  it("keeps uploading while another page is open, and clears finished files", async () => {
    let release: (response: Response) => void = () => undefined;
    const { spy } = backend(
      () =>
        new Promise<Response>((resolve) => {
          release = resolve;
        }),
    );
    await openImport(spy);
    drop([file("a.txt")]);
    await waitFor(() => {
      expect(row("a.txt").getByText("Uploading")).toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole("link", { name: "Dashboard" }));
    release(imported());
    await userEvent.click(screen.getByRole("link", { name: "Import" }));
    expect(await screen.findByText("1 of 1 files finished")).toBeInTheDocument();
    expect(row("a.txt").getByText("Imported")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear finished" }));
    expect(screen.queryByRole("table", { name: "Uploads" })).toBeNull();
  });

  it("never writes a file name to the console, whatever the outcome", async () => {
    const calls = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method),
    );
    const answers: (() => Response | Promise<Response>)[] = [
      () => imported(),
      () => jsonResponse({ detail: "x" }, 400),
      () => jsonResponse({ detail: "x" }, 500),
      () => Promise.reject(new TypeError("Failed to fetch")),
      () => new Response("not json", { status: 200 }),
      () => jsonResponse({ detail: "request refused" }, 401),
    ];
    const { spy } = backend((_sent, index) => (answers[index] ?? (() => imported()))());
    await openImport(spy);
    const names = answers.map((_answer, index) => `Patient ${String(index)} – Jane Doe.pdf`);
    drop([...names.map((name) => file(name)), file("bad\uD800 Jane Doe.txt")]);
    expect(await screen.findByText("7 of 7 files finished")).toBeInTheDocument();
    for (const call of calls) {
      expect(JSON.stringify(call.mock.calls)).not.toMatch(/Jane|Patient/);
    }
  });
});

describe("mtimeHeader", () => {
  it.each([
    [0, "0"],
    [MTIME, String(MTIME)],
    [MAX_MTIME_MS, String(MAX_MTIME_MS)],
    [MAX_MTIME_MS + 1, undefined],
    [-1, undefined],
    [1.5, undefined],
    [Number.NaN, undefined],
    [Number.POSITIVE_INFINITY, undefined],
  ])("%d gives %s", (value, expected) => {
    expect(mtimeHeader(value)).toBe(expected);
  });
});

describe("agreement with the backend", () => {
  it("knows every refusal reason the API can return, and no other", () => {
    const schema = JSON.parse(readFileSync("openapi.json", "utf8")) as {
      components: { schemas: { Reason: { enum: string[] } } };
    };
    const own = Object.keys(REFUSAL_TEXT).filter((key) => key !== "request" && key !== "folder");
    expect(own.sort()).toEqual([...schema.components.schemas.Reason.enum].sort());
  });

  it("names the validator's extensions and the upload's time limit", () => {
    const validate = readFileSync("../backend/src/vault/ingest/validate.py", "utf8");
    const table =
      /^EXTENSIONS: Final\[dict\[str, Kind\]\] = \{\n((?: {4}"[a-z]+": Kind\.[A-Z]+,\n)+)\}/m.exec(
        validate,
      );
    expect(table).not.toBeNull();
    const extensions = [...(table?.[1] ?? "").matchAll(/"([a-z]+)"/g)].map((match) => match[1]);
    expect([...SUPPORTED_EXTENSIONS].sort()).toEqual(extensions.sort());

    const imports = readFileSync("../backend/src/vault/api/imports.py", "utf8");
    const limit = /^MAX_MTIME_MS: Final = ([0-9_]+)\b/m.exec(imports);
    expect(Number(limit?.[1]?.replaceAll("_", ""))).toBe(MAX_MTIME_MS);
  });
});
