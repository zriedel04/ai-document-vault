import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type DocumentDetail, TEXT_WINDOW } from "../api/documents";
import { type FetchFn, TEST_TOKEN, healthyBackend, jsonResponse, renderApp } from "../test/render";
import { LARGE_IMAGE_BYTES, LARGE_IMAGE_PIXELS } from "./image-limits";

const ID = "01J9Z8Y7X6W5V4T3S2R1Q0P9N8";
const OTHER = "01J9Z8Y7X6W5V4T3S2R1Q0P9N7";
const VERSION = "01J9Z8Y7X6W5V4T3S2R1Q0P9N9";
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);

function detail(id: string, overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    id,
    name: "Scan.png",
    extension: "png",
    mime: "image/png",
    size: PNG.length,
    state: "ready",
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
    labels: [],
    title: { value: "Scan", source: "filename" },
    ...overrides,
  };
}

const NO_TEXT = {
  version_id: VERSION,
  extracted: false,
  extractor: null,
  total: 0,
  offset: 0,
  limit: TEXT_WINDOW,
  pages: [],
  next_offset: null,
};

type FileHandler = (id: string) => Response;

// The file answers with a type the viewer must not trust: it labels the blob itself.
const pngFile: FileHandler = () => new Response(PNG, { headers: { "content-type": "text/html" } });

/** A backend whose documents are `details`, and whose file route answers with `file`. */
function backend(details: Record<string, DocumentDetail>, file: FileHandler = pngFile) {
  return vi.fn<FetchFn>((input) => {
    const request = input as Request;
    const { pathname } = new URL(request.url);
    const match = /^\/api\/documents\/([^/]+)(\/file|\/pages)?$/.exec(pathname);
    if (match === null) return healthyBackend(input);
    if (request.headers.get("authorization") !== `Bearer ${TEST_TOKEN}`) {
      return Promise.resolve(jsonResponse({ detail: "request refused" }, 401));
    }
    const [, id = "", rest] = match;
    const found = details[id];
    if (found === undefined) return Promise.resolve(jsonResponse({ detail: "not found" }, 404));
    if (rest === "/file") return Promise.resolve(file(id));
    if (rest === "/pages") return Promise.resolve(jsonResponse(NO_TEXT));
    return Promise.resolve(jsonResponse(found));
  });
}

function fileRequests(spy: ReturnType<typeof backend>): Request[] {
  return spy.mock.calls
    .map(([input]) => input as Request)
    .filter((request) => new URL(request.url).pathname.endsWith("/file"));
}

let created: { url: string; blob: Blob }[];
let revoked: string[];

