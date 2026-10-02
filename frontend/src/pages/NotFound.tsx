import { Anchor, Stack, Text, Title } from "@mantine/core";
import { Link } from "@tanstack/react-router";

export function NotFound() {
  return (
    <Stack>
      <Title order={2}>Page not found</Title>
      <Text>
        There is nothing at this address. Go to the{" "}
        <Anchor component={Link} to="/">
          dashboard
        </Anchor>
        .
      </Text>
    </Stack>
  );
}
