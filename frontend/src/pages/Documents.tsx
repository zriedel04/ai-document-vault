import {
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Group,
  Loader,
  NativeSelect,
  Pagination,
  SegmentedControl,
  Stack,
  Table,
  Tabs,
  Text,
  Title,
} from "@mantine/core";
import { Link } from "@tanstack/react-router";
import { useEffect } from "react";

import { NO_LABEL_TEXT } from "../api/labels";
import { TYPE_LABEL } from "../api/search";
import { useSessionState } from "../api/session-context";
import {
  DOCUMENT_TYPES,
  type DocumentItem,
  ERROR_TEXT,
  FILTER_STATES,
  type Order,
  PAGE_SIZE,
  SORT_KEYS,
  STATE_LABEL,
  type SortKey,
  fileName,
  formatDate,
  formatSize,
} from "../api/documents";
import { type ListError, useDocumentList } from "../api/useDocuments";
import { LabelFilter } from "../components/LabelFilter";
import { type DocumentsSearch, withFilters, withLabel } from "./documents-search";

const SORT_LABEL: Record<SortKey, string> = {
  added: "Date added",
  name: "Name",
  size: "Size",
};

const ORDER_LABEL: Record<Order, string> = { desc: "Descending", asc: "Ascending" };

const LIST_ERROR_TEXT: Record<ListError, string> = { ...ERROR_TEXT, "no-label": NO_LABEL_TEXT };

interface Props {
  search: DocumentsSearch;
  /** Show another sort, order, page, tab or filter; the route puts it in the address. */
  onSearch: (search: DocumentsSearch) => void;
}

/**
 * The document library: one page of documents in use, or of the trash, sorted by the backend, and
 * optionally only those under one label (the label or any label below it), of some types and in
 * some states. In the trash, a state is the one a document had when it went there.
 */
export function Documents({ search, onSearch }: Props) {
  const { state: session } = useSessionState();
  return (
    <Stack>
      <Title order={2}>Documents</Title>
      <Tabs
        value={search.trash ? "trash" : "library"}
        onChange={(value) => {
          onSearch({ ...search, trash: value === "trash", page: 1 });
        }}
      >
        <Tabs.List>
          <Tabs.Tab value="library">Library</Tabs.Tab>
          <Tabs.Tab value="trash">Trash</Tabs.Tab>
        </Tabs.List>
      </Tabs>
      <Group>
        <NativeSelect
          label="Sort by"
          data={SORT_KEYS.map((value) => ({ value, label: SORT_LABEL[value] }))}
          value={search.sort}
          onChange={(event) => {
            const sort = SORT_KEYS.find((key) => key === event.currentTarget.value);
            if (sort) onSearch({ ...search, sort, page: 1 });
          }}
        />
        <SegmentedControl
          aria-label="Order"
          mt="lg"
          data={Object.entries(ORDER_LABEL).map(([value, label]) => ({ value, label }))}
          value={search.order}
          onChange={(value) => {
            onSearch({ ...search, order: value === "asc" ? "asc" : "desc", page: 1 });
          }}
        />
        {session.status === "signed-in" && (
          <LabelFilter
            value={search.label ?? null}
            counts={!search.trash}
            onChange={(label) => {
              onSearch(withLabel(search, label));
            }}
          />
        )}
      </Group>
      <Group align="flex-start" gap="xl">
        <Checkbox.Group
          label="Type"
          description="None checked shows every type."
          value={search.type ?? []}
          onChange={(values) => {
            onSearch(withFilters(search, values, search.state ?? []));
          }}
        >
          <Group mt="xs">
            {DOCUMENT_TYPES.map((type) => (
              <Checkbox key={type} value={type} label={TYPE_LABEL[type]} />
            ))}
          </Group>
        </Checkbox.Group>
        <Checkbox.Group
          label="State"
          description={
            search.trash
              ? "The state each had before the trash. None checked shows every state."
              : "None checked shows every state."
          }
          value={search.state ?? []}
          onChange={(values) => {
            onSearch(withFilters(search, search.type ?? [], values));
          }}
        >
          <Group mt="xs">
            {FILTER_STATES.map((state) => (
              <Checkbox key={state} value={state} label={STATE_LABEL[state]} />
            ))}
          </Group>
        </Checkbox.Group>
      </Group>
      {session.status === "signed-in" ? (
        <DocumentPage search={search} onSearch={onSearch} />
      ) : session.status === "loading" ? (
        <Loader aria-label="Checking access" />
      ) : (
        <Text>{ERROR_TEXT[session.status === "unreachable" ? "unreachable" : "needs-token"]}</Text>
      )}
    </Stack>
  );
}

