import { readFileSync } from "node:fs";

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ACTION_FAILED_TEXT,
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

afterEach(() => {
  sessionStorage.clear();
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
        return jsonResponse(status(true));
      },
      "/api/processing/resume": () => {
        paused = false;
        return jsonResponse(status(false));
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
