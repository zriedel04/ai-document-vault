import type { PickedFile } from "../api/useUploads";

/**
 * The files of a drop, each marked if the browser says it is a folder. A dropped folder appears
 * among the files as an entry the browser cannot read, which would fail as a lost connection, so
 * it is refused here without sending anything. The list is read during the event: the browser
 * empties it afterwards.
 */
export function droppedFiles(transfer: DataTransfer): PickedFile[] {
  const items = Array.from(transfer.items).filter((item) => item.kind === "file");
  if (items.length === 0) return Array.from(transfer.files, (file) => ({ file, folder: false }));
  const picked: PickedFile[] = [];
  for (const item of items) {
    const file = item.getAsFile();
    if (file === null) continue;
    picked.push({ file, folder: item.webkitGetAsEntry()?.isDirectory === true });
  }
  return picked;
}
