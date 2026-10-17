import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Code,
  Group,
  Loader,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from "@mantine/core";
import { Link } from "@tanstack/react-router";
import { type ReactNode, useRef, useState } from "react";

import { useApi } from "../api/api-context";
import {
  type DocumentDetail as Detail,
  type DocumentsError,
  ERROR_TEXT,
  MOVE_ERROR_TEXT,
  MOVE_UNKNOWN_RELOAD_FAILED_TEXT,
  MOVE_UNKNOWN_TEXT,
  type MoveAction,
  STATE_LABEL,
  fileName,
  formatDate,
  formatSize,
  moveDocument,
  saveOriginal,
} from "../api/documents";
import { useSessionState } from "../api/session-context";
import { loadDocument, useDocument } from "../api/useDocuments";

const METHOD_LABEL: Record<Detail["import_sources"][number]["method"], string> = {
  upload: "Upload",
  folder: "Folder import",
  watch: "Watched folder",
};

const STAGE_LABEL: Record<Detail["jobs"][number]["stage"], string> = {
  extract: "Text extraction",
  ocr: "OCR",
  metadata: "Metadata",
  chunk: "Chunking",
  embed: "Embedding",
  enrich: "Enrichment",
};

const JOB_LABEL: Record<Detail["jobs"][number]["status"], string> = {
  queued: "Queued",
  running: "Running",
  done: "Done",
  failed: "Failed",
};

function BackLink() {
  return (
    <Anchor component={Link} to="/documents">
      Back to documents
    </Anchor>
  );
}

/** One document: what the list shows, plus its hash, version, every import and its jobs. */
export function DocumentDetail({ documentId }: { documentId: string }) {
  const { state: session } = useSessionState();
  if (session.status === "loading") return <Loader aria-label="Checking access" />;
  if (session.status !== "signed-in") {
    return (
      <Stack>
        <BackLink />
        <Text>{ERROR_TEXT[session.status === "unreachable" ? "unreachable" : "needs-token"]}</Text>
      </Stack>
    );
  }
  return <Loaded documentId={documentId} />;
}

function Loaded({ documentId }: { documentId: string }) {
  const { state, reload } = useDocument(documentId);
  if (state.status === "loading") return <Loader aria-label="Loading the document" />;
  if (state.status === "error") {
    return (
      <Stack align="flex-start">
        <BackLink />
        <Alert color="red" title="Document not loaded">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{ERROR_TEXT[state.error]}</Text>
            {state.error !== "not-found" && (
              <Button size="xs" variant="light" onClick={reload}>
                Try again
              </Button>
            )}
          </Stack>
        </Alert>
      </Stack>
    );
  }
  return <Details document={state.data} reload={reload} />;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text size="sm" component="div">
        {children}
      </Text>
    </div>
  );
}

const MOVED_TEXT: Record<MoveAction, { title: string; text: string }> = {
  trash: {
    title: "Moved to trash",
    text: "The document is in the trash. Restore brings it back as it was.",
  },
  restore: { title: "Restored", text: "The document is back in the library." },
};

/** How the last move ended, as the page tells it. */
type MoveOutcome =
  | { kind: "done"; action: MoveAction }
  | { kind: "refused"; action: MoveAction; error: DocumentsError }
  | { kind: "unknown" }
  | { kind: "unknown-reload-failed"; error: DocumentsError };

const MOVE_FAILED_TITLE: Record<MoveAction, string> = {
  trash: "Not moved to trash",
  restore: "Not restored",
};

