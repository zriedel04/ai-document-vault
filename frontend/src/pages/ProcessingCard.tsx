import { Alert, Button, Card, Group, Stack, Table, Text, Title } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";

import { useApi } from "../api/api-context";
import {
  ACTION_FAILED_TEXT,
  POLL_INTERVAL_MS,
  PROCESSING_ERROR_TEXT,
  type ProcessingError,
  type ProcessingState,
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

/** The flag of a pause or resume's answer, or undefined for anything that is not one. */
function actionAnswer(data: ProcessingState | undefined): ProcessingState | undefined {
  return typeof data?.paused === "boolean" ? { paused: data.paused } : undefined;
}

/**
 * Whether background processing runs, how many jobs each stage has, and Pause or Resume.
 *
 * The status is asked for every POLL_INTERVAL_MS while the card is shown. A pause or resume
 * takes over from the polling while it runs: it ends by loading the status itself, whatever its
 * outcome, so what the card shows afterwards is what the backend said after the action (or that
 * it could not be loaded), never the status from before it. A poll sent before an action started
 * or ended is dropped when it answers, so an older answer cannot replace a newer one.
 */
export function ProcessingCard() {
  const api = useApi();
  const { state: session } = useSessionState();
  const [loaded, setLoaded] = useState<Loaded>({ status: "loading" });
  const [busy, setBusy] = useState(false);
  const [actionFailed, setActionFailed] = useState(false);
  // Bumped when an action starts and when it ends; a poll's answer is used only if unchanged.
  const epoch = useRef(0);
  const acting = useRef(false);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const fetchStatus = useCallback(async (): Promise<Loaded> => {
    try {
      const { data, response } = await api.GET("/api/processing");
      return answered(data, response);
    } catch {
      return { status: "error", error: "unreachable" };
    }
  }, [api]);

  // Only with access: without a token the request would be refused. A token pasted on this page
  // starts it. No session recheck on a refusal, since asking again after each one could loop.
  const signedIn = session.status === "signed-in";
  useEffect(() => {
    if (!signedIn) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll(first: boolean) {
      // The first load always runs; later ones wait while the tab is hidden. None runs during an
      // action, which loads the status itself when it ends.
      if (!acting.current && (first || document.visibilityState !== "hidden")) {
        const started = epoch.current;
        const state = await fetchStatus();
        if (stopped) return;
        if (epoch.current === started) setLoaded(state);
      }
      // Planned only after the answer, so two polls never overlap.
      if (!stopped) timer = setTimeout(() => void poll(false), POLL_INTERVAL_MS);
    }
    void poll(true);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [fetchStatus, signedIn]);

  // A function, so the check is read again after each await (a narrowed `.current` is not).
  const unmounted = () => !mounted.current;

  async function toggle(pause: boolean) {
    acting.current = true;
    epoch.current += 1;
    setBusy(true);
    setActionFailed(false);
    let answer: ProcessingState | undefined;
    try {
      const { data } = await (pause
        ? api.POST("/api/processing/pause")
        : api.POST("/api/processing/resume"));
      answer = actionAnswer(data);
    } catch {
      answer = undefined;
    }
    if (unmounted()) return;
    if (answer === undefined) {
      // Refused, an error or no answer: the action may or may not have reached the backend, so
      // the status shown before it is no longer known to be current.
      setActionFailed(true);
      setLoaded({ status: "loading" });
    } else {
      const { paused } = answer;
      setLoaded((last) =>
        last.status === "ok" ? { status: "ok", data: { ...last.data, paused } } : last,
      );
    }
    // Every outcome loads the status: the counts after the action, or what is true after one that
    // failed. If that load fails too, the card says that the status could not be loaded.
    const state = await fetchStatus();
    if (unmounted()) return;
    setLoaded(state);
    epoch.current += 1;
    acting.current = false;
    setBusy(false);
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
