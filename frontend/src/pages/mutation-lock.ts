/**
 * The document page's one lock for every request that changes the document and answers with it
 * (trash, restore, permanent delete, retry, and adding or removing a collection or tag). Each
 * such answer replaces the whole document the page shows, so two in flight at once could let the
 * older snapshot land last and hide the newer change (a label's answer from before a trash would
 * show the document in use again). With one lock only one runs at a time, and every control that
 * starts one is disabled while any runs.
 */
export interface MutationLock {
  /** Take the lock for a change; false, and nothing taken, when another change holds it. */
  begin: () => boolean;
  /** Give the lock back once the change has settled and its document is shown. */
  end: () => void;
  /** Whether any change of the page is running, for disabling the controls that start one. */
  busy: boolean;
}
