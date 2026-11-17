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
  FIRST_IMPORT_DAY,
  LAST_IMPORT_DAY,
  MAX_SEARCH_PAGE,
  RANGE_ERROR_TEXT,
  SEARCH_ERROR_TEXT,
  SEARCH_PAGE_SIZE,
  SEARCH_TYPES,
  type SearchHit,
  type SearchQuery,
  type SearchType,
  type Segment,
  TYPE_LABEL,
  importedRange,
} from "../api/search";
import { useSessionState } from "../api/session-context";
import { useSearchResults } from "../api/useSearch";
import { LabelFilter } from "../components/LabelFilter";
import { resultPlace } from "./documents-search";

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
 * left. The type, label and import date filters are kept the same way. The form has no action
 * and its field no name, so even a native submission would carry no query, and the browser is
 * asked not to remember what was typed.
 */
export function Search() {
  const { state: session } = useSessionState();
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const [types, setTypes] = useState<SearchType[]>([]);
  const [label, setLabel] = useState<string | null>(null);
  // The days as the date fields give them ("YYYY-MM-DD"), "" for an open side.
  const [firstDay, setFirstDay] = useState("");
  const [lastDay, setLastDay] = useState("");
  const [page, setPage] = useState(1);
  const range = importedRange(firstDay, lastDay);
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
      {session.status === "signed-in" && (
        <LabelFilter
          value={label}
          counts
          onChange={(value) => {
            setLabel(value);
            setPage(1);
          }}
        />
      )}
      <Group align="flex-start">
        <DayField label="Imported from" value={firstDay} onChange={setFirstDay} onPage={setPage} />
        <DayField label="Imported to" value={lastDay} onChange={setLastDay} onPage={setPage} />
      </Group>
      {query === null ? null : "error" in range ? (
        <Alert color="yellow" title="Search not run">
          <Text size="sm">{RANGE_ERROR_TEXT[range.error]}</Text>
        </Alert>
      ) : session.status === "signed-in" ? (
        <Results
          query={{
            query,
            types,
            label,
            importedFrom: range.from,
            importedTo: range.to,
            page,
          }}
          run={run}
          onPage={setPage}
        />
      ) : session.status === "loading" ? (
        <Loader aria-label="Checking access" />
      ) : (
        <Text>{ERROR_TEXT[session.status === "unreachable" ? "unreachable" : "needs-token"]}</Text>
      )}
    </Stack>
  );
}

/**
 * One side of the import date filter: a day in this computer's time zone, which the request
 * turns into the first or last instant of that day (`importedRange`). Empty leaves the side open.
 */
function DayField({
  label,
  value,
  onChange,
  onPage,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  onPage: (page: number) => void;
}) {
  return (
    <TextInput
      label={label}
      description="A day in this computer's time zone."
      type="date"
      min={FIRST_IMPORT_DAY}
      max={LAST_IMPORT_DAY}
      value={value}
      onChange={(event) => {
        onChange(event.currentTarget.value);
        onPage(1);
      }}
      w={200}
    />
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
    if (state.error === "refused" || state.error === "no-label") {
      return (
        <Alert color="yellow" title="Search not run">
          <Text size="sm">{SEARCH_ERROR_TEXT[state.error]}</Text>
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
 * One matching document: its name (linking to its page, at the result's page for a PDF), type,
 * section and the best passage. The page number is shown for a PDF, the one type whose pages are
 * the file's own.
 */
function Hit({ hit }: { hit: SearchHit }) {
  const name = fileName({ id: hit.document_id, name: hit.name, extension: hit.extension });
  const sections = hit.section_highlight.filter((title) => title.length > 0);
  return (
    <Card withBorder component="li">
      <Group gap="xs" wrap="nowrap" align="baseline">
        <Anchor
          renderRoot={(props) => (
            <Link
              {...props}
              to="/documents/$documentId"
              params={{ documentId: hit.document_id }}
              search={resultPlace(hit)}
            />
          )}
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
