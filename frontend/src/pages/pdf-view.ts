import type { DocumentProps } from "react-pdf";

/**
 * How PDF.js opens a document in the viewer. A PDF is untrusted input, so PDF.js gets only the
 * bytes the page fetched (never an address) and is told to fetch nothing else and run nothing:
 *
 * - No `cMapUrl`, `standardFontDataUrl`, `iccUrl`, `wasmUrl` or `docBaseUrl`, and no worker fetch,
 *   so PDF.js loads no resource of its own from anywhere. Fonts embedded in the file are loaded
 *   from its bytes; a font that is not embedded falls back to a system font, and characters that
 *   need one of Adobe's CMaps (some Chinese, Japanese and Korean PDFs) may not show.
 * - `useWasm: false`: no WebAssembly decoders (which the page's Content-Security-Policy would block
 *   anyway, as it has no `'wasm-unsafe-eval'`). JPEG 2000 images may not show.
 * - `enableXfa: false`: XFA forms, which PDF.js would turn into HTML, are not rendered.
 * - `verbosity: 0` (`VerbosityLevel.ERRORS`): PDF.js writes no warnings to the console, because a
 *   warning can quote the file (a font's name, say). Document content is never logged.
 *
 * PDF.js 6 has no `eval` path left (the `isEvalSupported` option of earlier releases was removed,
 * and `pdf-library.test.ts` checks the builds that are bundled for `eval(` and `new Function(`), so
 * there is no option to turn it off; the policy's `script-src 'self'` would refuse it anyway.
 * PDF.js runs a PDF's JavaScript only in the scripting sandbox of its own viewer application
 * (`pdf.sandbox.mjs`), which react-pdf does not load, and only through the annotation layer, which
 * `PdfViewer` does not render: no script, form field or link of the file reaches the page.
 */
export const PDF_OPTIONS = Object.freeze({
  verbosity: 0,
  enableXfa: false,
  useWasm: false,
  useWorkerFetch: false,
}) satisfies NonNullable<DocumentProps["options"]>;

/** The page the viewer shows, and whether the address asked for one past the end. */
export interface ShownPage {
  page: number;
  pastEnd: boolean;
}

/**
 * The page to show of a PDF with `pages` pages (at least 1), for the page the address asked for:
 * the first page when it asked for none, the last page (and `pastEnd`) when it asked for one past
 * the end. The address parser has already refused anything but a whole number from 1.
 */
export function pageToShow(requested: number | undefined, pages: number): ShownPage {
  if (requested === undefined) return { page: 1, pastEnd: false };
  if (requested > pages) return { page: pages, pastEnd: true };
  return { page: requested, pastEnd: false };
}

/**
 * A page typed into "Go to page", or null when it is not one of the PDF's pages. The field gives
 * a number or the text as typed, so only a whole number from 1 to `pages` is accepted.
 */
export function typedPage(value: string | number, pages: number): number | null {
  const page = typeof value === "number" ? value : /^[0-9]{1,9}$/.test(value) ? Number(value) : NaN;
  return Number.isSafeInteger(page) && page >= 1 && page <= pages ? page : null;
}
