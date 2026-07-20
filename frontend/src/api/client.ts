/**
 * Shared fetch wrapper for `/api` requests (COL-33).
 *
 * Every route under `/api` is gated by the API-key middleware
 * (`collapsarr/auth.py`, COL-26): requests must carry the instance's
 * auto-generated key as the `X-Api-Key` header. This app is a self-hosted,
 * single-user surface in the *arr family tradition, so the key is stashed in
 * `localStorage` -- no login flow, no session cookies, just a key the user
 * copies once from Settings > General (`GeneralSection`, where the
 * auto-generated key is displayed) matching how Sonarr/Radarr/Bazarr surface
 * their own API keys.
 *
 * `apiFetch` is the one place that attaches the header, so every view's API
 * module (`wanted.ts`, `activity.ts`, `instances.ts`, `settings.ts`) routes
 * through it instead of calling the global `fetch` directly.
 */

const API_KEY_STORAGE_KEY = "collapsarr.apiKey";
const API_KEY_HEADER = "X-Api-Key";

/** Reads the locally-stored API key, or `""` if unset/unavailable. */
export function getStoredApiKey(): string {
  try {
    return globalThis.localStorage?.getItem(API_KEY_STORAGE_KEY) ?? "";
  } catch {
    // Storage can throw in locked-down environments (e.g. private
    // browsing with storage disabled) -- treat as "no key stored".
    return "";
  }
}

/** Persists (or, given `""`, clears) the API key used for outgoing requests. */
export function setStoredApiKey(key: string): void {
  try {
    const trimmed = key.trim();
    if (trimmed) {
      globalThis.localStorage?.setItem(API_KEY_STORAGE_KEY, trimmed);
    } else {
      globalThis.localStorage?.removeItem(API_KEY_STORAGE_KEY);
    }
  } catch {
    // Best-effort; nothing sensible to do if storage is unavailable.
  }
}

/**
 * `fetch` wrapper that attaches the stored `X-Api-Key` header (when set) to
 * every request. Callers get plain `Response` objects back, same as `fetch`
 * itself -- ok-checking and JSON parsing stay with each API module.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const apiKey = getStoredApiKey();
  const headers = new Headers(init.headers);
  if (apiKey) {
    headers.set(API_KEY_HEADER, apiKey);
  }
  return fetch(path, { ...init, headers });
}

/**
 * Extracts a human-readable error message from a failed API response.
 *
 * FastAPI's default error shape is `{"detail": "..."}` (see
 * `collapsarr/arr/routes.py`'s `HTTPException` usage and Pydantic validation
 * errors); this reads that when present and falls back to `fallback`
 * otherwise. Every caller treats this as the last read of the response body
 * on the error path, so the body is consumed directly rather than cloned.
 */
export async function apiErrorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string") {
        return detail;
      }
      if (Array.isArray(detail)) {
        // Pydantic validation errors: a list of {loc, msg, ...} objects.
        const messages = detail
          .map((item) => (item && typeof item === "object" && "msg" in item ? String((item as { msg: unknown }).msg) : null))
          .filter((message): message is string => Boolean(message));
        if (messages.length > 0) {
          return messages.join("; ");
        }
      }
    }
  } catch {
    // Body wasn't JSON (or was empty) -- fall through to the fallback.
  }
  return fallback;
}
