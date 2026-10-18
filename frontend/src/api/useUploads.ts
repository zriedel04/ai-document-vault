import { useCallback, useRef, useState } from "react";

import type { Api } from "./client";
import { type UploadResult, uploadFile } from "./imports";

/** One file the owner added: waiting, uploading, finished, or not sent after a run stopped. */
export type UploadState =
  | { status: "waiting" }
  | { status: "uploading" }
  | UploadResult
  | { status: "not-sent"; error: Extract<UploadResult, { status: "error" }>["error"] };

export interface UploadEntry {
  key: number;
  /** The file's name, shown in the list. The owner's own data: never logged. */
  name: string;
  state: UploadState;
}

/** A file to upload, or a folder the browser offered among the files (refused without sending). */
export interface PickedFile {
  file: File;
  folder: boolean;
}

export interface Uploads {
  entries: readonly UploadEntry[];
  /** Add the files of one drop or one choice; they share one import batch. */
  add: (files: readonly PickedFile[]) => void;
  /** Remove every entry that is no longer waiting or uploading. */
  clearFinished: () => void;
}

interface Job {
  key: number;
  file: File;
  drop: number;
}

export function isFinished(state: UploadState): boolean {
  return state.status !== "waiting" && state.status !== "uploading";
}

/**
 * The upload queue. Files go up one at a time, in the order they were added, so the backend's two
 * upload slots are never all taken by one page and the order of the list is the order of import.
 * The first file of a drop is sent without a batch, and the rest of that drop with the batch the
 * first answer named (`X-Vault-Batch`), so each drop is one import batch. A file whose answer named
 * no batch (a refused request, an unknown outcome) leaves the next file of the drop to start it.
 *
 * It lives above the router, so leaving the page does not stop the uploads. A refused token or a
 * closed vault stops the run: the files still waiting are marked not sent, and a refused token asks
 * the session again, so the token notice comes back.
 */
export function useUploads(api: Api, recheck: () => void): Uploads {
  const [entries, setEntries] = useState<UploadEntry[]>([]);
  const queue = useRef<Job[]>([]);
  const running = useRef(false);
  const batches = useRef(new Map<number, string>());
  const counter = useRef(0);

  const setState = useCallback((keys: ReadonlySet<number>, state: UploadState) => {
    setEntries((list) => list.map((entry) => (keys.has(entry.key) ? { ...entry, state } : entry)));
  }, []);

  const run = useCallback(async () => {
    // One run at a time: a drop during a run only lengthens the queue.
    if (running.current) return;
    running.current = true;
    try {
      for (let job = queue.current.shift(); job; job = queue.current.shift()) {
        setState(new Set([job.key]), { status: "uploading" });
        const result = await uploadFile(api, job.file, batches.current.get(job.drop) ?? null);
        if ("batchId" in result && result.batchId !== null && !batches.current.has(job.drop)) {
          batches.current.set(job.drop, result.batchId);
        }
        setState(new Set([job.key]), result);
        if (result.status === "error") {
          const rest = queue.current.splice(0);
          setState(new Set(rest.map((waiting) => waiting.key)), {
            status: "not-sent",
            error: result.error,
          });
          if (result.error === "needs-token") recheck();
        }
      }
    } finally {
      running.current = false;
    }
  }, [api, recheck, setState]);

  const add = useCallback(
    (files: readonly PickedFile[]) => {
      if (files.length === 0) return;
      counter.current += 1;
      const drop = counter.current;
      const added: UploadEntry[] = [];
      for (const { file, folder } of files) {
        counter.current += 1;
        const key = counter.current;
        if (folder) {
          added.push({
            key,
            name: file.name,
            state: { status: "refused", reason: "folder", batchId: null },
          });
        } else {
          added.push({ key, name: file.name, state: { status: "waiting" } });
          queue.current.push({ key, file, drop });
        }
      }
      setEntries((list) => [...list, ...added]);
      void run();
    },
    [run],
  );

  const clearFinished = useCallback(() => {
    setEntries((list) => list.filter((entry) => !isFinished(entry.state)));
  }, []);

  return { entries, add, clearFinished };
}