function Details({ document: loaded, reload }: { document: Detail; reload: () => void }) {
  const api = useApi();
  const { recheck } = useSessionState();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<DocumentsError | null>(null);
  // The document as the last trash or restore returned it; it starts as the page loaded it.
  const [document, setDocument] = useState(loaded);
  const [moving, setMoving] = useState(false);
  // Set at once on a click, before React re-renders the disabled button, so a second click that
  // arrives in between sends nothing.
  const movingNow = useRef(false);
  const [outcome, setOutcome] = useState<MoveOutcome | null>(null);
  const trashed = document.state === "trashed";

  const move = (action: MoveAction) => {
    if (movingNow.current) return;
    movingNow.current = true;
    setMoving(true);
    setOutcome(null);
    const settle = (next: MoveOutcome, error?: DocumentsError) => {
      movingNow.current = false;
      setMoving(false);
      setOutcome(next);
      if (error === "needs-token") recheck(); // bring the token notice back (see useRequest)
    };
    void moveDocument(api, document.id, action).then(async (result) => {
      if (result.status === "ok") {
        setDocument(result.document);
        settle({ kind: "done", action });
        return;
      }
      if (result.status === "error") {
        settle({ kind: "refused", action, error: result.error }, result.error);
        return;
      }
      // The move may have been made: show the document as the backend now has it. The button
      // stays disabled until then. Trying again is safe either way, as both moves are idempotent.
      const reloaded = await loadDocument(api, document.id);
      if (reloaded.status === "ok") {
        setDocument(reloaded.data);
        settle({ kind: "unknown" });
      } else {
        settle({ kind: "unknown-reload-failed", error: reloaded.error }, reloaded.error);
      }
    });
  };

  const download = () => {
    setSaving(true);
    setSaveError(null);
    void saveOriginal(api, document).then((error) => {
      setSaving(false);
      setSaveError(error);
      if (error === "needs-token") recheck(); // bring the token notice back (see useRequest)
    });
  };

  return (
    <Stack>
      <BackLink />
      <Group justify="space-between" align="flex-start">
        <Title order={2} style={{ wordBreak: "break-word" }}>
          {fileName(document)}
        </Title>
        <Group gap="sm">
          {trashed ? (
            <Button
              variant="default"
              onClick={() => {
                move("restore");
              }}
              loading={moving}
            >
              Restore
            </Button>
          ) : (
            <Button
              variant="light"
              color="red"
              onClick={() => {
                move("trash");
              }}
              loading={moving}
            >
              Move to trash
            </Button>
          )}
          <Button onClick={download} loading={saving}>
            Download original
          </Button>
        </Group>
      </Group>
      {saveError !== null && (
        <Alert color="red" title="Download failed">
          {ERROR_TEXT[saveError]}
        </Alert>
      )}
      {outcome?.kind === "done" && (
        <Alert color="green" title={MOVED_TEXT[outcome.action].title} role="status">
          {MOVED_TEXT[outcome.action].text}
        </Alert>
      )}
      {outcome?.kind === "refused" && (
        <Alert color="red" title={MOVE_FAILED_TITLE[outcome.action]}>
          {MOVE_ERROR_TEXT[outcome.error]}
        </Alert>
      )}
      {outcome?.kind === "unknown" && (
        <Alert color="yellow" title="Change not confirmed">
          {MOVE_UNKNOWN_TEXT}
        </Alert>
      )}
      {outcome?.kind === "unknown-reload-failed" && (
        <Alert color="red" title="Change not confirmed">
          <Stack gap="xs" align="flex-start">
            <Text size="sm">{MOVE_UNKNOWN_RELOAD_FAILED_TEXT}</Text>
            <Text size="sm">{ERROR_TEXT[outcome.error]}</Text>
            <Button size="xs" variant="light" onClick={reload}>
              Reload
            </Button>
          </Stack>
        </Alert>
      )}
      <Card withBorder>
        <SimpleGrid cols={{ base: 1, sm: 3 }}>
          <Field label="Status">
            <Badge variant="light" color={document.state === "failed" ? "red" : "gray"}>
              {STATE_LABEL[document.state]}
            </Badge>
          </Field>
          <Field label="Type">
            {document.extension.toUpperCase()} ({document.mime})
          </Field>
          <Field label="Size">{formatSize(document.size)}</Field>
          <Field label="Added">{formatDate(document.added_at)}</Field>
          {document.trashed_at !== null && (
            <Field label="Moved to trash">{formatDate(document.trashed_at)}</Field>
          )}
          <Field label="Version">
            {String(document.version_no)}
            {document.version_origin === "import" ? " (as imported)" : ""}
          </Field>
          <Field label="SHA-256">
            <Code style={{ wordBreak: "break-all" }}>{document.sha256}</Code>
          </Field>
        </SimpleGrid>
      </Card>

      <Title order={3}>Imports</Title>
      <Text size="sm" c="dimmed">
        Every time these exact bytes were imported, oldest first. The first import names the
        document.
      </Text>
      <Table.ScrollContainer minWidth={560}>
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>File name</Table.Th>
              <Table.Th>Original location</Table.Th>
              <Table.Th>How</Table.Th>
              <Table.Th>Imported</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {document.import_sources.map((source) => (
              <Table.Tr key={source.id}>
                <Table.Td style={{ wordBreak: "break-word" }}>{source.filename}</Table.Td>
                <Table.Td style={{ wordBreak: "break-word" }}>
                  {source.original_path ??
                    (source.folders.length > 0
                      ? source.folders.join(" / ")
                      : "Not known (uploaded)")}
                </Table.Td>
                <Table.Td>{METHOD_LABEL[source.method]}</Table.Td>
                <Table.Td>{formatDate(source.imported_at)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>

      <Title order={3}>Processing</Title>
      {document.jobs.length === 0 ? (
        <Text size="sm" c="dimmed">
          No processing steps yet.
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={480}>
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Step</Table.Th>
                <Table.Th>Status</Table.Th>
                <Table.Th>Attempts</Table.Th>
                <Table.Th>Finished</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {document.jobs.map((job) => (
                <Table.Tr key={job.stage}>
                  <Table.Td>{STAGE_LABEL[job.stage]}</Table.Td>
                  <Table.Td>
                    {JOB_LABEL[job.status]}
                    {job.last_error !== null && (
                      <Text size="xs" c="red">
                        {job.last_error}
                      </Text>
                    )}
                  </Table.Td>
                  <Table.Td>
                    {String(job.attempts)} of {String(job.max_attempts)}
                  </Table.Td>
                  <Table.Td>
                    {job.finished_at === null ? "-" : formatDate(job.finished_at)}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
    </Stack>
  );
}
