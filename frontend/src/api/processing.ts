import type { components } from "./schema";

export type ProcessingStatus = components["schemas"]["ProcessingStatus"];
/** What a pause or resume answers: the flag alone, since the counts come from the status. */
export type ProcessingState = components["schemas"]["ProcessingState"];

/**
 * How often the card asks for the status while it is shown (plan section 2: the UI polls job
 * progress). The next request is planned only once the last one has answered, so they never
 * overlap, and none is sent while the tab is hidden or an action is in progress.
 */
export const POLL_INTERVAL_MS = 5000;
export type StageJobs = components["schemas"]["StageJobs"];

export const STAGE_NAME: Record<StageJobs["stage"], string> = {
  extract: "Text extraction",
  ocr: "OCR",
  metadata: "Metadata",
  chunk: "Chunking",
  embed: "Embedding",
  enrich: "Enrichment",
};

/** Why the processing status gave no answer. */
export type ProcessingError = "needs-token" | "no-vault" | "unreachable";

/** The error a response stands for, by its status code alone (its body is not shown). */
export function processingError(status: number): ProcessingError {
  if (status === 401) return "needs-token";
  if (status === 503) return "no-vault";
  return "unreachable";
}

export const PROCESSING_ERROR_TEXT: Record<ProcessingError, string> = {
  "needs-token": "This browser has no access to the vault yet. See the notice above.",
  "no-vault": "No vault is open. Start the app with a vault folder.",
  unreachable: "The processing status could not be loaded. Check that the backend is running.",
};

/**
 * What a pause or resume says when it got no usable answer (refused, an error, or none at all).
 * It may or may not have reached the backend, so the status is loaded again, and the card shows
 * what that load gives: the status, or that it could not be loaded.
 */
export const ACTION_FAILED_TEXT =
  "Pausing or resuming may not have worked. The status below is checked again.";

/** The stages that have any job, in the order of the pipeline. */
export function activeStages(status: ProcessingStatus): StageJobs[] {
  return status.stages.filter((row) => row.queued + row.running + row.done + row.failed > 0);
}
