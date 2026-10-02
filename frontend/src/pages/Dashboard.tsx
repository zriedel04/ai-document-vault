import { Card, Stack, Text, Title } from "@mantine/core";

import { useHealthState } from "../api/health-context";
import type { HealthState } from "../api/useHealth";

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

export function Dashboard() {
  const health = useHealthState();
  return (
    <Stack>
      <Title order={2}>Dashboard</Title>
      <Card withBorder>
        <Title order={4}>Backend</Title>
        <Text role="status">{describe(health)}</Text>
      </Card>
    </Stack>
  );
}
