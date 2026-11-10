import { Alert, Anchor, Box, Button, Group, Loader, Stack, Text, Title } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

import { useApi } from "../api/api-context";
import { type DocumentsError, ERROR_TEXT, fetchOriginal, formatSize } from "../api/documents";
import { useSessionState } from "../api/session-context";
import { type ImageType, LARGE_IMAGE_BYTES, LARGE_IMAGE_PIXELS, ZOOM_STEPS } from "./image-limits";

const IMAGE_ERROR_TEXT: Record<DocumentsError, string> = {
  ...ERROR_TEXT,
  unreachable: "The image could not be loaded. Check that the backend is running.",
};

type Fetched =
  | { status: "loading" }
  | { status: "error"; error: DocumentsError }
  | { status: "ok"; url: string };

/** "fit" shrinks the image to the width of the page (never wider than its own size). */
type Zoom = "fit" | number;

/**
 * The current version of a PNG or JPEG document, fitted to the width, with zoom and a link that
 * opens it alone. The bytes are fetched with the token header like every API call (ADR 0016) and
 * shown through a `blob:` address, which is released when the view goes or the document changes.
 */
export function ImageView({
  documentId,
  versionId,
  mime,
  size,
}: {
  documentId: string;
  versionId: string;
  mime: ImageType;
  size: number;
}) {
  const api = useApi();
  const { recheck } = useSessionState();
  const [wanted, setWanted] = useState(size <= LARGE_IMAGE_BYTES);
  const [attempt, setAttempt] = useState(0);
  // The answer with the request it belongs to, so an answer for an earlier document is never shown.
  const key = `${String(attempt)}:${documentId}:${versionId}`;
  const [answer, setAnswer] = useState<{ key: string; fetched: Fetched } | null>(null);
  const fetched: Fetched = answer?.key === key ? answer.fetched : { status: "loading" };

  useEffect(() => {
    if (!wanted) return;
    let cancelled = false;
    let url: string | null = null;
    void fetchOriginal(api, documentId).then((result) => {
      if (cancelled) return;
      if (result.status === "error") {
        setAnswer({ key, fetched: { status: "error", error: result.error } });
        if (result.error === "needs-token") recheck(); // bring the token notice back
        return;
      }
      // The same bytes under the document's own type (no copy is made), not the response's.
      url = URL.createObjectURL(result.blob.slice(0, result.blob.size, mime));
      setAnswer({ key, fetched: { status: "ok", url } });
    });
    return () => {
      cancelled = true;
      if (url !== null) URL.revokeObjectURL(url);
    };
    // `key` names the document, the version and the attempt.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, wanted, mime]);

  return (
    <Stack>
      <Title order={3}>Image</Title>
      {!wanted && (
        <Alert color="yellow" title="Large image">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">
              This image is {formatSize(size)}. Showing it holds the whole file in this page and may
              slow it down.
            </Text>
            <Button
              size="xs"
              variant="light"
              onClick={() => {
                setWanted(true);
              }}
            >
              Show image
            </Button>
          </Stack>
        </Alert>
      )}
      {wanted && fetched.status === "loading" && <Loader aria-label="Loading the image" />}
      {fetched.status === "error" && (
        <Alert color="red" title="Image not loaded">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{IMAGE_ERROR_TEXT[fetched.error]}</Text>
            {fetched.error !== "not-found" && (
              <Button
                size="xs"
                variant="light"
                onClick={() => {
                  setAttempt((n) => n + 1);
                }}
              >
                Try again
              </Button>
            )}
          </Stack>
        </Alert>
      )}
      {fetched.status === "ok" && <Picture url={fetched.url} />}
    </Stack>
  );
}

function Picture({ url }: { url: string }) {
  const image = useRef<HTMLImageElement>(null);
  const [natural, setNatural] = useState<{ width: number; height: number } | null>(null);
  const [broken, setBroken] = useState(false);
  const [allowed, setAllowed] = useState(false);
  const [zoom, setZoom] = useState<Zoom>("fit");

  if (broken) {
    return (
      <Alert color="red" title="Image not shown">
        The browser could not read this image. Download the original to open it elsewhere.
      </Alert>
    );
  }
  const huge = natural !== null && natural.width * natural.height > LARGE_IMAGE_PIXELS;
  // Until its size is known (and allowed), the image is not drawn, so it is not decoded either.
  const shown = natural !== null && (!huge || allowed);

  // The next step up or down from the scale drawn now. "Fit" is measured when a button is pressed
  // (reading the element during render would not be React's), so both buttons work from "Fit".
  const step = (direction: 1 | -1): number | undefined => {
    let current = 1;
    if (zoom !== "fit") current = zoom;
    else if (natural !== null && natural.width > 0 && (image.current?.clientWidth ?? 0) > 0) {
      current = (image.current?.clientWidth ?? 0) / natural.width;
    }
    return direction === 1
      ? ZOOM_STEPS.find((value) => value > current + 1e-6)
      : ZOOM_STEPS.findLast((value) => value < current - 1e-6);
  };
  const zoomBy = (direction: 1 | -1) => {
    const next = step(direction);
    if (next !== undefined) setZoom(next);
  };
  const first = ZOOM_STEPS[0];
  const last = ZOOM_STEPS[ZOOM_STEPS.length - 1];

  return (
    <Stack>
      {natural === null && <Loader aria-label="Reading the image" />}
      {huge && !allowed && (
        <Alert color="yellow" title="Very large image">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">
              This image is {String(natural.width)} × {String(natural.height)} pixels. Drawing it
              needs about {formatSize(natural.width * natural.height * 4)} of memory and may make
              the page slow or unresponsive.
            </Text>
            <Button
              size="xs"
              variant="light"
              onClick={() => {
                setAllowed(true);
              }}
            >
              Show anyway
            </Button>
          </Stack>
        </Alert>
      )}
      {shown && (
        <Group gap="xs">
          <Button
            size="xs"
            variant={zoom === "fit" ? "filled" : "default"}
            onClick={() => {
              setZoom("fit");
            }}
          >
            Fit
          </Button>
          <Button
            size="xs"
            variant={zoom === 1 ? "filled" : "default"}
            onClick={() => {
              setZoom(1);
            }}
          >
            100%
          </Button>
          <Button
            size="xs"
            variant="default"
            aria-label="Zoom out"
            disabled={zoom === first}
            onClick={() => {
              zoomBy(-1);
            }}
          >
            −
          </Button>
          <Button
            size="xs"
            variant="default"
            aria-label="Zoom in"
            disabled={zoom === last}
            onClick={() => {
              zoomBy(1);
            }}
          >
            +
          </Button>
          <Text size="sm" c="dimmed" aria-live="polite">
            {zoom === "fit" ? "Fitted to width" : `${String(Math.round(zoom * 100))}%`}
          </Text>
          <Anchor href={url} target="_blank" rel="noopener noreferrer" size="sm">
            Open image alone
          </Anchor>
        </Group>
      )}
      <Box style={{ overflow: "auto", maxHeight: "80vh" }}>
        <img
          ref={image}
          src={url}
          alt="The document's image"
          decoding="async"
          onLoad={(event) => {
            const { naturalWidth: width, naturalHeight: height } = event.currentTarget;
            setNatural({ width, height });
          }}
          onError={() => {
            setBroken(true);
          }}
          style={{
            display: shown ? "block" : "none",
            ...(zoom === "fit" || natural === null
              ? { maxWidth: "100%", height: "auto" }
              : { width: `${String(natural.width * zoom)}px`, maxWidth: "none", height: "auto" }),
          }}
        />
      </Box>
    </Stack>
  );
}
