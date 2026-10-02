import { Badge } from "@mantine/core";

import type { HealthState } from "../api/useHealth";

const LABELS = {
  loading: { color: "gray", text: "Backend: checking" },
  unreachable: { color: "red", text: "Backend: unreachable" },
} as const;

export function HealthBadge({ health }: { health: HealthState }) {
  if (health.status === "ok") {
    return (
      <Badge color="green" variant="light">
        Backend: ok
      </Badge>
    );
  }
  const { color, text } = LABELS[health.status];
  return (
    <Badge color={color} variant="light">
      {text}
    </Badge>
  );
}
