import { Alert, Button, Card, Group, Stack, Table, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";

import { useApi } from "../api/api-context";
import {
  ACTION_FAILED_TEXT,
  PROCESSING_ERROR_TEXT,
  type ProcessingError,
  type ProcessingStatus,
  STAGE_NAME,
  activeStages,
  processingError,
} from "../api/processing";
import { useSessionState } from "../api/session-context";

type Loaded =
  | { status: "loading" }
  | { status: "ok"; data: ProcessingStatus }
  | { status: "error"; error: ProcessingError };

function answered(data: ProcessingStatus | undefined, response: Response): Loaded {
  return data !== undefined
    ? { status: "ok", data }
    : { status: "error", error: processingError(response.status) };
}

/** Whether background processing runs, how many jobs each stage has, and Pause or Resume. */
export function ProcessingCard() {
  const api = useApi();
  const { state: session } = useSessionState();
  const [loaded, setLoaded] = useState<Loaded>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [actionFailed, setActionFailed] = useState(false);

  const fetchStatus = useCallback(async (): Promise<Loaded> => {
    try {
      const { data, response } = await api.GET("/api/processing");
      return answered(data, response);
    } catch {
      return { status: "error", error: "unreachable" };
    }
  }, [api]);

  // Only with access: without a token the request would be refused. A token pasted on this page
  // loads it. No session recheck on a refusal, since asking again after each one could loop.
  const signedIn = session.status === "signed-in";
  useEffect(() => {
    if (!signedIn) return;
    let cancelled = false;
    void fetchStatus().then((state) => {
      if (!cancelled) setLoaded(state);
    });
    return () => {
      cancelled = true;
    };
  }, [fetchStatus, signedIn]);

  async function toggle(pause: boolean) {
    setBusy(true);
    setActionFailed(false);
    try {
      const { data, response } = await (pause
        ? api.POST("/api/processing/pause")
        : api.POST("/api/processing/resume"));
      const state = answered(data, response);
      setLoaded(state);
      if (state.status === "error") setActionFailed(true);
    } catch {
      // The request may or may not have reached the backend: show what is true now.
      setActionFailed(true);
      setLoaded(await fetchStatus());
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card withBorder>
      <Title order={4}>Processing</Title>
      {!signedIn && <Text>{PROCESSING_ERROR_TEXT["needs-token"]}</Text>}
      {signedIn && loaded.status === "loading" && <Text>Checking background processing...</Text>}
      {signedIn && loaded.status === "error" && <Text>{PROCESSING_ERROR_TEXT[loaded.error]}</Text>}
      {signedIn && loaded.status === "ok" && (
        <Stack gap="sm">
          <Group justify="space-between">
            <Text role="status">
              {loaded.data.paused
                ? "Paused. Jobs already running finish, and no new job starts until you resume."
                : "Running. New documents are processed in the background."}
            </Text>
            <Button
              variant="default"
              loading={busy}
              onClick={() => void toggle(!loaded.data.paused)}
            >
              {loaded.data.paused ? "Resume" : "Pause"}
            </Button>
          </Group>
          <Text size="sm" c="dimmed">
            A pause lasts until you resume or the app restarts.
          </Text>
          <StageTable status={loaded.data} />
        </Stack>
      )}
      {actionFailed && (
        <Alert color="red" mt="sm">
          {ACTION_FAILED_TEXT}
        </Alert>
      )}
    </Card>
  );
}

function StageTable({ status }: { status: ProcessingStatus }) {
  const rows = activeStages(status);
  if (rows.length === 0) return <Text>No processing jobs yet.</Text>;
  return (
    <Table aria-label="Jobs by stage">
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Stage</Table.Th>
          <Table.Th>Waiting</Table.Th>
          <Table.Th>Running</Table.Th>
          <Table.Th>Done</Table.Th>
          <Table.Th>Failed</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {rows.map((row) => (
          <Table.Tr key={row.stage}>
            <Table.Td>{STAGE_NAME[row.stage]}</Table.Td>
            <Table.Td>{row.queued}</Table.Td>
            <Table.Td>{row.running}</Table.Td>
            <Table.Td>{row.done}</Table.Td>
            <Table.Td>{row.failed}</Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}
