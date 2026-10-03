import { Alert, Button, Code, Group, PasswordInput, Stack, Text } from "@mantine/core";
import { useState } from "react";

import { useSessionState } from "../api/session-context";
import { isToken, saveToken } from "../api/token";

/**
 * Shown while this browser has no accepted access token. The launcher normally opens the vault with
 * the token in the address; this is the fallback for a bookmark or a new tab. The token is typed
 * into a password field, never shown, and only kept in this tab (see `api/token.ts`).
 */
export function TokenBanner() {
  const { state, recheck } = useSessionState();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  if (state.status !== "needs-token") return null;

  const submit = (event: React.SyntheticEvent) => {
    event.preventDefault();
    const token = value.trim();
    if (!isToken(token)) {
      setError("That does not look like an access token.");
      return;
    }
    saveToken(token);
    setValue("");
    setError(null);
    recheck();
  };

  return (
    <Alert
      color="yellow"
      title={
        state.reason === "refused" ? "The access token was not accepted" : "Access token needed"
      }
      mb="md"
    >
      <form onSubmit={submit}>
        <Stack gap="xs">
          <Text size="sm">
            Open the vault with the launcher, which passes the token to this page. Or paste the
            token from the file <Code>local-token</Code> in your vault folder.
          </Text>
          <Group align="flex-end">
            <PasswordInput
              label="Access token"
              value={value}
              onChange={(event) => {
                setValue(event.currentTarget.value);
              }}
              error={error}
              autoComplete="off"
              w={360}
            />
            <Button type="submit">Save token</Button>
          </Group>
        </Stack>
      </form>
    </Alert>
  );
}
