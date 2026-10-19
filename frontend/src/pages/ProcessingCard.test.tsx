import { readFileSync } from "node:fs";

import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ACTION_FAILED_TEXT,
  POLL_INTERVAL_MS,
  PROCESSING_ERROR_TEXT,
  type ProcessingStatus,
  STAGE_NAME,
  processingError,
} from "../api/processing";
import { type FetchFn, TEST_TOKEN, healthyBackend, jsonResponse, renderApp } from "../test/render";

const STAGES = ["extract", "ocr", "metadata", "chunk", "embed", "enrich"] as const;

function status(paused: boolean, extract = { queued: 2, running: 1, done: 5, failed: 1 }) {
  return {
    paused,
    stages: STAGES.map((stage) =>
      stage === "extract"
        ? { stage, ...extract }
        : { stage, queued: 0, running: 0, done: 0, failed: 0 },
    ),
  } satisfies ProcessingStatus;
}

type Answer = (request: Request) => Response | Promise<Response>;

/** The backend with its processing endpoints answered by `answers`, keyed by path. */
function backend(answers: Record<string, Answer>) {
  return vi.fn<FetchFn>((input) => {
    const request = input as Request;
    const { pathname } = new URL(request.url);
    const answer = answers[pathname];
    if (answer !== undefined) {
      if (request.headers.get("authorization") !== `Bearer ${TEST_TOKEN}`) {
        return Promise.resolve(jsonResponse({ detail: "request refused" }, 401));
      }
      return Promise.resolve(answer(request));
    }
    return healthyBackend(input);
  });
}

function calls(fetch: ReturnType<typeof backend>, path: string, method: string): number {
  return fetch.mock.calls.filter(([input]) => {
    const request = input as Request;
    return new URL(request.url).pathname === path && request.method === method;
  }).length;
}

/** A response the test hands over when it chooses, to hold a request open. */
function held() {
  let release: (response: Response) => void = () => undefined;
  const response = new Promise<Response>((resolve) => {
    release = resolve;
  });
  return {
    response,
    release: (answer: Response) => {
      release(answer);
    },
  };
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
}

/** Lets the poll's timer run out and its request answer. */
async function nextPoll() {
  await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS));
}

/** Lets answered requests reach the card without moving the clock. */
async function settle() {
  for (let round = 0; round < 20; round += 1) {
    await act(() => vi.advanceTimersByTimeAsync(0));
  }
}

afterEach(() => {
  sessionStorage.clear();
  vi.useRealTimers();
  setHidden(false);
});

