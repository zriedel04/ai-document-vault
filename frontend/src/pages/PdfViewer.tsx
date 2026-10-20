import "react-pdf/dist/Page/TextLayer.css";

import { Alert, Box, Button, Group, Loader, NumberInput, Stack, Text, Title } from "@mantine/core";
import { useElementSize } from "@mantine/hooks";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?worker&url";
import { type SyntheticEvent, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";

import { VIEWER_ERROR_TEXT } from "../api/documents";
import { type PdfFile, useOriginalPdf } from "../api/useDocuments";
import { PDF_OPTIONS, pageToShow, typedPage } from "./pdf-view";

// The PDF.js worker is bundled by Vite as a file of this app (`?worker&url`), so it loads from the
// page's own origin under `script-src 'self'`, never from a CDN. react-pdf sets its own default
// when it is imported, so this is set here, after that import (react-pdf's README says the same).
pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

/** The widest the page is drawn, in CSS pixels; a narrower window draws it narrower. */
const MAX_WIDTH = 1000;

export const PDF_DAMAGED_TEXT =
  "This PDF could not be shown. It may be damaged; Download original saves it to open elsewhere.";
export const PDF_PASSWORD_TEXT =
  "This PDF is protected by a password, which the viewer does not ask for. Download original " +
  "saves it to open elsewhere.";
export const PAGE_FAILED_TEXT = "This page could not be shown.";

/**
 * The original PDF of a document, one page at a time, opened at `page` (from the address).
 *
 * The bytes come from the download endpoint through the API client, which sends the token in the
 * `Authorization` header (ADR 0016), and PDF.js is given the bytes, never an address, so it makes
 * no request of its own. `sha256` names the bytes: a new current version is fetched again.
 */
export function PdfViewer({
  documentId,
  sha256,
  page,
  onPageChange,
}: {
  documentId: string;
  sha256: string;
  page: number | undefined;
  onPageChange: (page: number) => void;
}) {
  const { state, reload } = useOriginalPdf(documentId, sha256);
  return (
    <Stack>
      <Title order={3}>PDF</Title>
      {state.status === "loading" && <Loader aria-label="Loading the PDF" />}
      {state.status === "error" && (
        <Alert color="red" title="PDF not loaded">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{VIEWER_ERROR_TEXT[state.error]}</Text>
            {state.error !== "not-found" && (
              <Button size="xs" variant="light" onClick={reload}>
                Try again
              </Button>
            )}
          </Stack>
        </Alert>
      )}
      {state.status === "ok" && <Pdf file={state.data} page={page} onPageChange={onPageChange} />}
    </Stack>
  );
}

type Opened =
  | { status: "opening" }
  | { status: "open"; pages: number }
  | { status: "failed"; password: boolean };

function Pdf({
  file,
  page,
  onPageChange,
}: {
  file: PdfFile;
  page: number | undefined;
  onPageChange: (page: number) => void;
}) {
  const [opened, setOpened] = useState<Opened>({ status: "opening" });
  // Set when PDF.js asks for a password, which is refused; the load then fails.
  const askedForPassword = useRef(false);
  const { ref, width } = useElementSize();

  return (
    <Box ref={ref}>
      <Document
        file={file}
        options={PDF_OPTIONS}
        suspense={false}
        onPassword={() => {
          askedForPassword.current = true;
          // PDF.js catches this and fails the load: the viewer never asks for a password.
          throw new Error("password refused");
        }}
        onLoadSuccess={(pdf) => {
          setOpened({ status: "open", pages: pdf.numPages });
        }}
        onLoadError={() => {
          setOpened({ status: "failed", password: askedForPassword.current });
        }}
        loading={<Loader aria-label="Opening the PDF" />}
        error={
          <Alert color="red" title="PDF not shown">
            {opened.status === "failed" && opened.password ? PDF_PASSWORD_TEXT : PDF_DAMAGED_TEXT}
          </Alert>
        }
      >
        {opened.status === "open" &&
          (opened.pages < 1 ? (
            <Text size="sm" c="dimmed">
              This PDF has no pages.
            </Text>
          ) : (
            <Pages
              pages={opened.pages}
              requested={page}
              width={width > 0 ? Math.min(width, MAX_WIDTH) : undefined}
              onPageChange={onPageChange}
            />
          ))}
      </Document>
    </Box>
  );
}

function Pages({
  pages,
  requested,
  width,
  onPageChange,
}: {
  pages: number;
  requested: number | undefined;
  width: number | undefined;
  onPageChange: (page: number) => void;
}) {
  const { page, pastEnd } = pageToShow(requested, pages);
  const [typed, setTyped] = useState<string | number>(page);
  const [typedWrong, setTypedWrong] = useState(false);

  // The field follows the page shown when it changes from elsewhere (Next, Back in the browser).
  const [shown, setShown] = useState(page);
  if (shown !== page) {
    setShown(page);
    setTyped(page);
    setTypedWrong(false);
  }

  const go = (event: SyntheticEvent) => {
    event.preventDefault();
    const target = typedPage(typed, pages);
    setTypedWrong(target === null);
    if (target !== null && target !== page) onPageChange(target);
  };

  return (
    <Stack>
      {pastEnd && (
        <Alert color="yellow" title="No such page" role="status">
          {`The address asks for page ${String(requested ?? page)}, but this PDF has ${String(pages)} ${pages === 1 ? "page" : "pages"}. The last page is shown.`}
        </Alert>
      )}
      <Group gap="sm" align="flex-end" role="group" aria-label="Pages">
        <Button
          variant="default"
          disabled={page <= 1}
          onClick={() => {
            onPageChange(page - 1);
          }}
        >
          Previous page
        </Button>
        <Button
          variant="default"
          disabled={page >= pages}
          onClick={() => {
            onPageChange(page + 1);
          }}
        >
          Next page
        </Button>
        <Text size="sm" aria-live="polite">
          {`Page ${String(page)} of ${String(pages)}`}
        </Text>
        <form onSubmit={go}>
          <Group gap="xs" align="flex-end">
            <NumberInput
              label="Go to page"
              value={typed}
              onChange={setTyped}
              min={1}
              max={pages}
              allowDecimal={false}
              allowNegative={false}
              hideControls
              w={110}
              error={typedWrong ? `A page from 1 to ${String(pages)}` : undefined}
            />
            <Button type="submit" variant="light">
              Go
            </Button>
          </Group>
        </form>
      </Group>
      <Page
        pageNumber={page}
        width={width}
        renderAnnotationLayer={false}
        renderForms={false}
        renderTextLayer
        loading={<Loader aria-label="Drawing the page" />}
        error={
          <Alert color="red" title="Page not shown">
            {PAGE_FAILED_TEXT}
          </Alert>
        }
      />
    </Stack>
  );
}
