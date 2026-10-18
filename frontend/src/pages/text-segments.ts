import type { DocumentPage } from "../api/documents";

export type PageHeading = DocumentPage["headings"][number];

/** A run of a page's text; a run that starts at a heading carries it, so the outline can jump there. */
export interface Segment {
  text: string;
  heading: PageHeading | null;
}

/**
 * A page's text cut at its headings. The backend gives each heading's offset in characters (code
 * points, as Python counts), while a JavaScript string counts UTF-16 units, so a character outside
 * the Basic Multilingual Plane moves every later position by one: the text is walked by code
 * point. A heading that does not start inside the text, or not after the heading before it, is
 * left out (the backend refuses such pages, so this only guards the page against a bad answer).
 */
export function splitAtHeadings(page: DocumentPage): Segment[] {
  const { text } = page;
  const starts: { index: number; heading: PageHeading }[] = [];
  let codePoint = 0;
  let index = 0;
  for (const heading of page.headings) {
    if (!Number.isInteger(heading.offset) || heading.offset < codePoint) continue;
    if (starts.length > 0 && heading.offset === codePoint) continue;
    while (codePoint < heading.offset && index < text.length) {
      index += (text.codePointAt(index) ?? 0) > 0xffff ? 2 : 1;
      codePoint += 1;
    }
    if (codePoint !== heading.offset || index >= text.length) break;
    starts.push({ index, heading });
  }
  const segments: Segment[] = [];
  const first = starts[0]?.index ?? text.length;
  if (first > 0 || starts.length === 0)
    segments.push({ text: text.slice(0, first), heading: null });
  starts.forEach((start, position) => {
    const end = starts[position + 1]?.index ?? text.length;
    segments.push({ text: text.slice(start.index, end), heading: start.heading });
  });
  return segments;
}
