import {
  Anchor,
  Badge,
  Button,
  FileButton,
  Group,
  Loader,
  Paper,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { IconUpload } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { type DragEvent, useEffect, useRef, useState } from "react";

import { ERROR_TEXT } from "../api/documents";
import { REFUSAL_TEXT, SUPPORTED_EXTENSIONS, UNKNOWN_TEXT } from "../api/imports";
import { useSessionState } from "../api/session-context";
import { useUploadsState } from "../api/uploads-context";
import { type UploadEntry, type UploadState, isFinished } from "../api/useUploads";
import { droppedFiles } from "./dropped-files";

const ACCEPT = SUPPORTED_EXTENSIONS.map((extension) => `.${extension}`).join(",");

/** Upload files into the vault by dropping them or choosing them, with each file's outcome. */
export function Import() {
  const { state: session } = useSessionState();
  return (
    <Stack>
      <Title order={2}>Import</Title>
      {session.status === "signed-in" ? (
        <Uploader />
      ) : session.status === "loading" ? (
        <Loader aria-label="Checking access" />
      ) : (
        <Text>{ERROR_TEXT[session.status === "unreachable" ? "unreachable" : "needs-token"]}</Text>
      )}
      <UploadList />
    </Stack>
  );
}

function Uploader() {
  const { add } = useUploadsState();
  const [over, setOver] = useState(false);
  const reset = useRef<() => void>(null);

  // A file dropped beside the zone would make the browser open it in place of the app, which
  // ends the queue; on this page such a drop does nothing.
  useEffect(() => {
    const ignore = (event: Event) => {
      event.preventDefault();
    };
    window.addEventListener("dragover", ignore);
    window.addEventListener("drop", ignore);
    return () => {
      window.removeEventListener("dragover", ignore);
      window.removeEventListener("drop", ignore);
    };
  }, []);

  const allowDrop = (event: DragEvent) => {
    event.preventDefault();
    setOver(true);
  };
  return (
    <Paper
      withBorder
      p="xl"
      aria-label="Drop files to import"
      role="region"
      bg={over ? "var(--mantine-primary-color-light)" : undefined}
      style={{ borderStyle: "dashed" }}
      onDragEnter={allowDrop}
      onDragOver={allowDrop}
      onDragLeave={() => {
        setOver(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        setOver(false);
        add(droppedFiles(event.dataTransfer));
      }}
    >
      <Stack align="center" gap="xs">
        <IconUpload aria-hidden size={32} />
        <Text>Drag files here, or</Text>
        <FileButton
          multiple
          accept={ACCEPT}
          resetRef={reset}
          inputProps={{ "aria-label": "Files to import" }}
          onChange={(files) => {
            add(files.map((file) => ({ file, folder: false })));
            // Let the same file be chosen again later.
            reset.current?.();
          }}
        >
          {(props) => <Button {...props}>Choose files</Button>}
        </FileButton>
        <Text size="sm" c="dimmed">
          Supported types: {SUPPORTED_EXTENSIONS.map((extension) => `.${extension}`).join(", ")}.
          Files are imported one at a time.
        </Text>
      </Stack>
    </Paper>
  );
}

function UploadList() {
  const { entries, clearFinished } = useUploadsState();
  if (entries.length === 0) return null;
  const finished = entries.filter((entry) => isFinished(entry.state)).length;
  return (
    <Stack>
      <Group justify="space-between">
        <Text size="sm" c="dimmed" role="status">
          {`${String(finished)} of ${String(entries.length)} files finished`}
        </Text>
        <Button size="xs" variant="light" disabled={finished === 0} onClick={clearFinished}>
          Clear finished
        </Button>
      </Group>
      <Table.ScrollContainer minWidth={480}>
        <Table aria-label="Uploads">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>File</Table.Th>
              <Table.Th>Status</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {entries.map((entry) => (
              <Row key={entry.key} entry={entry} />
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Stack>
  );
}

const LABEL: Record<UploadState["status"], string> = {
  waiting: "Waiting",
  uploading: "Uploading",
  imported: "Imported",
  duplicate: "Already in the vault",
  refused: "Refused",
  error: "Not imported",
  "not-sent": "Not sent",
  unknown: "Unknown",
};

const COLOR: Record<UploadState["status"], string> = {
  waiting: "gray",
  uploading: "blue",
  imported: "green",
  duplicate: "teal",
  refused: "red",
  error: "red",
  "not-sent": "gray",
  unknown: "yellow",
};

function detail(state: UploadState) {
  switch (state.status) {
    case "imported":
    case "duplicate":
      return (
        <Anchor component={Link} to={`/documents/${encodeURIComponent(state.documentId)}`}>
          Open document
        </Anchor>
      );
    case "refused":
      return REFUSAL_TEXT[state.reason];
    case "error":
    case "not-sent":
      return ERROR_TEXT[state.error];
    case "unknown":
      return UNKNOWN_TEXT;
    default:
      return null;
  }
}

function Row({ entry }: { entry: UploadEntry }) {
  const { state } = entry;
  const more = detail(state);
  return (
    <Table.Tr>
      <Table.Td style={{ wordBreak: "break-word" }}>{entry.name}</Table.Td>
      <Table.Td>
        <Group gap="xs" wrap="nowrap" align="flex-start">
          <Badge variant="light" color={COLOR[state.status]} style={{ flexShrink: 0 }}>
            {LABEL[state.status]}
          </Badge>
          {state.status === "uploading" && <Loader size="xs" aria-hidden />}
          {more !== null && <Text size="sm">{more}</Text>}
        </Group>
      </Table.Td>
    </Table.Tr>
  );
}
