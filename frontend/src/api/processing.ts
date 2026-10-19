import type { components } from "./schema";

export type ProcessingStatus = components["schemas"]["ProcessingStatus"];
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

/** What a pause or resume whose answer never came says; the status is then loaded again. */
export const ACTION_FAILED_TEXT =
  "The request did not go through. Showing the status as it is now.";

/** The stages that have any job, in the order of the pipeline. */
export function activeStages(status: ProcessingStatus): StageJobs[] {
  return status.stages.filter((row) => row.queued + row.running + row.done + row.failed > 0);
}
