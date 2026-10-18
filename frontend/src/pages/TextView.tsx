import { Alert, Anchor, Button, Card, Group, Loader, Stack, Text, Title } from "@mantine/core";
import { useId, useState } from "react";

import { type DocumentPages, PAGES_ERROR_TEXT } from "../api/documents";
import { useDocumentPages } from "../api/useDocuments";
import { type PageHeading, splitAtHeadings } from "./text-segments";

/** The id of a heading's place in the text, made from numbers only (never from its words). */
function anchorId(prefix: string, page: number, heading: number): string {
  return `${prefix}-p${String(page)}-h${String(heading)}`;
}

/** Bring a heading into view and move the keyboard focus to it. */
function jumpTo(id: string) {
  const target = window.document.getElementById(id);
  if (target === null) return;
  // jsdom has no scrollIntoView; browsers do.
  if (typeof target.scrollIntoView === "function") target.scrollIntoView({ block: "start" });
  target.focus({ preventScroll: true });
}

/**
 * The extracted text of a document's current version, a window of pages at a time, as plain text.
 * Every piece of text is a React text child, never HTML, so markup in a document is shown as
 * written. The outline lists the headings of the pages shown and jumps to them.
 */
export function TextView({ documentId }: { documentId: string }) {
  // The offsets of the windows before this one, so "Previous pages" returns to exactly the window
  // it came from (a window can hold fewer pages than asked for when its pages are long).
  const [trail, setTrail] = useState<number[]>([]);
  const [offset, setOffset] = useState(0);
  const { state, reload } = useDocumentPages(documentId, offset);

  const next = (to: number) => {
    setTrail((before) => [...before, offset]);
    setOffset(to);
  };
  const previous = () => {
    setOffset(trail.at(-1) ?? 0);
    setTrail((before) => before.slice(0, -1));
  };

  return (
    <Stack>
      <Title order={3}>Text</Title>
      {state.status === "loading" && <Loader aria-label="Loading the text" />}
      {state.status === "error" && (
        <Alert color="red" title="Text not loaded">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{PAGES_ERROR_TEXT[state.error]}</Text>
            {state.error !== "not-found" && (
              <Button size="xs" variant="light" onClick={reload}>
                Try again
              </Button>
            )}
          </Stack>
        </Alert>
      )}
      {state.status === "ok" && (
        <Window window={state.data} onNext={next} onPrevious={trail.length > 0 ? previous : null} />
      )}
    </Stack>
  );
}

function Window({
  window: shown,
  onNext,
  onPrevious,
}: {
  window: DocumentPages;
  onNext: (offset: number) => void;
  onPrevious: (() => void) | null;
}) {
  const prefix = useId();
  if (!shown.extracted) {
    return (
      <Text size="sm" c="dimmed">
        The text has not been extracted yet. It appears here once the text extraction step has run.
      </Text>
    );
  }
  const pages = shown.pages.map((page) => ({ page, segments: splitAtHeadings(page) }));
  const outline = pages.flatMap(({ page, segments }) =>
    segments
      .map((segment, position) => ({ heading: segment.heading, position }))
      .filter(
        (entry): entry is { heading: PageHeading; position: number } => entry.heading !== null,
      )
      .map((entry) => ({ ...entry, page: page.number })),
  );
  const first = shown.pages[0]?.number;
  const last = shown.pages.at(-1)?.number;

  const nextOffset = shown.next_offset;

  return (
    <Stack>
      <Text size="sm" c="dimmed">
        {first === undefined || last === undefined
          ? `No pages here. The text has ${String(shown.total)} pages.`
          : `Pages ${String(first)} to ${String(last)} of ${String(shown.total)}`}
      </Text>
      {outline.length > 0 && (
        <nav aria-label="Outline">
          <Stack gap={4} component="ul" style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {outline.map(({ heading, page, position }) => (
              <li
                key={anchorId(prefix, page, position)}
                style={{ paddingInlineStart: `${String((heading.level - 1) * 16)}px` }}
              >
                <Anchor
                  component="button"
                  type="button"
                  size="sm"
                  onClick={() => {
                    jumpTo(anchorId(prefix, page, position));
                  }}
                >
                  {heading.text}
                </Anchor>
                <Text span size="xs" c="dimmed">
                  {` p. ${String(page)}`}
                </Text>
              </li>
            ))}
          </Stack>
        </nav>
      )}
      {pages.map(({ page, segments }) => (
        <Card
          withBorder
          key={page.number}
          component="section"
          aria-label={`Page ${String(page.number)}`}
        >
          <Text size="xs" c="dimmed" mb="xs">
            Page {String(page.number)}
          </Text>
          {page.text.length === 0 ? (
            <Text size="sm" c="dimmed">
              This page has no text.
            </Text>
          ) : (
            <Text
              component="pre"
              size="sm"
              style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: 0 }}
            >
              {segments.map((segment, position) =>
                segment.heading === null ? (
                  <span key={position}>{segment.text}</span>
                ) : (
                  <span key={position} id={anchorId(prefix, page.number, position)} tabIndex={-1}>
                    {segment.text}
                  </span>
                ),
              )}
            </Text>
          )}
        </Card>
      ))}
      {(onPrevious !== null || nextOffset !== null) && (
        <Group gap="xs">
          {onPrevious !== null && (
            <Button size="xs" variant="default" onClick={onPrevious}>
              Previous pages
            </Button>
          )}
          {nextOffset !== null && (
            <Button
              size="xs"
              variant="default"
              onClick={() => {
                onNext(nextOffset);
              }}
            >
              Next pages
            </Button>
          )}
        </Group>
      )}
    </Stack>
  );
}
