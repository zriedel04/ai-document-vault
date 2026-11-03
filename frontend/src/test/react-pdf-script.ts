/**
 * What the react-pdf stand-in (`react-pdf.tsx`) plays and records: a test scripts the outcome (a
 * PDF with some pages, a damaged one or one that needs a password) and reads what the viewer
 * passed. `setup.ts` resets both after each test.
 */
import type { DocumentProps, PageProps } from "react-pdf";

export interface PdfScript {
  pages: number;
  fail?: "damaged" | "password";
  pageFails?: boolean;
}

const DEFAULT_SCRIPT: PdfScript = { pages: 3 };

export const playing: { script: PdfScript } = { script: DEFAULT_SCRIPT };

/** What the viewer handed to each `Document` and `Page` it rendered, in order. */
export const seen: { documents: DocumentProps[]; pages: PageProps[] } = {
  documents: [],
  pages: [],
};

export function scriptPdf(next: PdfScript) {
  playing.script = next;
}

export function resetPdf() {
  playing.script = DEFAULT_SCRIPT;
  seen.documents.length = 0;
  seen.pages.length = 0;
}

/** react-pdf's `pdfjs`, of which the viewer sets only the worker's address. */
export const pdfjs = { GlobalWorkerOptions: { workerSrc: "" } };