function DocumentPage({ search, onSearch }: Props) {
  const { state, reload } = useDocumentList(search);
  const pages =
    state.status === "ok" ? Math.max(1, Math.ceil(state.data.total / PAGE_SIZE)) : search.page;

  // A page past the end (the trash was emptied, or an old link) goes to the last page instead.
  useEffect(() => {
    if (state.status === "ok" && state.data.items.length === 0 && search.page > pages) {
      onSearch({ ...search, page: pages });
    }
  }, [state, search, pages, onSearch]);

  if (state.status === "loading") return <Loader aria-label="Loading documents" />;
  if (state.status === "error") {
    if (state.error === "no-label") {
      return (
        <Alert color="yellow" title="Label not found">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{LIST_ERROR_TEXT["no-label"]}</Text>
            <Button
              size="xs"
              variant="light"
              onClick={() => {
                onSearch(withLabel(search, null));
              }}
            >
              Show every document
            </Button>
          </Stack>
        </Alert>
      );
    }
    return (
      <Alert color="red" title="Documents not loaded">
        <Stack gap="xs" align="flex-start">
          <Text size="sm">{LIST_ERROR_TEXT[state.error]}</Text>
          <Button size="xs" variant="light" onClick={reload}>
            Try again
          </Button>
        </Stack>
      </Alert>
    );
  }
  const { items, total } = state.data;
  if (total === 0) {
    if (search.type !== undefined || search.state !== undefined) {
      return (
        <Stack gap="xs" align="flex-start">
          <Text c="dimmed">
            {search.trash
              ? "No document in the trash matches these filters."
              : "No document in the library matches these filters."}
          </Text>
          <Button
            size="xs"
            variant="light"
            onClick={() => {
              onSearch(withFilters(search, [], []));
            }}
          >
            Clear type and state
          </Button>
        </Stack>
      );
    }
    if (search.label !== undefined) {
      return (
        <Text c="dimmed">
          {search.trash
            ? "No document in the trash has this label."
            : "No document in the library has this label."}
        </Text>
      );
    }
    return <Text c="dimmed">{search.trash ? "The trash is empty." : "No documents yet."}</Text>;
  }
  const first = (search.page - 1) * PAGE_SIZE + 1;
  return (
    <Stack>
      <Text size="sm" c="dimmed" role="status">
        {items.length === 0
          ? `${String(total)} documents`
          : `Showing ${String(first)} to ${String(first + items.length - 1)} of ${String(total)} documents`}
      </Text>
      <Table.ScrollContainer minWidth={640}>
        <Table striped highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Name</Table.Th>
              <Table.Th>Type</Table.Th>
              <Table.Th>Size</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th>{search.trash ? "Moved to trash" : "Added"}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {items.map((item) => (
              <Row key={item.id} item={item} trash={search.trash} />
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
      {pages > 1 && (
        <Pagination
          total={pages}
          value={Math.min(search.page, pages)}
          onChange={(page) => {
            onSearch({ ...search, page });
          }}
          getControlProps={(control) => ({
            "aria-label": control === "previous" ? "Previous page" : "Next page",
          })}
        />
      )}
    </Stack>
  );
}

function Row({ item, trash }: { item: DocumentItem; trash: boolean }) {
  const when = trash && item.trashed_at !== null ? item.trashed_at : item.added_at;
  return (
    <Table.Tr>
      <Table.Td>
        <Anchor component={Link} to={`/documents/${encodeURIComponent(item.id)}`}>
          {fileName(item)}
        </Anchor>
        {item.sources > 1 && (
          <Text span size="xs" c="dimmed">
            {" "}
            ({String(item.sources)} imports)
          </Text>
        )}
      </Table.Td>
      <Table.Td>{item.extension.toUpperCase()}</Table.Td>
      <Table.Td>{formatSize(item.size)}</Table.Td>
      <Table.Td>
        <Badge variant="light" color={item.state === "failed" ? "red" : "gray"}>
          {STATE_LABEL[item.state]}
        </Badge>
      </Table.Td>
      <Table.Td>{formatDate(when)}</Table.Td>
    </Table.Tr>
  );
}
