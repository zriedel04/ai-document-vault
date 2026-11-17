import { Alert, Button, Group, NativeSelect, Stack, Text, TextInput, Title } from "@mantine/core";
import { useRef, useState } from "react";

import { useApi } from "../api/api-context";
import { type DocumentDetail, type DocumentsError, ERROR_TEXT } from "../api/documents";
import {
  type AssignError,
  type CreateError,
  LABEL_CHANGE_ERROR_TEXT,
  type LabelNode,
  type OwnerKind,
  PATH_SEPARATOR,
  createLabel,
  isOwnerKind,
  ownerAssignments,
  setDocumentLabel,
  useLabels,
} from "../api/labels";
import { useSessionState } from "../api/session-context";
import { loadDocument } from "../api/useDocuments";

const KIND_TEXT: Record<OwnerKind, string> = { collection: "Collection", tag: "Tag" };

/** The value of the empty choice in the "Add" list; a label id is never empty. */
const NONE = "";

type Outcome =
  | { kind: "refused"; error: CreateError | AssignError }
  | { kind: "unknown" }
  | { kind: "unknown-reload-failed"; error: DocumentsError };

function pathText(path: readonly string[]): string {
  return path.join(PATH_SEPARATOR);
}

/**
 * The document's collections and tags: each one the owner gave it, with Remove; a list of the
 * others to add; and a field to make a new collection at the top of the tree and add it at once.
 * Names are the owner's text and are only ever rendered as text. Every outcome is a fixed text
 * chosen by the status code (`LABEL_CHANGE_ERROR_TEXT`); an answer that does not prove the change
 * reloads the document, since both requests are safe to repeat.
 */
export function OwnerLabels({
  document,
  onDocument,
}: {
  document: DocumentDetail;
  onDocument: (document: DocumentDetail) => void;
}) {
  const api = useApi();
  const { recheck } = useSessionState();
  const { state: tree, reload: reloadTree } = useLabels();
  const [chosen, setChosen] = useState(NONE);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  // Set at once on a click, so a second click before the re-render sends nothing.
  const busyNow = useRef(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const assigned = ownerAssignments(document);
  const assignedIds = new Set(assigned.map((label) => label.id));
  const choices: LabelNode[] =
    tree.status === "ok"
      ? tree.data.items.filter((label) => isOwnerKind(label.kind) && !assignedIds.has(label.id))
      : [];

  const settle = (next: Outcome | null) => {
    busyNow.current = false;
    setBusy(false);
    setOutcome(next);
    const error = next !== null && "error" in next ? next.error : null;
    if (error === "needs-token") recheck(); // bring the token notice back (see useRequest)
  };

  const start = (): boolean => {
    if (busyNow.current) return false;
    busyNow.current = true;
    setBusy(true);
    setOutcome(null);
    return true;
  };

  const change = async (labelId: string, add: boolean) => {
    const result = await setDocumentLabel(api, document.id, labelId, add);
    if (result.status === "ok") {
      onDocument(result.document);
      reloadTree(); // the counts changed
      settle(null);
      return;
    }
    if (result.status === "error") {
      settle({ kind: "refused", error: result.error });
      return;
    }
    const reloaded = await loadDocument(api, document.id);
    if (reloaded.status === "ok") {
      onDocument(reloaded.data);
      reloadTree();
      settle({ kind: "unknown" });
    } else {
      settle({ kind: "unknown-reload-failed", error: reloaded.error });
    }
  };

  const toggle = (labelId: string, add: boolean) => {
    if (!start()) return;
    void change(labelId, add).then(() => {
      if (add) setChosen(NONE);
    });
  };

  const createAndAdd = () => {
    if (name.trim() === "" || !start()) return;
    void createLabel(api, "collection", name).then(async (created) => {
      if (created.status === "ok") {
        setName("");
        await change(created.label.id, true);
        return;
      }
      reloadTree();
      settle(
        created.status === "error"
          ? { kind: "refused", error: created.error }
          : { kind: "unknown" },
      );
    });
  };

  return (
    <Stack gap="sm">
      <Title order={3}>Collections and tags</Title>
      {assigned.length === 0 ? (
        <Text size="sm" c="dimmed">
          This document is in no collection and has no tags.
        </Text>
      ) : (
        <Stack gap={4}>
          {assigned.map((label) => (
            <Group key={label.id} gap="sm" wrap="nowrap">
              <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                <bdi>{pathText(label.path)}</bdi>{" "}
                <Text span size="xs" c="dimmed">
                  ({isOwnerKind(label.kind) ? KIND_TEXT[label.kind] : label.kind})
                </Text>
              </Text>
              <Button
                size="compact-xs"
                variant="subtle"
                color="red"
                disabled={busy}
                aria-label={`Remove ${pathText(label.path)}`}
                onClick={() => {
                  toggle(label.id, false);
                }}
              >
                Remove
              </Button>
            </Group>
          ))}
        </Stack>
      )}
      <Group align="flex-end" gap="sm">
        <NativeSelect
          label="Add to"
          w={320}
          disabled={tree.status !== "ok" || busy}
          description={tree.status === "error" ? "Labels could not be loaded." : undefined}
          data={[
            { value: NONE, label: choices.length > 0 ? "Choose a collection or tag" : "None left" },
            ...choices.map((label) => ({
              value: label.id,
              label: `${pathText(label.path)} (${isOwnerKind(label.kind) ? KIND_TEXT[label.kind] : label.kind})`,
            })),
          ]}
          value={chosen}
          onChange={(event) => {
            setChosen(event.currentTarget.value);
          }}
        />
        <Button
          variant="light"
          disabled={chosen === NONE || busy}
          onClick={() => {
            toggle(chosen, true);
          }}
        >
          Add
        </Button>
      </Group>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          createAndAdd();
        }}
      >
        <Group align="flex-end" gap="sm">
          <TextInput
            label="New collection"
            w={320}
            maxLength={255}
            autoComplete="off"
            value={name}
            disabled={busy}
            onChange={(event) => {
              setName(event.currentTarget.value);
            }}
          />
          <Button type="submit" variant="light" disabled={name.trim() === ""} loading={busy}>
            Create and add
          </Button>
        </Group>
      </form>
      {outcome?.kind === "refused" && (
        <Alert color="red" title="Not changed">
          {LABEL_CHANGE_ERROR_TEXT[outcome.error]}
        </Alert>
      )}
      {outcome?.kind === "unknown" && (
        <Alert color="yellow" title="Change not confirmed">
          We could not confirm the change. Showing the document as it is now.
        </Alert>
      )}
      {outcome?.kind === "unknown-reload-failed" && (
        <Alert color="red" title="Change not confirmed">
          We could not confirm the change, and the document could not be loaded again.{" "}
          {ERROR_TEXT[outcome.error]}
        </Alert>
      )}
    </Stack>
  );
}
