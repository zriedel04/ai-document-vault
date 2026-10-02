import { Stack, Text, Title } from "@mantine/core";

import type { NavItem } from "../nav";

export function Placeholder({ item }: { item: NavItem }) {
  return (
    <Stack>
      <Title order={2}>{item.label}</Title>
      <Text>{item.description}</Text>
      <Text c="dimmed">This page is a placeholder. The feature arrives in a later phase.</Text>
    </Stack>
  );
}
