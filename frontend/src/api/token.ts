/**
 * The per-install access token, kept in this tab's `sessionStorage` and sent as a bearer header.
 *
 * `sessionStorage` belongs to one origin (scheme, host and port), so another program serving a page
 * on a different local port cannot read it, which a cookie would not guarantee (ADR 0016). The
 * launcher hands the token over once, in the address fragment (`#token=...`): a fragment is never
 * sent to the server, so it appears in no request, access log or `Referer` header. The page stores
 * it and removes the fragment from the address bar before anything else runs.
 */

export const TOKEN_KEY = "vault.token";

/** The shape the backend creates (`security.TOKEN`): URL-safe characters, 32 to 128 of them. */
const TOKEN_SHAPE = /^[A-Za-z0-9_-]{32,128}$/;

// Used when the browser refuses storage (for example with site data blocked), so the page still
// works until it is reloaded.
let fallback: string | null = null;

export function isToken(value: string): boolean {
  return TOKEN_SHAPE.test(value);
}

function storage(): Storage | null {
  try {
    return globalThis.sessionStorage;
  } catch {
    return null;
  }
}

export function readToken(): string | null {
  try {
    const stored = storage()?.getItem(TOKEN_KEY) ?? null;
    if (stored !== null && isToken(stored)) return stored;
  } catch {
    // fall through to the in-memory copy
  }
  return fallback;
}

/** Remember a token. Anything that is not shaped like one is refused and false is returned. */
export function saveToken(value: string): boolean {
  if (!isToken(value)) return false;
  fallback = value;
  try {
    storage()?.setItem(TOKEN_KEY, value);
  } catch {
    // the in-memory copy above is enough for this page load
  }
  return true;
}

export function forgetToken(): void {
  fallback = null;
  try {
    storage()?.removeItem(TOKEN_KEY);
  } catch {
    // nothing stored, or storage is blocked
  }
}

type Place = Pick<Window, "location" | "history">;

/**
 * Take the token from `#token=...` in the address, store it and remove that part of the address.
 *
 * The fragment is removed whether or not it held a usable token, so a mistyped or stale one does
 * not stay in the address bar, the history or a bookmark. Other fragment parameters are kept.
 * Returns true when a token was stored.
 */
export function takeTokenFromUrl(place: Place = globalThis): boolean {
  const params = new URLSearchParams(place.location.hash.slice(1));
  if (!params.has("token")) return false;
  const value = params.get("token") ?? "";
  params.delete("token");
  const rest = params.toString();
  const { pathname, search } = place.location;
  place.history.replaceState(
    place.history.state,
    "",
    `${pathname}${search}${rest ? `#${rest}` : ""}`,
  );
  return saveToken(value);
}