beforeEach(() => {
  created = [];
  revoked = [];
  URL.createObjectURL = vi.fn((blob: Blob) => {
    const url = `blob:test/${String(created.length + 1)}`;
    created.push({ url, blob });
    return url;
  });
  URL.revokeObjectURL = vi.fn((url: string) => {
    revoked.push(url);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function findImage(): Promise<HTMLImageElement> {
  // jsdom lays nothing out, so the image is found by its alt text, also while it is not drawn.
  return waitFor(() => {
    const image = document.querySelector<HTMLImageElement>('img[alt="The document\'s image"]');
    if (image === null) throw new Error("no image yet");
    return image;
  });
}

/** The browser has read the image's header: jsdom loads no images, so the test says its size. */
function load(image: HTMLImageElement, width: number, height: number) {
  Object.defineProperty(image, "naturalWidth", { configurable: true, value: width });
  Object.defineProperty(image, "naturalHeight", { configurable: true, value: height });
  fireEvent.load(image);
}

describe("image view", () => {
  it("fetches the image with the token, labels it as PNG and shows it once its size is read", async () => {
    const spy = backend({ [ID]: detail(ID) });
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByRole("heading", { level: 3, name: "Image" })).toBeInTheDocument();

    const image = await findImage();
    expect(image.getAttribute("src")).toBe("blob:test/1");
    // Not drawn (so not decoded) until its size is known.
    expect(image.style.display).toBe("none");
    expect(screen.getByLabelText("Reading the image")).toBeInTheDocument();

    const [request] = fileRequests(spy);
    expect(request?.headers.get("authorization")).toBe(`Bearer ${TEST_TOKEN}`);
    expect(request?.url).not.toContain(TEST_TOKEN);
    const blob = created[0]?.blob ?? new Blob();
    expect(blob.type).toBe("image/png"); // the document's type, not the response's text/html
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(PNG);

    load(image, 800, 600);
    expect(image.style.display).toBe("block");
    expect(image.style.maxWidth).toBe("100%");
    expect(screen.getByText("Fitted to width")).toBeInTheDocument();
    const alone = screen.getByRole("link", { name: "Open image alone" });
    expect(alone).toHaveAttribute("href", "blob:test/1");
    expect(alone).toHaveAttribute("target", "_blank");
    expect(alone).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("labels a JPEG as JPEG", async () => {
    const spy = backend({ [ID]: detail(ID, { mime: "image/jpeg", extension: "jpg" }) });
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    await findImage();
    expect(created[0]?.blob.type).toBe("image/jpeg");
  });

  it.each([
    ["text/markdown", "md"],
    ["text/plain", "txt"],
    ["image/svg+xml", "svg"],
  ])("shows no viewer for a %s document and fetches no file", async (mime, extension) => {
    const spy = backend({ [ID]: detail(ID, { mime, extension }) });
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    await screen.findByRole("heading", { level: 3, name: "Text" });
    expect(screen.queryByRole("heading", { name: "Image" })).toBeNull();
    expect(fileRequests(spy)).toEqual([]);
    expect(created).toEqual([]);
  });

  it("says when the image is not loaded, and tries again", async () => {
    let fail = true;
    const spy = backend({ [ID]: detail(ID) }, (id) =>
      fail ? jsonResponse({ detail: "unavailable" }, 500) : pngFile(id),
    );
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText("Image not loaded")).toBeInTheDocument();
    expect(
      screen.getByText("The vault's stored copy of this file is missing or damaged."),
    ).toBeInTheDocument();
    expect(created).toEqual([]);

    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect((await findImage()).getAttribute("src")).toBe("blob:test/1");
    expect(screen.queryByText("Image not loaded")).toBeNull();
  });

  it("shows an image the browser cannot read as an error", async () => {
    const spy = backend({ [ID]: detail(ID) });
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    fireEvent.error(await findImage());
    expect(screen.getByText("Image not shown")).toBeInTheDocument();
  });

  it("zooms to 100%, in and out by steps, and back to fit", async () => {
    const spy = backend({ [ID]: detail(ID) });
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    const image = await findImage();
    load(image, 800, 600);
    // Fitted into 400 pixels: the image is drawn at 50%, so a step in goes to 75%.
    Object.defineProperty(image, "clientWidth", { configurable: true, value: 400 });
    await userEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(image.style.width).toBe("600px");
    expect(screen.getByText("75%")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "100%" }));
    expect(image.style.width).toBe("800px");
    expect(image.style.maxWidth).toBe("none");
    await userEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(image.style.width).toBe("600px");

    for (let i = 0; i < 6; i += 1) {
      await userEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    }
    expect(image.style.width).toBe("3200px"); // 400%, the last step
    expect(screen.getByRole("button", { name: "Zoom in" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Fit" }));
    expect(image.style.maxWidth).toBe("100%");
    expect(image.style.width).toBe("");
    expect(screen.getByText("Fitted to width")).toBeInTheDocument();
  });

  it("releases the address when the page goes and when another document is shown", async () => {
    const spy = backend({ [ID]: detail(ID), [OTHER]: detail(OTHER) });
    const { router } = await renderApp({
      route: `/documents/${ID}`,
      fetch: spy,
      token: TEST_TOKEN,
    });
    await findImage();
    expect(revoked).toEqual([]);

    act(() => {
      router.history.push(`/documents/${OTHER}`);
    });
    await waitFor(() => {
      expect(created.map((entry) => entry.url)).toEqual(["blob:test/1", "blob:test/2"]);
    });
    expect(revoked).toEqual(["blob:test/1"]);
    expect((await findImage()).getAttribute("src")).toBe("blob:test/2");

    act(() => {
      router.history.push("/documents");
    });
    await waitFor(() => {
      expect(revoked).toEqual(["blob:test/1", "blob:test/2"]);
    });
  });

  it("makes no address for an answer that arrives after the view has gone", async () => {
    const spy = backend({ [ID]: detail(ID) });
    let answer: () => void = () => undefined;
    // The file's answer is held until the page has moved on.
    const fetch = vi.fn<FetchFn>((input) => {
      if (!new URL((input as Request).url).pathname.endsWith("/file")) return spy(input);
      return new Promise<Response>((resolve) => {
        answer = () => {
          resolve(pngFile(ID));
        };
      });
    });
    const { router } = await renderApp({ route: `/documents/${ID}`, fetch, token: TEST_TOKEN });
    await screen.findByLabelText("Loading the image");
    act(() => {
      router.history.push("/documents");
    });
    await screen.findByRole("heading", { level: 2, name: "Documents" });
    await act(async () => {
      answer();
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(created).toEqual([]);
  });

  it("asks before fetching a large file", async () => {
    const spy = backend({ [ID]: detail(ID, { size: LARGE_IMAGE_BYTES + 1 }) });
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    expect(await screen.findByText("Large image")).toBeInTheDocument();
    expect(fileRequests(spy)).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Show image" }));
    expect((await findImage()).getAttribute("src")).toBe("blob:test/1");
    expect(fileRequests(spy)).toHaveLength(1);
  });

  it("does not draw an image of very many pixels until asked", async () => {
    const spy = backend({ [ID]: detail(ID) });
    await renderApp({ route: `/documents/${ID}`, fetch: spy, token: TEST_TOKEN });
    const image = await findImage();
    const side = Math.ceil(Math.sqrt(LARGE_IMAGE_PIXELS)) + 1;
    load(image, side, side);
    expect(screen.getByText("Very large image")).toBeInTheDocument();
    expect(image.style.display).toBe("none");
    expect(screen.queryByRole("button", { name: "Zoom in" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show anyway" }));
    expect(image.style.display).toBe("block");
  });
});
