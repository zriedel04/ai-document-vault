/**
 * A stand-in for react-pdf's components in tests (`setup.ts` mocks the package with this module
 * and `react-pdf-script.ts`). jsdom has no canvas or workers, so PDF.js cannot run here; these
 * record what the viewer passes and play the outcome a test scripted.
 */
import { type ReactNode, useEffect, useState } from "react";
import type { DocumentProps, PageProps } from "react-pdf";

import { playing, seen } from "./react-pdf-script";

function shown(node: ReactNode | (() => ReactNode)): ReactNode {
  return typeof node === "function" ? node() : node;
}

export function Document(props: DocumentProps) {
  const [state, setState] = useState<"loading" | "ok" | "error">("loading");
  const { file, onLoadSuccess, onLoadError, onPassword } = props;

  useEffect(() => {
    seen.documents.push(props);
    const { script } = playing;
    let active = true;
    void Promise.resolve().then(() => {
      if (!active) return;
      if (script.fail === "password") {
        // What PDF.js does: it calls onPassword and fails the load when that throws.
        try {
          onPassword?.(() => undefined, 1);
        } catch {
          // refused, as the viewer means it to be
        }
      }
      if (script.fail !== undefined) {
        onLoadError?.(new Error("load failed"));
        setState("error");
        return;
      }
      // Only numPages is read by the viewer.
      onLoadSuccess?.({ numPages: script.pages } as Parameters<
        NonNullable<typeof onLoadSuccess>
      >[0]);
      setState("ok");
    });
    return () => {
      active = false;
    };
    // A new file is a new load, as in react-pdf; the callbacks are read when it ends.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file]);

  if (state === "loading") return <>{shown(props.loading)}</>;
  if (state === "error") return <>{shown(props.error)}</>;
  return <div data-testid="pdf-document">{props.children as ReactNode}</div>;
}

export function Page(props: PageProps) {
  seen.pages.push(props);
  if (playing.script.pageFails) return <>{shown(props.error)}</>;
  return <div data-testid="pdf-page" data-page={props.pageNumber} />;
}
