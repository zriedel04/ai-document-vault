import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Checkbox,
  Group,
  Loader,
  Mark,
  Pagination,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { Link } from "@tanstack/react-router";
import { type SyntheticEvent, useEffect, useState } from "react";

import { ERROR_TEXT, fileName } from "../api/documents";
import {
  MAX_SEARCH_PAGE,
  SEARCH_ERROR_TEXT,
  SEARCH_PAGE_SIZE,
  SEARCH_TYPES,
  type SearchHit,
  type SearchQuery,
  type SearchType,
  type Segment,
  TYPE_LABEL,
} from "../api/search";
import { useSessionState } from "../api/session-context";
import { useSearchResults } from "../api/useSearch";

/**
 * Text with the words the query matched marked. Every segment is a React text child, never HTML:
 * a snippet is document text, and markup in it is shown as written.
 */
export function Highlighted({ segments }: { segments: readonly Segment[] }) {
  return (
    <>
      {segments.map((segment, position) =>
        segment.match ? (
          <Mark key={position}>{segment.text}</Mark>
        ) : (
          <span key={position}>{segment.text}</span>
        ),
      )}
    </>
  );
}

/**
 * Keyword search over the vault. The query is kept in this component's state only: it never goes
 * in the address, the browser's history or a log (plan section 8), and it is gone when the page is
 * left. The form has no action and its field no name, so even a native submission would carry no
 * query, and the browser is asked not to remember what was typed.
 */
export function Search() {
  const { state: session } = useSessionState();
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const [types, setTypes] = useState<SearchType[]>([]);
  const [page, setPage] = useState(1);
  const [run, setRun] = useState(0);

  const submit = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    setQuery(draft.trim() === "" ? null : draft);
    setPage(1);
    setRun((value) => value + 1);
  };

  return (
    <Stack>
      <Title order={2}>Search</Title>
      <form role="search" aria-label="Search the vault" onSubmit={submit} autoComplete="off">
        <Group align="flex-end">
          <TextInput
            label="Search for"
            description='Every word must match. Use "quotes" for a phrase and a * at the end of a word to match its start.'
            type="search"
            autoComplete="off"
            spellCheck={false}
            value={draft}
            onChange={(event) => {
              setDraft(event.currentTarget.value);
            }}
            style={{ flex: 1, minWidth: 240 }}
          />
          <Button type="submit">Search</Button>
        </Group>
      </form>
      <Checkbox.Group
        label="Types"
        description="None checked searches every type."
        value={types}
        onChange={(values) => {
          setTypes(SEARCH_TYPES.filter((type) => values.includes(type)));
          setPage(1);
        }}
      >
        <Group mt="xs">
          {SEARCH_TYPES.map((type) => (
            <Checkbox key={type} value={type} label={TYPE_LABEL[type]} />
          ))}
        </Group>
      </Checkbox.Group>
      {query === null ? null : session.status === "signed-in" ? (
        <Results query={{ query, types, page }} run={run} onPage={setPage} />
      ) : session.status === "loading" ? (
        <Loader aria-label="Checking access" />
      ) : (
        <Text>{ERROR_TEXT[session.status === "unreachable" ? "unreachable" : "needs-token"]}</Text>
      )}
    </Stack>
  );
}

function Results({
  query,
  run,
  onPage,
}: {
  query: SearchQuery;
  run: number;
  onPage: (page: number) => void;
}) {
  const { state, reload } = useSearchResults(query, run);
  const pages =
    state.status === "ok"
      ? Math.min(MAX_SEARCH_PAGE, Math.max(1, Math.ceil(state.data.total / SEARCH_PAGE_SIZE)))
      : query.page;

  // A page past the end (documents changed since the last page) goes to the last page instead.
  useEffect(() => {
    if (state.status === "ok" && state.data.items.length === 0 && query.page > pages) {
      onPage(pages);
    }
  }, [state, query.page, pages, onPage]);

  if (state.status === "loading") return <Loader aria-label="Searching" />;
  if (state.status === "error") {
    if (state.error === "refused") {
      return (
        <Alert color="yellow" title="Search not run">
          <Text size="sm">{SEARCH_ERROR_TEXT.refused}</Text>
        </Alert>
      );
    }
    return (
      <Alert color="red" title="Search failed">
        <Stack gap="xs" align="flex-start">
          <Text size="sm">{SEARCH_ERROR_TEXT[state.error]}</Text>
          <Button size="xs" variant="light" onClick={reload}>
            Try again
          </Button>
        </Stack>
      </Alert>
    );
  }
  const { items, total } = state.data;
  if (total === 0) {
    return (
      <Text c="dimmed" role="status">
        No documents match. Try fewer words, or end a word with * to match its start.
      </Text>
    );
  }
  const first = (query.page - 1) * SEARCH_PAGE_SIZE + 1;
  return (
    <Stack>
      <Text size="sm" c="dimmed" role="status">
        {items.length === 0
          ? `${String(total)} matching documents`
          : `Showing ${String(first)} to ${String(first + items.length - 1)} of ${String(total)} matching documents`}
      </Text>
      <Stack gap="sm" component="ol" aria-label="Results" style={{ listStyle: "none", padding: 0 }}>
        {items.map((hit) => (
          <Hit key={hit.document_id} hit={hit} />
        ))}
      </Stack>
      {pages > 1 && (
        <Pagination
          total={pages}
          value={Math.min(query.page, pages)}
          onChange={onPage}
          getControlProps={(control) => ({
            "aria-label": control === "previous" ? "Previous page" : "Next page",
          })}
        />
      )}
    </Stack>
  );
}

/**
 * One matching document: its name (linking to its page), type, section and the best passage. The
 * text view cannot open at a page yet, so the link goes to the document and the page number is
 * shown beside it for a PDF, the one type whose pages are the file's own.
 */
function Hit({ hit }: { hit: SearchHit }) {
  const name = fileName({ id: hit.document_id, name: hit.name, extension: hit.extension });
  const sections = hit.section_highlight.filter((title) => title.length > 0);
  return (
    <Card withBorder component="li">
      <Group gap="xs" wrap="nowrap" align="baseline">
        <Anchor
          component={Link}
          to={`/documents/${encodeURIComponent(hit.document_id)}`}
          fw={500}
          style={{ overflowWrap: "anywhere" }}
        >
          {hit.name !== null && hit.name_highlight.length > 0 ? (
            <Highlighted segments={hit.name_highlight} />
          ) : (
            name
          )}
        </Anchor>
        <Badge variant="light" color="gray">
          {hit.extension.toUpperCase()}
        </Badge>
      </Group>
      <Text size="xs" c="dimmed" mt={4}>
        {hit.extension === "pdf" && `Page ${String(hit.page)}`}
        {hit.extension === "pdf" && sections.length > 0 && " · "}
        {sections.map((title, position) => (
          <span key={position}>
            {position > 0 && " › "}
            <Highlighted segments={title} />
          </span>
        ))}
      </Text>
      {hit.snippet.length > 0 && (
        <Text size="sm" mt="xs" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          <Highlighted segments={hit.snippet} />
        </Text>
      )}
      {hit.matches > 1 && (
        <Text size="xs" c="dimmed" mt={4}>
          {`${String(hit.matches)} matching passages`}
        </Text>
      )}
    </Card>
  );
}
