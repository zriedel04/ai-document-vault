import { Card, Stack, Text, Title } from "@mantine/core";

import { useHealthState } from "../api/health-context";
import { useSessionState } from "../api/session-context";
import type { HealthState } from "../api/useHealth";
import type { SessionState } from "../api/useSession";
import { ProcessingCard } from "./ProcessingCard";

function describe(health: HealthState): string {
  switch (health.status) {
    case "loading":
      return "Checking the backend...";
    case "ok":
      return `The backend is running (version ${health.version}).`;
    case "unreachable":
      return "The backend did not answer. Start it with `uv run python -m vault` in backend/.";
  }
}

function describeSession(state: SessionState): string {
  switch (state.status) {
    case "loading":
      return "Checking access...";
    case "signed-in":
      return `This browser has access, as ${state.name}.`;
    case "needs-token":
      return state.reason === "refused"
        ? "The access token was not accepted. Paste the current one above."
        : "This browser has no access token yet. See the notice above.";
    case "unreachable":
      return "Access could not be checked because the backend did not answer.";
  }
}

export function Dashboard() {
  const health = useHealthState();
  const { state: session } = useSessionState();
  return (
    <Stack>
      <Title order={2}>Dashboard</Title>
      <Card withBorder>
        <Title order={4}>Backend</Title>
        <Text role="status">{describe(health)}</Text>
      </Card>
      <Card withBorder>
        <Title order={4}>Access</Title>
        <Text>{describeSession(session)}</Text>
      </Card>
      <ProcessingCard />
    </Stack>
  );
}
