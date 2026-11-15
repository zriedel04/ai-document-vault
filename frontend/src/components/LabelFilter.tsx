import { NativeSelect } from "@mantine/core";

import { type LabelNode, labelText, useLabels } from "../api/labels";

const KIND_LABEL: Record<LabelNode["kind"], string> = {
  collection: "Collections",
  category: "Categories",
  course: "Courses",
  project: "Projects",
  tag: "Tags",
  topic: "Topics",
  person: "People",
  org: "Organizations",
  folder: "Folders",
};

/** The value of the "every document" choice; a label id is never empty. */
const ALL = "";

interface Props {
  /** The chosen label's id, or null for every document. */
  value: string | null;
  onChange: (value: string | null) => void;
  /** Show each label's count of documents in use (the trash tab does not: they count others). */
  counts: boolean;
}

/**
 * Choose a label to filter by. A label matches the documents that carry it or any label below it,
 * as the backend filters. Labels are grouped by kind and shown by their whole path, in the
 * backend's tree order; every name is the owner's text and is rendered as an option's text only.
 */
export function LabelFilter({ value, onChange, counts }: Props) {
  const { state } = useLabels();
  const items = state.status === "ok" ? state.data.items : [];
  const kinds = [...new Set(items.map((label) => label.kind))];
  const groups = kinds.map((kind) => ({
    group: KIND_LABEL[kind],
    items: items
      .filter((label) => label.kind === kind)
      .map((label) => ({ value: label.id, label: labelText(label, counts) })),
  }));
  // A label chosen in an old link that the vault no longer has: shown as such, never as another.
  const missing =
    value !== null && !items.some((label) => label.id === value)
      ? [{ value, label: state.status === "ok" ? "Label not found" : "Chosen label" }]
      : [];
  return (
    <NativeSelect
      label="Label"
      w={320}
      description={
        state.status === "error" ? "Labels could not be loaded." : "Includes the labels below it."
      }
      disabled={state.status !== "ok"}
      data={[{ value: ALL, label: "All documents" }, ...missing, ...groups]}
      value={value ?? ALL}
      onChange={(event) => {
        const chosen = event.currentTarget.value;
        onChange(chosen === ALL ? null : chosen);
      }}
    />
  );
}
