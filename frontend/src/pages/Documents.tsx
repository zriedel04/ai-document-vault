import {
  Alert,
  Anchor,
  Badge,
  Button,
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

import { useSessionState } from "../api/session-context";
import {
  type DocumentItem,
  ERROR_TEXT,
  type Order,
  PAGE_SIZE,
  SORT_KEYS,
  STATE_LABEL,
  type SortKey,
  fileName,
  formatDate,
  formatSize,
} from "../api/documents";
import { useDocumentList } from "../api/useDocuments";
import type { DocumentsSearch } from "./documents-search";

const SORT_LABEL: Record<SortKey, string> = {
  added: "Date added",
  name: "Name",
  size: "Size",
};

const ORDER_LABEL: Record<Order, string> = { desc: "Descending", asc: "Ascending" };

interface Props {
  search: DocumentsSearch;
  /** Show another sort, order, page or tab; the route puts it in the address. */
  onSearch: (search: DocumentsSearch) => void;
}

/** The document library: one page of documents in use, or of the trash, sorted by the backend. */
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
    return (
      <Alert color="red" title="Documents not loaded">
        <Stack gap="xs" align="flex-start">
          <Text size="sm">{ERROR_TEXT[state.error]}</Text>
          <Button size="xs" variant="light" onClick={reload}>
            Try again
          </Button>
        </Stack>
      </Alert>
    );
  }
  const { items, total } = state.data;
  if (total === 0) {
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