describe("processing on the dashboard", () => {
  it("shows that processing runs and the jobs of each stage that has any", async () => {
    const fetch = backend({ "/api/processing": () => jsonResponse(status(false)) });
    await renderApp({ fetch, token: TEST_TOKEN });
    expect(await screen.findByText(/^Running\./)).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Jobs by stage" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(2); // the header and text extraction; empty stages are left out
    expect(
      within(rows[1] as HTMLElement)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([STAGE_NAME.extract, "2", "1", "5", "1"]);
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });

  it("says so when there are no jobs yet", async () => {
    const empty = { queued: 0, running: 0, done: 0, failed: 0 };
    const fetch = backend({ "/api/processing": () => jsonResponse(status(false, empty)) });
    await renderApp({ fetch, token: TEST_TOKEN });
    expect(await screen.findByText("No processing jobs yet.")).toBeInTheDocument();
  });

  it("pauses and resumes, showing the state the backend answers with", async () => {
    let paused = false;
    const fetch = backend({
      "/api/processing": () => jsonResponse(status(paused)),
      "/api/processing/pause": () => {
        paused = true;
        return jsonResponse({ paused: true });
      },
      "/api/processing/resume": () => {
        paused = false;
        return jsonResponse({ paused: false });
      },
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByText(/^Paused\./)).toBeInTheDocument();
    expect(calls(fetch, "/api/processing/pause", "POST")).toBe(1);
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByText(/^Running\./)).toBeInTheDocument();
    expect(calls(fetch, "/api/processing/resume", "POST")).toBe(1);
    expect(screen.queryByText(ACTION_FAILED_TEXT)).not.toBeInTheDocument();
  });

  it("loads the status again when a pause gets no answer", async () => {
    let paused = false;
    const fetch = backend({
      "/api/processing": () => jsonResponse(status(paused)),
      "/api/processing/pause": () => {
        paused = true; // it reached the backend, but the answer was lost
        throw new TypeError("network");
      },
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByText(ACTION_FAILED_TEXT)).toBeInTheDocument();
    expect(await screen.findByText(/^Paused\./)).toBeInTheDocument();
  });

  it("loads the counts again after an action", async () => {
    let paused = true;
    let extract = { queued: 2, running: 1, done: 5, failed: 1 };
    const fetch = backend({
      "/api/processing": () => jsonResponse(status(paused, extract)),
      "/api/processing/resume": () => {
        paused = false;
        extract = { queued: 0, running: 2, done: 7, failed: 1 };
        return jsonResponse({ paused: false });
      },
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await userEvent.click(await screen.findByRole("button", { name: "Resume" }));
    expect(await screen.findByText(/^Running\./)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("cell", { name: "7" })).toBeInTheDocument();
    });
    expect(calls(fetch, "/api/processing", "GET")).toBe(2);
  });

  // Every way a pause can go wrong. The pause took effect in each case here (a 500 from a proxy,
  // an answer lost on the way back), so only loading the status again shows the truth, and the
  // old "Running" must not stay on screen as if it were current.
  it.each([
    ["a 500", () => jsonResponse({ detail: "Patient record" }, 500)],
    ["a 401", () => jsonResponse({ detail: "request refused" }, 401)],
    ["a 503", () => jsonResponse({ detail: "no vault is open" }, 503)],
    ["a 200 that is not an answer", () => jsonResponse({ detail: "Patient record" })],
    ["a lost answer", () => Promise.reject(new TypeError("network"))],
  ] as const)("loads the status again after %s to a pause", async (_name, answer) => {
    let paused = false;
    const reload = held();
    let gets = 0;
    const fetch = backend({
      "/api/processing": () => {
        gets += 1;
        return gets === 1 ? jsonResponse(status(paused)) : reload.response;
      },
      "/api/processing/pause": () => {
        paused = true;
        return answer();
      },
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByText(ACTION_FAILED_TEXT)).toBeInTheDocument();
    await waitFor(() => {
      expect(gets).toBe(2);
    });
    // While the status is loaded again, the card claims neither state.
    expect(screen.queryByText(/^Running\./)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Paused\./)).not.toBeInTheDocument();
    reload.release(jsonResponse(status(paused)));
    expect(await screen.findByText(/^Paused\./)).toBeInTheDocument();
    expect(screen.queryByText(/Patient/)).not.toBeInTheDocument();
  });

  it("says the status is unknown when a pause fails and so does the status", async () => {
    let gets = 0;
    const fetch = backend({
      "/api/processing": () => {
        gets += 1;
        return gets === 1 ? jsonResponse(status(false)) : Promise.reject(new TypeError("down"));
      },
      "/api/processing/pause": () => jsonResponse({ detail: "failed" }, 500),
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByText(PROCESSING_ERROR_TEXT.unreachable)).toBeInTheDocument();
    expect(screen.getByText(ACTION_FAILED_TEXT)).toBeInTheDocument();
    expect(screen.queryByText(/^Running\./)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Paused\./)).not.toBeInTheDocument();
  });

  it("says the status is unknown when a pause works but the status then fails", async () => {
    let gets = 0;
    const fetch = backend({
      "/api/processing": () => {
        gets += 1;
        return gets === 1
          ? jsonResponse(status(false))
          : jsonResponse({ detail: "database is locked" }, 500);
      },
      "/api/processing/pause": () => jsonResponse({ paused: true }),
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(await screen.findByText(PROCESSING_ERROR_TEXT.unreachable)).toBeInTheDocument();
    expect(screen.queryByText(/^Running\./)).not.toBeInTheDocument();
    expect(screen.queryByText(ACTION_FAILED_TEXT)).not.toBeInTheDocument();
  });

  it.each([
    [401, "needs-token"],
    [503, "no-vault"],
    [500, "unreachable"],
  ] as const)("shows fixed text for a %i", async (code, error) => {
    expect(processingError(code)).toBe(error);
    const fetch = backend({
      "/api/processing": () => jsonResponse({ detail: "Patient record" }, code),
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    // Wait for the answer: before access is checked, the card shows the 401's text as well.
    await waitFor(() => {
      expect(calls(fetch, "/api/processing", "GET")).toBe(1);
    });
    await waitFor(() => {
      expect(screen.getByText(PROCESSING_ERROR_TEXT[error])).toBeInTheDocument();
    });
    expect(screen.queryByText(/Patient/)).not.toBeInTheDocument();
  });

  it("asks for access without a token and loads once one is pasted", async () => {
    const fetch = backend({ "/api/processing": () => jsonResponse(status(false)) });
    await renderApp({ fetch });
    expect(calls(fetch, "/api/processing", "GET")).toBe(0);
    expect(await screen.findByText(PROCESSING_ERROR_TEXT["needs-token"])).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Access token"), TEST_TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    await waitFor(() => {
      expect(screen.getByText(/^Running\./)).toBeInTheDocument();
    });
  });

  it("names every stage of the backend's schema", () => {
    const schema = JSON.parse(readFileSync("openapi.json", "utf-8")) as {
      components: { schemas: { StageJobs: { properties: { stage: { enum: string[] } } } } };
    };
    expect(Object.keys(STAGE_NAME)).toEqual(
      schema.components.schemas.StageJobs.properties.stage.enum,
    );
  });
});

describe("polling the processing status", () => {
  beforeEach(() => {
    // Only the poll's timer is faked; Testing Library's own waiting keeps its real interval.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  });

  it("asks again at a fixed interval while the card is shown", async () => {
    let extract = { queued: 2, running: 1, done: 5, failed: 1 };
    const fetch = backend({ "/api/processing": () => jsonResponse(status(false, extract)) });
    await renderApp({ fetch, token: TEST_TOKEN });
    await settle();
    expect(screen.getByText(/^Running\./)).toBeInTheDocument();
    expect(calls(fetch, "/api/processing", "GET")).toBe(1);
    await act(() => vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS - 1));
    expect(calls(fetch, "/api/processing", "GET")).toBe(1);
    extract = { queued: 0, running: 1, done: 7, failed: 1 };
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(calls(fetch, "/api/processing", "GET")).toBe(2);
    await settle();
    expect(screen.getByRole("cell", { name: "7" })).toBeInTheDocument();
    await nextPoll();
    expect(calls(fetch, "/api/processing", "GET")).toBe(3);
  });

  it("never sends a poll while the last one is unanswered", async () => {
    const slow = held();
    let gets = 0;
    const fetch = backend({
      "/api/processing": () => {
        gets += 1;
        return gets === 2 ? slow.response : jsonResponse(status(false));
      },
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await settle();
    expect(screen.getByText(/^Running\./)).toBeInTheDocument();
    await nextPoll();
    expect(gets).toBe(2);
    await nextPoll();
    await nextPoll();
    expect(gets).toBe(2);
    slow.release(jsonResponse(status(false)));
    await settle();
    expect(gets).toBe(2); // the next one waits a whole interval after the answer
    await nextPoll();
    expect(gets).toBe(3);
  });

  it("waits while the tab is hidden", async () => {
    const fetch = backend({ "/api/processing": () => jsonResponse(status(false)) });
    await renderApp({ fetch, token: TEST_TOKEN });
    await settle();
    expect(screen.getByText(/^Running\./)).toBeInTheDocument();
    setHidden(true);
    await nextPoll();
    await nextPoll();
    expect(calls(fetch, "/api/processing", "GET")).toBe(1);
    setHidden(false);
    await nextPoll();
    expect(calls(fetch, "/api/processing", "GET")).toBe(2);
  });

  it("stops when the card goes away", async () => {
    const fetch = backend({ "/api/processing": () => jsonResponse(status(false)) });
    const view = await renderApp({ fetch, token: TEST_TOKEN });
    await settle();
    expect(screen.getByText(/^Running\./)).toBeInTheDocument();
    view.unmount();
    await nextPoll();
    await nextPoll();
    expect(calls(fetch, "/api/processing", "GET")).toBe(1);
  });

  it("plans no other poll when the card goes away with one in flight", async () => {
    const pending = held();
    let gets = 0;
    const fetch = backend({
      "/api/processing": () => {
        gets += 1;
        return gets === 2 ? pending.response : jsonResponse(status(false));
      },
    });
    const view = await renderApp({ fetch, token: TEST_TOKEN });
    await settle();
    expect(screen.getByText(/^Running\./)).toBeInTheDocument();
    await nextPoll();
    expect(gets).toBe(2);
    view.unmount();
    pending.release(jsonResponse(status(true)));
    await settle();
    await nextPoll();
    await nextPoll();
    expect(gets).toBe(2);
  });

  it("drops a poll that answers after a pause began", async () => {
    let paused = false;
    const stale = held();
    let gets = 0;
    const fetch = backend({
      "/api/processing": () => {
        gets += 1;
        // The second request is a poll sent before the pause and answered after it, with the
        // state from before; every other one answers with the state as it is.
        return gets === 2 ? stale.response : jsonResponse(status(paused));
      },
      "/api/processing/pause": () => {
        paused = true;
        return jsonResponse({ paused: true });
      },
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await settle();
    const pause = screen.getByRole("button", { name: "Pause" });
    await nextPoll();
    expect(gets).toBe(2); // the poll is in flight
    fireEvent.click(pause);
    await settle();
    expect(gets).toBe(3); // the pause loaded the status itself
    await settle();
    expect(screen.getByText(/^Paused\./)).toBeInTheDocument();
    stale.release(jsonResponse(status(false)));
    await settle();
    expect(screen.getByText(/^Paused\./)).toBeInTheDocument();
    expect(screen.queryByText(/^Running\./)).not.toBeInTheDocument();
  });

  it("drops a poll that answers while a pause is in progress", async () => {
    const stale = held();
    const slowPause = held();
    let gets = 0;
    const fetch = backend({
      "/api/processing": () => {
        gets += 1;
        return gets === 2 ? stale.response : jsonResponse(status(gets > 2));
      },
      "/api/processing/pause": () => slowPause.response,
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await settle();
    const pause = screen.getByRole("button", { name: "Pause" });
    await nextPoll();
    fireEvent.click(pause);
    stale.release(jsonResponse(status(false, { queued: 9, running: 9, done: 9, failed: 9 })));
    await settle();
    expect(screen.queryAllByRole("cell", { name: "9" })).toHaveLength(0);
    slowPause.release(jsonResponse({ paused: true }));
    await settle();
    expect(screen.getByText(/^Paused\./)).toBeInTheDocument();
    expect(screen.queryAllByRole("cell", { name: "9" })).toHaveLength(0);
  });

  it("sends no poll while a pause is in progress", async () => {
    let paused = false;
    const slowPause = held();
    const fetch = backend({
      "/api/processing": () => jsonResponse(status(paused)),
      "/api/processing/pause": () => slowPause.response,
    });
    await renderApp({ fetch, token: TEST_TOKEN });
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await nextPoll();
    await nextPoll();
    expect(calls(fetch, "/api/processing", "GET")).toBe(1);
    paused = true;
    slowPause.release(jsonResponse({ paused: true }));
    await settle();
    expect(screen.getByText(/^Paused\./)).toBeInTheDocument();
    expect(calls(fetch, "/api/processing", "GET")).toBe(2);
  });
});
